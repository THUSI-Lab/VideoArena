#!/usr/bin/env python3
"""
Deterministic verifier for the Video-Arena BADMINTON three-tier task.
Pure code -- no VLM/LLM judge. Same time-matched F1 + macro_acc design as
gfootball/go/sc2 (tasks/gfootball/judge.py), so a badminton
number is comparable with a football number rather than merely adjacent to it.

V(t) mechanism A: V = pts_near - pts_far within one set, read from the rally
annotation. No evaluator, no rollout, no win probability. Every label is an
objective function of the score sequence the match produced.

THREE TIERS over the same video (the paper's (t, c, a) -> dV -> V ladder):
    T1  the event      -> game_clock_s, scoring_side in {near, far},
                          zone in {net, out, in_front, in_mid, in_back}
    T2  the dV it made -> impact in {extended, taken, levelled, narrowed}
    T3  V after it     -> score_after as the pair (near, far)

Scoring = maximum 1:1 time-matched F1: predicted rows are matched to GT rows
with the SAME key and |dt| <= TOL, taking the largest such matching. reward = F1.
Order-independent; one wrong or missing row costs exactly one event.

WHY THE T1 ACTOR IS A ZONE AND NOT A SHOT TYPE
----------------------------------------------
ShuttleSet labels 18 shot types and one of them is literally 未知球種 ("unknown
shot type") -- the annotators could not always tell even from the real match
video. An answer key with an unanswerable class in it measures the
labeller, not the model. The landing zone is geometry against painted lines that
are visible in the frame, so it is answerable in the same sense the timestamp is.

WHY near/far IS NOT PART OF THE ZONE
------------------------------------
It is already the other half of the T1 key. A winning shot must land in the
OPPONENT's half and a net error falls on the LOSER's own side, so the physical
half is a function of the scorer; spelling it out twice would hand it over for
free. See badminton_zones.py for the measurement (934/937 error points and
487/511 in-court points obeyed it, and the 24 exceptions are what uncovered a
ground-truth-inverting end-swap bug).

THE ALPHABET IS READ OUT OF THE GT FILE, NOT COPIED INTO THIS ONE
-----------------------------------------------------------------
gen_badminton_case.py writes `zone_alphabet` into every GT file. This judge
asserts its own canonical tuple against it and reports a mismatch loudly instead
of scoring against a stale copy -- two copies of an alphabet is two alphabets,
and the failure mode is silent partial credit.

TOLERANCE
---------
|dt| <= 3 s, as in gfootball. Here it is provably unambiguous rather than
conventional: the smallest gap between consecutive points anywhere in the corpus
is 10.46 s (p1 14.10, median 30.36), so a 3 s window around one point cannot
reach another. gen_badminton_case.py re-asserts this per case.

WHAT T3 DOES AND DOES NOT MEASURE (the same caveat gfootball carries)
--------------------------------------------------------------------
With V a score difference, the running score is a deterministic function of a
COMPLETE T1 -- score_after is the running count of prior scoring_side, +1. So T3
here measures whether the model enumerated every point in a ~20 minute video
with replays and an interval in it, not an independent situation judgement. That
is a real and separately-failing skill (one missed point shifts every later pair,
and a badminton set has 30-59 of them against football's 15.7 goals), but the
paper must not claim T3 is independent evidence here. It is independent in
doudizhu/Go, where V is a win probability.

TWO DIAGNOSTICS THAT EXIST BECAUSE ZERO IS UNINFORMATIVE
--------------------------------------------------------
  * `side_swapped` variants of T1 and T3. The two ends of a badminton court are
    the same court photographed from one end, and a model that reads the rally
    perfectly but names the wrong end scores 0 on every tier. Reporting the score
    it WOULD have got under a global near<->far flip separates "cannot see who
    won the rally" from "saw everything and mirrored the court", which are
    different failures with different fixes. It is a diagnostic and never the
    reward: near/far are defined by the camera, so getting them right is part of
    the task.
  * `self_consistent_pairs`. The fraction of the answer's own rows whose
    score_after agrees with the running count implied by its own earlier
    scoring_side rows. A model that writes plausible-looking pairs without
    counting scores near 0 here even when a few pairs happen to land; a model
    that counted and mis-saw one point stays near 1. Cheap, and it reads the
    answer only against itself, so it is not a second GT.

Answer format (JSON written by the agent):
    {"events": [
        {"type":"point","idx":1,"game_clock_s":14.9,"scoring_side":"near",
         "zone":"in_back","impact":"taken","score_after":[1,0]},
        ...]}

Scores reported:
  strict     -- F1 with the FULL key (scoring_side, zone, impact, score_after)
                + |dt|<=3s. PRIMARY reward.
  tiers.t1_localization -- F1 with key (scoring_side, zone) + |dt|<=3s.
  tiers.t2_impact -- acc + macro_acc over the 4 impact classes, paired via the
                T1 time-match. macro_acc is the headline (majority-proof).
  tiers.t3_score -- exact-pair accuracy (headline), per-side accuracy (partial
                credit), per-side MAE, and the modal-pair baseline.

Usage:
    python judge.py --answer answer.json --gt gt.json [--out reward.json]
                    [--tol 3.0] [--primary auto|strict|t1]
"""
import argparse
import heapq
import json
import re
import sys
from collections import Counter, defaultdict

