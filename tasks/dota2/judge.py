#!/usr/bin/env python3
"""dota_judge.py — deterministic verifier for the three-tier Dota 2 GT. Pure code,
no VLM/LLM judge. COUPLED like  / the board games: the tiers are inlined onto ONE
unified event list and scored by a SINGLE maximum 1:1 full-tuple F1.

COUPLED event tuple (per T1 key event), matched 1:1 on (type, |Δvts|<=tol):
    entity attrs (T1 localization, by type)
      tower      -> side, tier, lane
      barracks   -> side, rax_type, lane
      ancient    -> side
      roshan     -> killer_side
      firstblood -> killer_side
      teamfight  -> winner_side, deaths
    + minute_gain  (T2) = who GAINED more gold DURING the event's own minute [m, m+1]
                          = leader(gold_adv[m+1]-gold_adv[m])  {radiant,dire,even} (flow)
    + leader_after (T3) = who is cumulatively AHEAD at the end of that minute (m+1),
                          i.e. AFTER this event landed = leader(gold_adv[m+1]) (stock)
Both anchored to the whole minute AFTER the event (m+1) so they include this event's
own gold impact plus all preceding history. Two PER-EVENT economy fields off the same
gold curve, a (minute_gain, leader_after) pair; there is NO global winner/case_type
in the tuple. T3 is named leader_after as in the board games / doudizhu / sc2 /
gfootball; `leader_before` is NOT accepted.
String fields compare case- and whitespace-insensitively ("Radiant" == "radiant"),
integer fields numerically (3 == 3.0 == "3"). A GT attr of None is a wildcard.
Any single wrong field — incl. minute_gain (T2) or
leader_after (T3) — voids the event, exactly like 's all-or-nothing full-tuple F1.

  reward = the single unified F1.
The decomposition (entity-only T1 F1, event-level T2 minute_gain / T3 leader_after
macro_acc, dense per-minute leader, and the match-global winner/case_type correctness)
is kept in `detail` for TRANSPARENCY ONLY and does NOT feed the reward. Difficulty /
visibility is diagnosed by dota_judge_relax.py OFFLINE, never here.

  --oracle  feed the GT back as the prediction (unified F1 = 1.0, every decomposition
            tier = 1.0; an empty answer = 0.0).
  --perturb corrupt the oracle (flip every side + minute_gain + leader_after) to
            confirm the unified reward collapses (discrimination check).

Usage:
    python dota_judge.py --gt dota2/gt/final/game_<id>_gt.json --pred pred.json
    python dota_judge.py --gt <gt> --oracle [--perturb] [--tol 3.0]
"""
import argparse, copy, heapq, json, sys
from collections import defaultdict

# entity attributes that must match for a T1 true positive, per event type
ATTRS = {
    "tower": ("side", "tier", "lane"),
    "barracks": ("side", "rax_type", "lane"),
    "ancient": ("side",),
    "roshan": ("killer_side",),
    "firstblood": ("killer_side",),
    "teamfight": ("winner_side", "deaths"),
}
# the two inlined per-event economy fields that couple T2 + T3 onto every event
COUPLE_KEYS = ("minute_gain", "leader_after")
LEADERS = ("radiant", "dire", "even")
FLIP = {"radiant": "dire", "dire": "radiant", "even": "even"}
CASE_FLIP = {"碾压": "翻转", "翻转": "碾压", "焦灼": "碾压"}


def _num(x):
    """float for a number or numeric string; None otherwise (bool, NaN, inf too)."""
    if isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if abs(v) < 1e18 else None


def _lc(x):
    return x.strip().lower() if isinstance(x, str) else x


def _same(gv, pv):
    """Strings case/whitespace-insensitively, numbers numerically (3 == 3.0 == "3")."""
    if isinstance(gv, str):
        return isinstance(pv, str) and _lc(pv) == _lc(gv)
    if isinstance(gv, (int, float)) and not isinstance(gv, bool):
        v = _num(pv)
        return v is not None and v == gv
    return pv == gv


def attrs_match(gt, pred, keys):
    """GT value None => wildcard (keeps the oracle at 1.0)."""
    for k in keys:
        gv = gt.get(k)
        if gv is None:
            continue
        if not _same(gv, pred.get(k)):
            return False
    return True


