#!/usr/bin/env python3
"""
Deterministic verifier for the Video-Arena GFootball (soccer) THREE-TIER task.
Pure code — no VLM/LLM judge. Shares the time-matched F1 + macro_acc design with
the Go/Chess/Gomoku scorer (tasks/go/judge.py).

V(t) mechanism A: V = left_goals - right_goals, read directly from the engine.
No superhuman evaluator. Every label is an objective function of the score
sequence the engine produced.

Scoring = maximum 1:1 time-matched F1 (SC2/Go style): predicted rows are
matched to GT rows with the SAME key and |Δt| ≤ TOL (3s), taking the largest such
matching. reward = F1. Order-independent; one wrong/missing row costs one event.

THREE TIERS over the same video (paper's (t, c, a) -> ΔV -> V ladder):
    T1  key event (t, c, a)  -> game_clock_s, scoring_side in {L,R}, scorer_name
    T2  local impact  (ΔV)   -> impact in {extended, taken, levelled, narrowed}
    T3  cumulative state (V) -> score_after as the pair (L_after, R_after)

T3 IS THE SCORE **AFTER** THE GOAL. The unified Video-Arena semantics are
T1 = the event, T2 = ΔV, T3 = V **after** that event, identical in every game.
`score_before` is NOT accepted as an alias: it is a different pair of integers
(off by one goal for the scoring side), so silently aliasing it would hand full
credit to an answer that got the tier wrong. An answer that supplies only
`score_before` is instead COUNTED in detail.answer_rows_score_before_only, so
it shows up as a format failure rather than as inexplicable zeros.

With V = a score difference, the running score is a deterministic function of a
COMPLETE T1 (score_after = the running count of prior scoring_side, +1 for this
goal). So gfootball's T3 measures whether the model enumerated every goal, not an
independent situation judgement -- a real and separately-failing skill: one
missed goal shifts every later pair (selfcheck check 4). It is independent in
doudizhu/Go, where V is a win probability.

WHY THESE LABELS:
  * T1's actor is the player NAME the engine renders over the ball carrier
    (player.cpp:403), i.e. something a viewer can actually see, not an engine
    index.
  * T2 depends on the MAGNITUDE of the deficit (levelled vs narrowed) rather than
    only its sign, so it is not an adjacent sample of the same step function as
    T3; T3 is the exact running score, which has no majority class at all.

T2 chance level = 0.25 macro_acc (4 balanced classes).
T3 has ~n distinct pairs in an n-goal match, so the modal-pair baseline is ~1/n;
the judge computes and reports it so the absence of a shortcut is checkable, not
merely asserted.

Answer format (JSON written by the agent):
    {"events": [
        {"type":"goal","idx":1,"game_clock_s":128.4,"scoring_side":"L",
         "scorer_name":"Turing","impact":"taken","score_after":[1,0]},
        ...]}

Scores reported:
  strict     — F1 with the FULL key (scoring_side, scorer_name, impact,
               score_after) + |Δt|≤3s. PRIMARY reward.
  tiers.t1_localization — F1 with key (scoring_side, scorer_name) + |Δt|≤3s.
  tiers.t2_impact  — acc + macro_acc over the 4 impact classes, paired via the
               T1 time-match. macro_acc is the headline (majority-proof).
  tiers.t3_score   — exact-pair accuracy (headline), per-side accuracy (partial
               credit: one side right still counts), per-side MAE, and the
               modal-pair baseline.

Usage:
    python judge.py --answer answer.json --gt gt.json [--out reward.json]
                    [--tol 3.0] [--primary auto|strict|t1]
"""
import argparse
import heapq
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict

TOL = 3.0  # seconds; event-timing standard (same as Go/SC2/)

SIDE_ALIAS = {"L": "L", "LEFT": "L", "左": "L", "0": "L", "HOME": "L",
              "R": "R", "RIGHT": "R", "右": "R", "1": "R", "AWAY": "R"}

IMPACT_CLASSES = ("extended", "taken", "levelled", "narrowed")
# Accepted spellings for T2. Kept deliberately tight: these are the four labels
# the task statement gives the model, plus the obvious orthographic variants and
# their Chinese glosses.
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

# The engine's 22 hardcoded last names (teamdata.cpp:277-287 + playerdata.cpp),
# used ONLY to report whether a predicted name is even on the roster. Membership
# is diagnostic; the key comparison is plain normalized equality, so a model that
# invents a plausible name simply fails to match.
ROSTER_L = ["Lovelace", "Turing", "Johnson", "da Vinci", "Newton", "Blackwell",
            "Borg", "Euler", "Pythagoras", "Curie", "Nixon Sutton"]
ROSTER_R = ["Meitner", "Einstein", "Vaughaun", "Archimedinho", "Banach",
            "Banneker", "Goodall", "Copernicus", "Feynman", "Franklin",
            "Roy Mouton"]


def norm_side(x):
    if x is None:
        return None
    s = str(x).strip().upper()
    if s in ("EQUAL", "EQ", "E", "=", "EVEN", "DRAW", "TIE", "平", "均", "均势"):
        return "equal"
    return SIDE_ALIAS.get(s, s.lower())