TOL = 3.0  # seconds; event-timing standard (same as Go/SC2//gfootball)

# near = the half nearest the camera. Assigned per case by a recorded seed, so "near"
# carries no information about who won the match -- ShuttleSet's own A/B does, which is
# why it never reaches a case.
SIDE_ALIAS = {
    "NEAR": "near", "N": "near", "BOTTOM": "near", "FRONT": "near",
    "近": "near", "近端": "near", "下": "near", "近场": "near", "0": "near",
    "FAR": "far", "F": "far", "TOP": "far", "BACK": "far",
    "远": "far", "远端": "far", "上": "far", "远场": "far", "1": "far",
}

ZONES = ("net", "out", "in_front", "in_mid", "in_back")
# Kept deliberately tight: the five labels the task statement gives the model,
# the obvious orthographic variants, and their Chinese glosses.
# A model that invents a sixth zone simply fails to match.
ZONE_ALIAS = {
    "net": "net", "intonet": "net", "innet": "net", "shortofnet": "net",
    "hitnet": "net", "nothingover": "net", "notovernet": "net", "网": "net",
    "挂网": "net", "掛網": "net", "未过网": "net", "未過網": "net", "下网": "net",
    "out": "out", "outofbounds": "out", "outside": "out", "wide": "out",
    "long": "out", "outlong": "out", "outwide": "out", "出界": "out", "界外": "out",
    "infront": "in_front", "front": "in_front", "forecourt": "in_front",
    "net_zone": "in_front", "frontcourt": "in_front", "前场": "in_front",
    "网前": "in_front", "前": "in_front",
    "inmid": "in_mid", "mid": "in_mid", "midcourt": "in_mid",
    "middle": "in_mid", "中场": "in_mid", "中": "in_mid",
    "inback": "in_back", "back": "in_back", "backcourt": "in_back",
    "rear": "in_back", "deep": "in_back", "后场": "in_back", "後場": "in_back",
    "底线": "in_back", "后": "in_back",
}

IMPACT_CLASSES = ("extended", "taken", "levelled", "narrowed")
# Same four labels and the same spellings gfootball accepts, on purpose: V is a
# score difference in both games, so `u` -- the scorer's deficit immediately
# before the event -- partitions the same four ways and the two tiers' T2
# numbers are directly comparable.
IMPACT_ALIAS = {
    "extended": "extended", "extend": "extended", "extends": "extended",
    "extendedlead": "extended", "扩大领先": "extended", "扩大": "extended",
    "taken": "taken", "take": "taken", "takelead": "taken",
    "takenlead": "taken", "取得领先": "taken", "打破平分": "taken",
    "levelled": "levelled", "leveled": "levelled", "level": "levelled",
    "levels": "levelled", "equalised": "levelled", "equalized": "levelled",
    "equaliser": "levelled", "equalizer": "levelled", "追平": "levelled",
    "narrowed": "narrowed", "narrow": "narrowed", "narrows": "narrowed",
    "reduced": "narrowed", "追近": "narrowed",
}


def _key(x):
    """Casefold, strip everything that is not alphanumeric or CJK."""
    return re.sub(r"[^0-9A-Za-z一-鿿]+", "", str(x)).casefold()


def norm_side(x):
    if x is None:
        return None
    s = str(x).strip().upper()
    return SIDE_ALIAS.get(s, SIDE_ALIAS.get(_key(x).upper(), s.lower()))