def attrs_match_coupled(gt, pred):
    """The full COUPLED predicate: entity attrs (by type) AND the two inlined
    per-event economy fields (minute_gain = T2, leader_after = T3) must all match.
    Any single wrong field voids the event, so the tiers are answered together in one
    tuple. GT None acts as a wildcard."""
    return attrs_match(gt, pred, ATTRS.get(gt["type"], ())) and \
        attrs_match(gt, pred, COUPLE_KEYS)


def _differ(gt, pred):
    """How many of the GT event's labelled fields the prediction gets wrong (time
    aside): the tie-break between GT events at the same distance."""
    return sum(1 for k, v in gt.items()
               if k not in ("video_ts", "time_s") and v is not None
               and not _same(v, pred.get(k)))


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


def match_pairs(gt_events, pred_events, tol, pred_fn):
    """Maximum 1:1 time-matched pairing: a prediction may pair with a GT event of the
    same type within `tol` that satisfies `pred_fn(gt, pred)`, and the judge takes the
    LARGEST set of disjoint pairs, so row order cannot move the score -- and, of the
    matchings that large, the one with the least total |dt| (then the fewest
    disagreeing fields, for an exact time tie), so the T2/T3 macros read each
    prediction against its nearest GT partner. Predictions are put in (time, content)
    order first, so the pairs are deterministic too. Returns [(gt, pred)] so callers
    can derive F1 (from the pair count) or read matched fields (event-level T2/T3
    macros)."""
    rows = sorted((p for p in pred_events if _num(p.get("video_ts")) is not None),
                  key=lambda p: (_num(p["video_ts"]),
                                 json.dumps(p, sort_keys=True, default=str)))
    big = 1 + sum(len(g) for g in gt_events)   # > any matching's total field disagreement
    cand = []
    for pe in rows:
        pt, pty = _num(pe["video_ts"]), _lc(pe.get("type"))
        cand.append([(round(abs(pt - g["video_ts"]) * 1e6) * big + _differ(g, pe), gi)
                     for gi, g in enumerate(gt_events)
                     if g["type"] == pty and abs(pt - g["video_ts"]) <= tol
                     and pred_fn(g, pe)])
    owner = _assign(cand, len(gt_events))
    return [(gt_events[gi], rows[k]) for gi, k in enumerate(owner) if k >= 0]


def _prf(tp, n_pred, n_gt):
    prec = tp / n_pred if n_pred else 1.0
    rec = tp / n_gt if n_gt else 1.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return {"tp": tp, "n_pred": n_pred, "n_gt": n_gt,
            "precision": round(prec, 4), "recall": round(rec, 4), "f1": round(f1, 4)}


def score_unified(gt_events, pred_events, tol, pred_fn=attrs_match_coupled):
    """Single maximum 1:1 F1 over the full coupled tuple. THIS is the reward."""
    pairs = match_pairs(gt_events, pred_events, tol, pred_fn)
    return _prf(len(pairs), len(pred_events), len(gt_events))


def score_t1(gt_events, pred_events, tol):
    """Transparency: entity-only localization F1 (drops the inlined T2/T3 fields)."""
    pairs = match_pairs(gt_events, pred_events, tol,
                        lambda g, p: attrs_match(g, p, ATTRS.get(g["type"], ())))
    return _prf(len(pairs), len(pred_events), len(gt_events))


def _event_field_macro(gt_events, pred_events, tol, field):
    """Transparency: pair events by entity localization, then macro accuracy (mean
    per-class recall) of the inlined per-event `field` (minute_gain or leader_after)
    over matched pairs — defeats the majority-class shortcut."""
    pairs = match_pairs(gt_events, pred_events, tol,
                        lambda g, p: attrs_match(g, p, ATTRS.get(g["type"], ())))
    tot = defaultdict(int)
    hit = defaultdict(int)
    for g, p in pairs:
        gv = g.get(field)
        tot[gv] += 1
        hit[gv] += int(_same(gv, p.get(field)))
    macro = sum(hit[c] / tot[c] for c in tot) / len(tot) if tot else 0.0
    return {"macro_acc": round(macro, 4), "matched_events": len(pairs),
            "per_class": {c: f"{hit[c]}/{tot[c]}" for c in tot}}