def norm_name(x):
    """Case-folded, accent-free, punctuation- and space-free.
    'da Vinci' -> 'davinci'; 'DA-VINCI' -> 'davinci'; 'Roy Mouton' ->
    'roymouton'. The engine renders only last names, so no first-name handling."""
    if x is None:
        return None
    s = str(x)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^0-9A-Za-z]+", "", s).casefold()
    return s or None


ROSTER_NORM = {norm_name(n) for n in ROSTER_L + ROSTER_R}


def norm_impact(x):
    if x is None:
        return None
    s = re.sub(r"[^0-9A-Za-z一-鿿]+", "", str(x)).casefold()
    return IMPACT_ALIAS.get(s)


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
    seq = obj.get("events", obj) if isinstance(obj, dict) else obj
    if not isinstance(seq, list):
        return []
    rows = []
    for r in seq:
        if not isinstance(r, dict):
            continue
        rows.append({
            "type": "goal",
            "game_clock_s": norm_time(r),
            "scoring_side": norm_side(r.get("scoring_side", r.get("side"))),
            "scorer_name": norm_name(r.get("scorer_name", r.get("scorer",
                                     r.get("player_name")))),
            "impact": norm_impact(r.get("impact", r.get("scorer_impact"))),
            # `score`/`scoreline` alias score_after: a scoreline quoted for a goal
            # is the scoreline that goal produced. `score_before` is deliberately
            # NOT in this chain -- see the module docstring.
            "score_after": norm_pair(r.get("score_after",
                                           r.get("score", r.get("scoreline")))),
        })
    return rows


def count_score_before_only(obj):
    """Rows that carry `score_before` but no usable score_after. Diagnostic only.

    Exists so an answer giving the PRE-goal pair is reported as a format mismatch
    instead of scoring 0 on T3 for no visible reason -- the same purpose as
    answer_names_offroster."""
    seq = obj.get("events", obj) if isinstance(obj, dict) else obj
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


def gt_has_strategy(gt):
    return any(r["impact"] is not None or r["score_after"] is not None
               for r in gt)


def k_strict(r):
    return (r["scoring_side"], r["scorer_name"], r["impact"], r["score_after"])


def k_t1(r):
    return (r["scoring_side"], r["scorer_name"])


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
    scores 1/(number of GT classes), not the majority frequency.
    pairs = [(gt_label, pred_label_or_None)]."""
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
    """T2/T3 over GT goal rows, paired via the T1 (scoring_side, scorer_name)
    time-match. A GT goal not localized within ±tol counts as wrong for its
    class — the tiers are conditional on having found the event, which is the
    point of a nested ladder."""
    _, _, _, _, _, _, t1_pairs = match(pred, gt, k_t1, tol)
    pred_for_gt = {gi: a for gi, a in t1_pairs}
    total = len(gt)
    localized = sum(1 for i in range(total) if i in pred_for_gt)
    has_t2 = any(r["impact"] is not None for r in gt)
    has_t3 = any(r["score_after"] is not None for r in gt)

    t2_pairs = []
    t2_ok = 0
    t3_exact = 0
    side_hits = 0
    side_total = 0
    abs_err_l, abs_err_r = [], []
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
                abs_err_l.append(abs(pp[0] - g["score_after"][0]))
                abs_err_r.append(abs(pp[1] - g["score_after"][1]))

    out = {"localized_goals": localized, "total_goals": total,
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
            "mae_left": round(sum(abs_err_l) / len(abs_err_l), 4) if abs_err_l else None,
            "mae_right": round(sum(abs_err_r) / len(abs_err_r), 4) if abs_err_r else None,
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
        gt = as_rows(json.load(f))

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
        f = match(ans, gt, k_strict, t)[0]
        diag["strict_f1_tol%ds" % int(t)] = round(f, 6)
    # a predicted name that is not on the engine roster can never match; report it
    # so a systematic name-reading failure is visible instead of silent zeros.
    named = [r["scorer_name"] for r in ans if r["scorer_name"]]
    diag["answer_names_offroster"] = sorted(
        {n for n in named if n not in ROSTER_NORM})
    diag["answer_rows_missing_name"] = sum(1 for r in ans if not r["scorer_name"])
    diag["answer_rows_missing_impact"] = sum(1 for r in ans if r["impact"] is None)
    diag["answer_rows_missing_score"] = sum(1 for r in ans
                                            if r["score_after"] is None)
    # format guard: T3 is score_after, not score_before.
    diag["answer_rows_score_before_only"] = (count_score_before_only(raw)
                                             if raw is not None else 0)

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
            "localized_goals": tiers["localized_goals"],
            "total_goals": tiers["total_goals"],
        },
        "detail": {"answer_rows": len(ans), "gt_rows": len(gt), **diag},
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(json.dumps(result, ensure_ascii=False))
    return result["reward"]


if __name__ == "__main__":
    main()