def norm_zone(x):
    if x is None:
        return None
    return ZONE_ALIAS.get(_key(x))


def norm_impact(x):
    if x is None:
        return None
    return IMPACT_ALIAS.get(_key(x))


def norm_pair(x):
    """score_after -> (int, int) or None. Accepts [a,b], (a,b), 'a-b', 'a:b'."""
    if x is None:
        return None
    if isinstance(x, (list, tuple)):
        if len(x) != 2:
            return None
        try:
            return (int(x[0]), int(x[1]))
        except Exception:
            return None
    m = re.match(r"^\s*(\d+)\s*[-:to –]+\s*(\d+)\s*$", str(x))
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return None


def norm_time(r):
    for k in ("game_clock_s", "t_video_s", "t_seconds", "t"):
        if k in r and r[k] is not None:
            try:
                return float(r[k])
            except Exception:
                return None
    return None


def as_rows(obj):
    """The GT's `points` and an answer's `sequence` are both accepted, and a bare
    list too. GT carries `points` (rich) and `sequence` (answer-shaped); reading
    `sequence` first means the judge compares like with like."""
    seq = obj
    if isinstance(obj, dict):
        seq = obj.get("events")
        if not isinstance(seq, list):
            seq = obj.get("points", obj)
    if not isinstance(seq, list):
        return []
    rows = []
    for r in seq:
        if not isinstance(r, dict):
            continue
        rows.append({
            "type": "point",
            "game_clock_s": norm_time(r),
            "scoring_side": norm_side(r.get("scoring_side",
                                            r.get("side", r.get("winner")))),
            "zone": norm_zone(r.get("zone", r.get("landing_zone",
                                                  r.get("area")))),
            "impact": norm_impact(r.get("impact", r.get("scorer_impact"))),
            # `score`/`scoreline` alias score_after: a scoreline quoted for a
            # point is the scoreline that point produced. `score_before` is
            # deliberately NOT in this chain -- it is a different pair of
            # integers, and aliasing it would hand full credit to an answer that
            # got the tier wrong.
            "score_after": norm_pair(r.get("score_after",
                                           r.get("score", r.get("scoreline")))),
        })
    return rows


def count_score_before_only(obj):
    """Rows carrying score_before but no usable score_after. Diagnostic only, so
    an answer giving the pre-point score reads as a format mismatch
    instead of scoring 0 on T3 for no visible reason."""
    seq = obj
    if isinstance(obj, dict):
        seq = obj.get("events") if isinstance(obj.get("events"), list) \
            else obj.get("points", obj)
    if not isinstance(seq, list):
        return 0
    n = 0
    for r in seq:
        if not isinstance(r, dict):
            continue
        if norm_pair(r.get("score_after", r.get("score", r.get("scoreline")))) \
                is None and norm_pair(r.get("score_before")) is not None:
            n += 1
    return n


def flip_rows(rows):
    """Every row with near<->far exchanged, in the side AND in the pair. For the
    end-mirror diagnostic; never used for the reward."""
    out = []
    for r in rows:
        s = r["scoring_side"]
        p = r["score_after"]
        out.append({**r,
                    "scoring_side": ("far" if s == "near" else
                                     "near" if s == "far" else s),
                    "score_after": (p[1], p[0]) if p else None})
    return out


def self_consistency(rows):
    """Does the answer's own score_after agree with the running count implied by
    its own scoring_side sequence? Reads the answer against itself only.

    Rows are taken in time order, and a row with no time cannot be placed, so it
    is counted as inconsistent rather than dropped -- an unplaceable row is
    exactly what a model that did not follow the rally produces."""
    have = [r for r in rows if r["score_after"] is not None]
    if not have:
        return None, 0
    ordered = sorted(rows, key=lambda r: (r["game_clock_s"] is None,
                                          r["game_clock_s"] or 0.0))
    n = ok = a = b = 0
    for r in ordered:
        if r["score_after"] is None:
            continue
        n += 1
        if r["scoring_side"] == "near":
            a += 1
        elif r["scoring_side"] == "far":
            b += 1
        if r["score_after"] == (a, b):
            ok += 1
        else:
            # a mismatch RESYNCS to the answer's own claim rather than poisoning
            # the tail: the measure asks whether one chain was counted, so a
            # single miscount should cost one row, not every row after it.
            a, b = r["score_after"]
    return round(ok / n, 6), n