def score_t2_dense(gt_track, pred_track, winner):
    """Transparency: dense per-minute momentary leader. macro_acc + informative_acc
    (minutes where the momentary leader != eventual winner) defeat the majority-class
    shortcut of always guessing the eventual winner."""
    pred_by_min = {int(_num(p.get("minute"))): _lc(p.get("leader"))
                   for p in pred_track
                   if isinstance(p, dict) and _num(p.get("minute")) is not None}
    n = correct = 0
    per_tot = defaultdict(int)
    per_hit = defaultdict(int)
    inf_tot = inf_hit = 0
    for g in gt_track:
        n += 1
        gl = g["leader"]
        pl = pred_by_min.get(g["minute"])
        ok = (pl == gl)
        correct += ok
        per_tot[gl] += 1
        per_hit[gl] += ok
        if gl != winner:
            inf_tot += 1
            inf_hit += ok
    macro = sum(per_hit[c] / per_tot[c] for c in per_tot) / len(per_tot) if per_tot else 0.0
    return {"acc": round(correct / n, 4) if n else 1.0,
            "macro_acc": round(macro, 4),
            "informative_acc": round(inf_hit / inf_tot, 4) if inf_tot else None,
            "informative_minutes": inf_tot,
            "per_class": {c: f"{per_hit[c]}/{per_tot[c]}" for c in per_tot}}


def score_outcome_global(gt_out, pred_out):
    """Transparency (NOT coupled, NOT in reward): the match-global winner + case_type.
    Reported so we still see whether the model called the game, but it is a global
    label, not a per-event tuple field — mirrors  keeping macro outcome out of the
    per-event F1."""
    w = int(_same(gt_out["winner"], pred_out.get("winner")))
    gc = gt_out["case_type"]["type_zh"]
    ct = pred_out.get("case_type")
    pc = ct.get("type_zh") if isinstance(ct, dict) else None
    return {"winner_correct": w, "case_type_correct": int(pc == gc),
            "gt_winner": gt_out["winner"], "gt_case": gc}


def evaluate(pred, gt, tol):
    """COUPLED (-style) reward: a single maximum 1:1 F1 over the full per-event
    tuple (entity attrs + minute_gain + leader_after). The decomposition is kept in
    `detail` for transparency only and does NOT feed the reward."""
    if not isinstance(pred, dict):
        pred = {}
    ev = pred.get("events")
    ans = [e for e in ev if isinstance(e, dict)] if isinstance(ev, list) else []
    unified = score_unified(gt["events"], ans, tol)

    # --- transparency-only decomposition (not part of reward) ---
    t1 = score_t1(gt["events"], ans, tol)
    t2_gain = _event_field_macro(gt["events"], ans, tol, "minute_gain")
    t3_lead = _event_field_macro(gt["events"], ans, tol, "leader_after")
    t2_dense = None
    if isinstance(pred.get("t2_leader_track"), list) and pred["t2_leader_track"]:
        t2_dense = score_t2_dense(gt["t2_leader_track"], pred["t2_leader_track"],
                                  gt["winner"])
    outcome = None
    if isinstance(pred.get("t3_outcome"), dict) and pred["t3_outcome"]:
        outcome = score_outcome_global(gt["t3_outcome"], pred["t3_outcome"])

    # format diagnostic: `leader_before` is NOT accepted as a fallback for T3 (an
    # answer using it scores its leader_after as missing), so surface how many answer
    # rows carry it to flag a format error.
    stale = sum(1 for a in ans if "leader_after" not in a and "leader_before" in a)

    return {"reward": unified["f1"], "unified": unified, "tol_s": tol,
            "detail": {"gt_rows": len(gt["events"]),
                       "t1_localization": t1,
                       "t2_minute_gain_event": t2_gain,
                       "t3_leader_after_event": t3_lead,
                       "t2_leader_dense": t2_dense,
                       "outcome_global": outcome,
                       "answer_rows_using_leader_before": stale}}


def perturb(pred):
    """Corrupt the oracle: flip every entity side AND the two inlined economy fields
    (minute_gain, leader_after), plus the dense leader track and (if present) the
    global t3_outcome — to confirm the coupled reward collapses."""
    p = copy.deepcopy(pred)
    for e in p["events"]:
        for k in ("side", "killer_side", "winner_side", "minute_gain", "leader_after"):
            if e.get(k) in FLIP:
                e[k] = FLIP[e[k]]
    for r in p.get("t2_leader_track", []):
        r["leader"] = FLIP.get(r.get("leader"), r.get("leader"))
    if p.get("t3_outcome"):
        p["t3_outcome"]["winner"] = FLIP.get(p["t3_outcome"]["winner"])
        ct = p["t3_outcome"]["case_type"]
        ct["type_zh"] = CASE_FLIP.get(ct["type_zh"], ct["type_zh"])
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gt", required=True)
    # --pred is the standalone name (run_dota_batch etc.); --answer is the shared driver
    # CLI (eval_registry.run_judge passes --answer/--gt/--out/--tol) — accept either.
    ap.add_argument("--pred")
    ap.add_argument("--answer")
    ap.add_argument("--out", help="write reward.json here (shared-driver CLI)")
    ap.add_argument("--oracle", action="store_true")
    ap.add_argument("--perturb", action="store_true")
    ap.add_argument("--tol", type=float, default=3.0, help="video-second tolerance for T1")
    args = ap.parse_args()

    gt = json.load(open(args.gt, encoding="utf-8"))

    if args.oracle:
        oracle_pred = {"events": gt["events"],
                       "t2_leader_track": gt["t2_leader_track"],
                       "t3_outcome": gt["t3_outcome"]}
        pred = perturb(oracle_pred) if args.perturb else oracle_pred
        res = evaluate(pred, gt, args.tol)
        det = res["detail"]
        if args.perturb:
            ok = res["reward"] < 0.5   # coupled tuple should collapse hard
            print(f"PERTURB unified_f1={res['reward']} "
                  f"(t1_f1={det['t1_localization']['f1']} "
                  f"t2gain_macro={det['t2_minute_gain_event']['macro_acc']} "
                  f"t3lead_macro={det['t3_leader_after_event']['macro_acc']} "
                  f"dense_macro={det['t2_leader_dense']['macro_acc']})  "
                  f"-> {'PASS' if ok else 'FAIL'}")
            raise SystemExit(0 if ok else 1)
        empty = evaluate({"events": []}, gt, args.tol)
        ok = (res["reward"] == 1.0 and res["unified"]["f1"] == 1.0
              and det["t1_localization"]["f1"] == 1.0
              and det["t2_minute_gain_event"]["macro_acc"] == 1.0
              and det["t3_leader_after_event"]["macro_acc"] == 1.0
              and det["t2_leader_dense"]["macro_acc"] == 1.0
              and det["outcome_global"]["winner_correct"] == 1
              and det["outcome_global"]["case_type_correct"] == 1
              and empty["reward"] == 0.0)
        print(f"ORACLE unified_f1={res['reward']} "
              f"(decomp t1_f1={det['t1_localization']['f1']} "
              f"t2gain_macro={det['t2_minute_gain_event']['macro_acc']} "
              f"t3lead_macro={det['t3_leader_after_event']['macro_acc']} "
              f"dense_macro={det['t2_leader_dense']['macro_acc']} "
              f"dense_inf={det['t2_leader_dense']['informative_acc']} "
              f"winner_ok={det['outcome_global']['winner_correct']} "
              f"case_ok={det['outcome_global']['case_type_correct']})  "
              f"EMPTY reward={empty['reward']}  -> {'PASS' if ok else 'FAIL'}")
        raise SystemExit(0 if ok else 1)

    pred_path = args.answer or args.pred
    if not pred_path:
        ap.error("one of --answer/--pred is required (or use --oracle)")
    # A malformed answer (BOM, bare list, null, non-dict rows, string times) scores,
    # never crashes: a crash turns the cell into "judge failed" and drops it from the mean.
    try:
        with open(pred_path, encoding="utf-8-sig") as f:
            pred = json.load(f)
    except Exception as e:
        print(f"answer unreadable ({e}) -> 0", file=sys.stderr)
        pred = {}
    if isinstance(pred, list):
        pred = {"events": pred}
    res = evaluate(pred, gt, args.tol)
    res["id"] = gt.get("id")
    if args.out:
        json.dump(res, open(args.out, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return res


if __name__ == "__main__":
    main()