def gt_has_strategy(gt):
    return any(r["impact"] is not None or r["score_after"] is not None
               for r in gt)


def k_strict(r):
    return (r["scoring_side"], r["zone"], r["impact"], r["score_after"])


def k_t1(r):
    return (r["scoring_side"], r["zone"])


def _differ(a, g):
    """How many of GT row g's labelled fields pred row a gets wrong (time and private
    keys aside): the tie-break between GT rows at the same distance."""
    return sum(1 for k, v in g.items()
               if k != "game_clock_s" and not k.startswith("_") and v is not None
               and a.get(k) != v)


def _assign(cand, n_gt):
    """Maximum 1:1 matching of least total cost. cand[j] = [(cost, i), ...]: pred j may
    take GT row i at an integer cost. Successive shortest augmenting paths, searched
    from ALL free preds at once (Dijkstra on reduced costs), so the result has the most
    pairs and, of the matchings that large, the least total cost -- a pred never keeps
    a farther GT row just because it was looked at first. Returns owner[i] = the pred
    holding GT i, or -1."""
    P, INF = 1 + n_gt, float("inf")      # node ids: sink 0, GT row i 1+i, pred j P+j
    mate, owner = [-1] * len(cand), [-1] * n_gt
    price = [dict((i, c) for c, i in cs) for cs in cand]
    pot = [0] * (P + len(cand))
    while True:
        dist, prev = [INF] * len(pot), [-1] * len(pot)
        heap = [(0, P + j) for j in range(len(cand)) if mate[j] < 0 and cand[j]]
        for _, v in heap:                # sorted, so already a heap
            dist[v] = 0
        while heap:
            d, u = heapq.heappop(heap)
            if d > dist[u]:
                continue
            if u == 0:                   # the sink: the cheapest augmenting path
                break
            if u >= P:
                j = u - P
                steps = [(1 + i, c) for c, i in cand[j] if i != mate[j]]
            else:
                j = owner[u - 1]
                steps = [(P + j, -price[j][u - 1])] if j >= 0 else [(0, 0)]
            for v, c in steps:
                nd = d + c + pot[u] - pot[v]
                if nd < dist[v]:
                    dist[v], prev[v] = nd, u
                    heapq.heappush(heap, (nd, v))
        if dist[0] == INF:
            return owner
        for v in range(len(pot)):        # capped at the sink's distance: still feasible
            pot[v] += min(dist[v], dist[0])
        i = prev[0] - 1
        while i >= 0:
            j = prev[1 + i] - P
            nxt = mate[j]
            mate[j], owner[i] = i, j
            i = nxt


def match(pred, gt, keyfn, tol):
    """Maximum 1:1 time-matched matching. Returns
    (f1, precision, recall, tp, fp, fn, pairs) where pairs = [(gt_index, pred_row)].
    A pred row may pair with a GT row of the SAME key within |dt| <= tol; the judge
    takes the LARGEST set of disjoint pairs, so neither row order nor a near-miss
    earlier in the list can cost another row its match -- and, of the matchings that
    large, the one with the least total |dt| (then the fewest disagreeing fields, for
    an exact time tie), so the tiers read off `pairs` compare each row with its
    nearest GT partner. Pred rows are put in (time, content) order first, so the
    pairs are deterministic too.
    A row with no parseable game_clock_s cannot match (time is part of the answer)."""
    order = sorted((a for a in pred
                    if a["game_clock_s"] is not None
                    and abs(a["game_clock_s"]) < 1e18),      # NaN/inf fail too
                   key=lambda a: (a["game_clock_s"],
                                  json.dumps(a, sort_keys=True, default=str)))
    gkeys = [keyfn(g) if g["game_clock_s"] is not None else None for g in gt]
    big = 1 + sum(len(g) for g in gt)       # > any matching's total field disagreement
    cand = []
    for a in order:
        at, ak = a["game_clock_s"], keyfn(a)
        cand.append([(round(abs(g["game_clock_s"] - at) * 1e6) * big + _differ(a, g), i)
                     for i, g in enumerate(gt) if gkeys[i] is not None and gkeys[i] == ak
                     and abs(g["game_clock_s"] - at) <= tol])
    owner = _assign(cand, len(gt))
    pairs = [(i, order[j]) for i, j in enumerate(owner) if j >= 0]
    tp = len(pairs)
    fp = max(0, len(pred) - tp)
    fn = len(gt) - tp
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0
    return f1, p, r, tp, fp, fn, pairs


def _macro_acc(pairs):
    """Macro (balanced) accuracy = mean per-class recall over the GT classes
    PRESENT in this case. Defeats the majority-class shortcut: a constant answer
    scores 1/(number of GT classes), not the majority frequency."""
    tot, hit = defaultdict(int), defaultdict(int)
    for g, p in pairs:
        tot[g] += 1
        if p == g:
            hit[g] += 1
    if not tot:
        return None, {}
    per = {c: round(hit[c] / tot[c], 6) for c in tot}
    macro = round(sum(per.values()) / len(per), 6)
    return macro, per


def score_tiers(pred, gt, tol):
    """T2/T3 over GT point rows, paired via the T1 (scoring_side, zone)
    time-match. A GT point not localized within +-tol counts as wrong for its
    class -- the tiers are conditional on having found the event, which is the
    point of a nested ladder."""
    _, _, _, _, _, _, t1_pairs = match(pred, gt, k_t1, tol)
    pred_for_gt = {gi: a for gi, a in t1_pairs}
    total = len(gt)
    localized = sum(1 for i in range(total) if i in pred_for_gt)
    has_t2 = any(r["impact"] is not None for r in gt)
    has_t3 = any(r["score_after"] is not None for r in gt)

    t2_pairs = []
    t2_ok = t3_exact = side_hits = side_total = 0
    abs_err_n, abs_err_f = [], []
    gt_pairs_seq = []
    for i, g in enumerate(gt):
        a = pred_for_gt.get(i)
        if has_t2:
            pim = a["impact"] if a else None
            t2_pairs.append((g["impact"], pim))
            if pim is not None and pim == g["impact"]:
                t2_ok += 1
        if has_t3 and g["score_after"] is not None:
            gt_pairs_seq.append(g["score_after"])
            pp = a["score_after"] if a else None
            side_total += 2
            if pp is not None:
                if pp[0] == g["score_after"][0]:
                    side_hits += 1
                if pp[1] == g["score_after"][1]:
                    side_hits += 1
                if pp == g["score_after"]:
                    t3_exact += 1
                abs_err_n.append(abs(pp[0] - g["score_after"][0]))
                abs_err_f.append(abs(pp[1] - g["score_after"][1]))

    out = {"localized_points": localized, "total_points": total,
           "t2_impact": None, "t3_score": None}

    if has_t2:
        macro, per = _macro_acc(t2_pairs)
        gt_dist = Counter(g for g, _ in t2_pairs)
        out["t2_impact"] = {
            "correct": t2_ok, "of_localized": localized, "of_all": total,
            "acc_localized": round(t2_ok / localized, 6) if localized else 0.0,
            "acc_all": round(t2_ok / total, 6) if total else 0.0,
            "macro_acc": macro,              # HEADLINE (shortcut-resistant)
            "per_class_recall": per,
            "gt_class_counts": dict(gt_dist),
            "chance_macro_acc": round(1.0 / len(gt_dist), 6) if gt_dist else None,
        }

    if has_t3 and gt_pairs_seq:
        n = len(gt_pairs_seq)
        modal = Counter(gt_pairs_seq).most_common(1)[0]
        out["t3_score"] = {
            "exact_pairs": t3_exact, "of_localized": localized, "of_all": total,
            "exact_pair_acc": round(t3_exact / n, 6),   # HEADLINE
            "per_side_acc": round(side_hits / side_total, 6) if side_total else 0.0,
            "mae_near": round(sum(abs_err_n) / len(abs_err_n), 4) if abs_err_n else None,
            "mae_far": round(sum(abs_err_f) / len(abs_err_f), 4) if abs_err_f else None,
            "distinct_gt_pairs": len(set(gt_pairs_seq)),
            "modal_pair_baseline": round(modal[1] / n, 6),
            "modal_pair": list(modal[0]),
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--answer", required=True)
    ap.add_argument("--gt", required=True)
    ap.add_argument("--out", default="reward.json")
    ap.add_argument("--tol", type=float, default=TOL)
    ap.add_argument("--primary", default="auto", choices=["auto", "strict", "t1"])
    args = ap.parse_args()
    tol = args.tol

    raw = None
    try:
        with open(args.answer, encoding="utf-8-sig") as f:
            raw = json.load(f)
        ans = as_rows(raw)
    except Exception as e:
        print("answer unreadable (%s) -> 0" % e, file=sys.stderr)
        ans = []
    with open(args.gt, encoding="utf-8") as f:
        gt_doc = json.load(f)
    gt = as_rows(gt_doc)

    # The alphabet is the case's, not this file's. A divergence is loud.
    gt_alpha = gt_doc.get("zone_alphabet") if isinstance(gt_doc, dict) else None
    alpha_note = None
    if gt_alpha is not None and tuple(gt_alpha) != ZONES:
        alpha_note = ("GT zone_alphabet %s != judge ZONES %s -- the case was "
                      "built against a different alphabet, so every zone "
                      "comparison below is suspect"
                      % (list(gt_alpha), list(ZONES)))
        print("[judge][WARN] %s" % alpha_note, file=sys.stderr)

    ws = gt_has_strategy(gt)
    s_f1, s_p, s_r, s_tp, s_fp, s_fn, _ = match(ans, gt, k_strict, tol)
    t1_f1, t1_p, t1_r, t1_tp, t1_fp, t1_fn, _ = match(ans, gt, k_t1, tol)
    tiers = score_tiers(ans, gt, tol)

    primary = args.primary
    if primary == "auto":
        primary = "strict" if ws else "t1"
    reward = s_f1 if primary == "strict" else t1_f1

    diag = {}
    for t in (5.0, 10.0):
        diag["strict_f1_tol%ds" % int(t)] = round(match(ans, gt, k_strict, t)[0], 6)

    # end-mirror diagnostic: what the same answer would score with near<->far
    # exchanged everywhere. Diagnostic only -- see the module docstring.
    fl = flip_rows(ans)
    fl_t1 = match(fl, gt, k_t1, tol)[0]
    fl_tiers = score_tiers(fl, gt, tol)
    diag["side_swapped"] = {
        "t1_f1": round(fl_t1, 6),
        "strict_f1": round(match(fl, gt, k_strict, tol)[0], 6),
        "t3_exact_pair_acc": (fl_tiers["t3_score"] or {}).get("exact_pair_acc"),
        "note": "score this answer would get with near<->far exchanged; if it is "
                "much higher than the reward the model read the rallies and "
                "mirrored the court",
    }

    sc, sc_n = self_consistency(ans)
    diag["self_consistent_pairs"] = sc
    diag["self_consistent_of"] = sc_n

    zs = [r["zone"] for r in ans]
    diag["answer_rows_missing_zone"] = sum(1 for z in zs if z is None)
    diag["answer_zone_counts"] = dict(Counter(z for z in zs if z))
    diag["answer_side_counts"] = dict(Counter(r["scoring_side"] for r in ans
                                              if r["scoring_side"]))
    diag["answer_rows_missing_side"] = sum(1 for r in ans if not r["scoring_side"])
    diag["answer_rows_missing_impact"] = sum(1 for r in ans if r["impact"] is None)
    diag["answer_rows_missing_score"] = sum(1 for r in ans
                                            if r["score_after"] is None)
    diag["answer_rows_score_before_only"] = (count_score_before_only(raw)
                                             if raw is not None else 0)
    if alpha_note:
        diag["zone_alphabet_mismatch"] = alpha_note

    result = {
        "reward": round(reward, 6),
        "primary": primary,
        "tol_s": tol,
        "has_strategy": ws,
        "strict": {"f1": round(s_f1, 6), "precision": round(s_p, 6),
                   "recall": round(s_r, 6), "tp": s_tp, "fp": s_fp, "fn": s_fn},
        "tiers": {
            "t1_localization": {"f1": round(t1_f1, 6), "precision": round(t1_p, 6),
                                "recall": round(t1_r, 6),
                                "tp": t1_tp, "fp": t1_fp, "fn": t1_fn},
            "t2_impact": tiers["t2_impact"],
            "t3_score": tiers["t3_score"],
            "localized_points": tiers["localized_points"],
            "total_points": tiers["total_points"],
        },
        "detail": {"answer_rows": len(ans), "gt_rows": len(gt), **diag},
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(json.dumps(result, ensure_ascii=False))
    return result["reward"]


if __name__ == "__main__":
    main()
