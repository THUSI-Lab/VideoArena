#!/usr/bin/env python3
"""cs2_judge.py — deterministic verifier for the CS2 three-tier task. Pure code,
no VLM/LLM judge. CS2 analog of tasks/go/judge.py.

Unlike the board games, CS2 needs no engine value function: every label is
objective ground truth read from the demo. The three tiers:

  T1 key-event localization : match answer events to GT on (round_no exact,
       type exact, |video_ts - gt_ts| <= tol) AND the type's entity attribute(s).
       round_no is part of the event's identity: miscounting one round shifts the
       round_no of every later event, and none of those can match (by design --
       counting rounds is part of reading the match):
         kill        -> victim colour (orange=T / blue=CT) + number (1-10 dot)
         bomb_planted-> planter colour
         conclusion  -> winner colour  (bomb_exploded/bomb_defused/elimination/save)
       A wrong number or wrong colour is NOT a match. Maximum 1-1, report P/R/F1.
       type in {kill, bomb_planted, bomb_defused, bomb_exploded, elimination, save}
       (save = 保枪 = time-expiry/target-saved). A GT attr of None is a wildcard.
  T2 current-round winner   : winner in {T, CT} per round. Report raw acc AND
       macro_acc (mean per-class recall over the classes PRESENT) — the
       shortcut-resistant headline (always-'T' collapses to <=0.5, oracle=1.0).
  T3 running match score    : REAL scoreboard by persistent team (A,B) across the
       halftime side-swap. Report exact-match frac over rounds AND final exact.

COUPLED answer format ( / board-game style — ONE unified event list; T2 and T3
are inlined onto every event and are round-constant, so all events in a round carry
the SAME round_winner + score_after):
  {"events":[{"round_no":1,"type":"kill","video_ts":3.4,"side":"CT","number":7,
                 "round_winner":"T","score_after":{"A":1,"B":0}}, ...]}

Primary reward = a SINGLE maximum 1:1 F1 over the full tuple
  (round_no, type, |Δt|<=tol, entity attrs, round_winner, score_after).
Any single wrong field — incl. the inlined round_winner (T2) or score_after (T3) —
voids the event, exactly like 's all-or-nothing full-tuple F1. The three-tier
breakdown (entity-only T1 F1, folded per-round T2/T3) is kept in `detail` for
transparency only and does NOT feed the reward.

Oracle self-check: `--oracle` feeds the GT itself as the answer; every tier must
score 1.0. An empty answer must score 0.0.

Usage:
    python cs2_judge.py --answer answer.json --gt gt.json [--tol 3.0] [--out reward.json]
    python cs2_judge.py --gt gt.json --oracle          # self-check (expect all 1.0)
"""
import argparse, heapq, json, sys
from collections import Counter, defaultdict

# colour/side aliases — the radar shows orange (T) vs blue (CT); accept either the
# colour the model reads off the video or the raw side token, EN or ZH.
WIN_ALIAS = {"T": "T", "TERRORIST": "T", "TERRORISTS": "T", "2": "T",
             "CT": "CT", "COUNTER-TERRORIST": "CT", "COUNTER-TERRORISTS": "CT", "3": "CT",
             "ORANGE": "T", "YELLOW": "T", "GOLD": "T", "橙": "T", "橙色": "T", "黄": "T",
             "黄色": "T", "T方": "T", "T队": "T",
             "BLUE": "CT", "蓝": "CT", "蓝色": "CT", "CT方": "CT", "CT队": "CT"}
TYPE_ALIAS = {"KILL": "kill", "DEATH": "kill", "FRAG": "kill",
              "死亡": "kill", "击杀": "kill", "阵亡": "kill",
              "BOMB_PLANTED": "bomb_planted", "PLANT": "bomb_planted",
              "PLANTED": "bomb_planted", "埋包": "bomb_planted", "下包": "bomb_planted",
              "安装炸弹": "bomb_planted", "安包": "bomb_planted",
              "BOMB_DEFUSED": "bomb_defused", "DEFUSE": "bomb_defused",
              "DEFUSED": "bomb_defused", "包拆": "bomb_defused", "拆包": "bomb_defused",
              "拆除": "bomb_defused", "拆弹": "bomb_defused",
              "BOMB_EXPLODED": "bomb_exploded", "EXPLODE": "bomb_exploded",
              "EXPLODED": "bomb_exploded", "DETONATE": "bomb_exploded",
              "包炸": "bomb_exploded", "爆炸": "bomb_exploded", "炸包": "bomb_exploded",
              "ELIMINATION": "elimination", "ELIM": "elimination", "WIPE": "elimination",
              "ACE": "elimination", "歼灭": "elimination", "团灭": "elimination",
              "SAVE": "save", "TIME": "save", "TIMEOUT": "save", "TARGET_SAVED": "save",
              "保枪": "save", "超时": "save"}


def nwin(w):
    return WIN_ALIAS.get(str(w).strip().upper(), str(w).strip().upper())


def ntype(t):
    return TYPE_ALIAS.get(str(t).strip().upper(), str(t).strip().lower())


def _color(d, keys):
    """First non-empty colour/side value among `keys`, normalized to T/CT (or None)."""
    for k in keys:
        v = d.get(k)
        if v is not None and str(v).strip() != "":
            return nwin(v)
    return None


def _num(x):
    """float for a number or numeric string; None otherwise (bool, NaN, inf too)."""
    if isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if abs(v) < 1e18 else None


def _round_no(x):
    """Integral round number (1, 1.0 or "1"); None for anything else."""
    v = _num(x)
    return int(v) if v is not None and v == int(v) else None


def _number(d):
    """Victim dot number (1-10) from an answer/GT event: prefer explicit 'number',
    else 'slot'+1 (slots are 0-9)."""
    if d.get("number") is not None:
        try:
            return int(d["number"])
        except (ValueError, TypeError, OverflowError):
            return None
    if d.get("slot") is not None:
        try:
            return int(d["slot"]) + 1
        except (ValueError, TypeError, OverflowError):
            return None
    return None


_COLOR_KEYS = ("side", "color", "colour", "team", "victim_side", "victim_color",
               "victim_colour", "planter_side", "planter_color", "winner_side",
               "winner_color", "winner")


def attr_ok(a, g):
    """Does answer event `a` carry the entity attribute(s) required by GT event `g`?
    A GT attribute of None acts as a wildcard (keeps the oracle at 1.0 when a rare
    parse could not resolve the victim)."""
    ty = ntype(g["type"])
    acol = _color(a, _COLOR_KEYS)
    if ty == "kill":
        gcol = nwin(g["side"]) if g.get("side") else None
        gnum = _number(g)
        anum = _number(a)
        col_ok = gcol is None or acol == gcol
        num_ok = gnum is None or (anum is not None and anum == gnum)
        return col_ok and num_ok
    if ty == "bomb_planted":
        gcol = nwin(g["side"]) if g.get("side") else None
        return gcol is None or acol == gcol
    # conclusion: bomb_exploded / bomb_defused / elimination / save -> winner colour
    gcol = nwin(g["winner_side"]) if g.get("winner_side") else None
    return gcol is None or acol == gcol


def _score_after(d):
    """Running scoreboard (A,B) inlined on an event, as an ordered pair or None."""
    sc = d.get("score_after")
    if isinstance(sc, dict) and sc.get("A") is not None and sc.get("B") is not None:
        try:
            return (int(sc["A"]), int(sc["B"]))
        except (ValueError, TypeError, OverflowError):
            return None
    # tolerate flat keys score_A / score_B
    if d.get("score_A") is not None and d.get("score_B") is not None:
        try:
            return (int(d["score_A"]), int(d["score_B"]))
        except (ValueError, TypeError, OverflowError):
            return None
    return None


def attr_ok_coupled(a, g):
    """-style COUPLED predicate: the entity attribute(s) AND the two inlined
    macro-state fields must all match — round_winner (T2) and score_after (T3).
    Any single wrong field (incl. winner or score) voids the event, so the three
    tiers are answered together inside one event tuple. GT None acts as wildcard."""
    if not attr_ok(a, g):
        return False
    # T2: round_winner (colour), read from `round_winner` ONLY -- falling back to
    # the entity colour (`side`, `winner_side`, ...) credited an answer that never
    # stated a round winner. GT None => wildcard.
    gw = nwin(g["round_winner"]) if g.get("round_winner") else None
    if gw is not None:
        aw = _color(a, ("round_winner",))
        if aw != gw:
            return False
    # T3: score_after (A,B) exact. GT None => wildcard.
    gs = _score_after(g)
    if gs is not None:
        if _score_after(a) != gs:
            return False
    return True


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


def _match(ans, gt, tol, pred):
    """Maximum 1:1 time-matched matching over (round_no, type, |dt|<=tol, pred).
    Takes the LARGEST set of disjoint (answer, GT) pairs, so row order cannot move
    the score; of the matchings that large it takes the least total |dt|, as every
    judge does. Answer rows are put in (time, content) order first, so the pairs are
    deterministic too. Returns the pair count."""
    rows = sorted((a for a in ans if _num(a.get("video_ts")) is not None),
                  key=lambda a: (_num(a["video_ts"]),
                                 json.dumps(a, sort_keys=True, default=str)))
    gts = [(e["round_no"], ntype(e["type"]), float(e["video_ts"]), e) for e in gt]
    cand = []
    for a in rows:
        art, aty = _round_no(a.get("round_no")), ntype(a.get("type"))
        ats = _num(a["video_ts"])
        cand.append([(round(abs(ats - gt_ts) * 1e6), gi)
                     for gi, (grn, gty, gt_ts, e) in enumerate(gts)
                     if grn == art and gty == aty and abs(ats - gt_ts) <= tol + 1e-9
                     and pred(a, e)])
    owner = _assign(cand, len(gts))
    return sum(1 for o in owner if o >= 0)


def _prf(matched, ans, gt):
    p = matched / len(ans) if ans else 0.0
    r = matched / len(gt) if gt else 0.0
    f1 = (2 * p * r / (p + r)) if (p + r) else 0.0
    return {"precision": round(p, 4), "recall": round(r, 4), "f1": round(f1, 4),
            "matched": matched, "n_ans": len(ans), "n_gt": len(gt)}


def score_unified(ans, gt, tol, pred=attr_ok_coupled):
    """Single maximum 1:1 time-matched F1 over the COUPLED event tuple
    (round_no, type, |Δt|<=tol, entity attrs, round_winner, score_after).
    Same matching core as score_t1 but the attribute predicate is injectable."""
    return _prf(_match(ans, gt, tol, pred), ans, gt)


def fold_rounds(events):
    """Collapse the inlined round_winner / score_after off a unified event list into
    per-round T2 / T3 views (the tiers are round-constant). If a round carries
    inconsistent values across its events, take the mode and flag the round."""
    win, score, seen = defaultdict(Counter), defaultdict(Counter), {}
    for e in events:
        rn = _round_no(e.get("round_no"))
        if rn is None:
            continue
        if e.get("round_winner") is not None:
            win[rn][nwin(e["round_winner"])] += 1
        sc = _score_after(e)
        if sc is not None:
            score[rn][sc] += 1
    inconsistent = sorted({rn for rn in win if len(win[rn]) > 1}
                          | {rn for rn in score if len(score[rn]) > 1})
    t2 = [{"round_no": rn, "winner": win[rn].most_common(1)[0][0]}
          for rn in sorted(win)]
    t3 = [{"round_no": rn, "A": score[rn].most_common(1)[0][0][0],
           "B": score[rn].most_common(1)[0][0][1]} for rn in sorted(score)]
    return t2, t3, inconsistent


def score_t1(ans, gt, tol):
    # entity-only: a wrong number / colour is still not a match
    return _prf(_match(ans, gt, tol, attr_ok), ans, gt)


def macro_acc(pairs):
    # pairs: list of (pred, gold); mean per-class recall over classes present in gold
    by = defaultdict(lambda: [0, 0])
    for pred, gold in pairs:
        by[gold][1] += 1
        if pred == gold:
            by[gold][0] += 1
    if not by:
        return 0.0
    return round(sum(c / n for c, n in by.values()) / len(by), 4)


def score_t2(ans, gt):
    gmap = {r["round_no"]: nwin(r["winner"]) for r in gt}
    amap = {a["round_no"]: nwin(a.get("winner")) for a in ans}
    pairs = [(amap.get(rn, "?"), w) for rn, w in gmap.items()]
    acc = round(sum(1 for p, g in pairs if p == g) / len(pairs), 4) if pairs else 0.0
    return {"acc": acc, "macro_acc": macro_acc(pairs), "n_rounds": len(gmap),
            "class_dist": dict(Counter(gmap.values()))}


def score_t3(ans, gt):
    # real team scoreboard (persistent team A vs B across the halftime side-swap)
    gmap = {r["round_no"]: (int(r["A"]), int(r["B"])) for r in gt}
    amap = {a["round_no"]: (int(a.get("A", -1)), int(a.get("B", -1))) for a in ans}
    exact = sum(1 for rn, sc in gmap.items() if amap.get(rn) == sc)
    frac = round(exact / len(gmap), 4) if gmap else 0.0
    final_rn = max(gmap) if gmap else None
    final_ok = amap.get(final_rn) == gmap.get(final_rn) if final_rn else False
    return {"exact_frac": frac, "exact": exact, "n_rounds": len(gmap),
            "final_score_exact": bool(final_ok)}


def evaluate(answer, gt, tol):
    """COUPLED (-style) reward: a single maximum 1:1 F1 over the full event tuple
    (round_no, type, |Δt|<=tol, entity attrs, round_winner, score_after). Any wrong
    field — incl. the inlined round_winner (T2) or score_after (T3) — voids the event.

    The three-tier decomposition is kept in `detail` for transparency only (it does
    NOT feed the reward): T1 = the entity-only F1 (score_t1); T2/T3 are FOLDED
    back out of the answer's inlined fields (fold_rounds) and scored per round."""
    ev = answer.get("events") if isinstance(answer, dict) else None
    ans_events = [e for e in ev if isinstance(e, dict)] if isinstance(ev, list) else []
    unified = score_unified(ans_events, gt["events"], tol)

    # --- transparency-only decomposition (not part of reward) ---
    t1 = score_t1(ans_events, gt["events"], tol)
    # T2/T3 come ONLY from the inlined event fields, so a diagnostic can never
    # report a T2 the scored events did not claim.
    a_t2, a_t3, incons = fold_rounds(ans_events)
    t2 = score_t2(a_t2, gt["t2_round_winners"])
    t3 = score_t3(a_t3, gt["t3_score_track"])

    return {"reward": unified["f1"], "unified": unified, "tol_s": tol,
            "detail": {"t1_localization": t1, "t2_round_winner": t2,
                       "t3_score_track": t3, "inconsistent_rounds": incons}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gt", required=True)
    ap.add_argument("--answer")
    ap.add_argument("--tol", type=float, default=3.0)
    ap.add_argument("--oracle", action="store_true")
    ap.add_argument("--out")
    args = ap.parse_args()
    gt = json.load(open(args.gt, encoding="utf-8"))

    if args.oracle:
        # coupling invariant: within each round every inlined event carries the SAME
        # round_winner + score_after (GT is built that way; assert to catch regressions)
        by_r = defaultdict(set)
        for e in gt["events"]:
            by_r[e["round_no"]].add((e.get("round_winner"),
                                     tuple(sorted((e.get("score_after") or {}).items()))))
        bad = {rn: v for rn, v in by_r.items() if len(v) > 1}
        # oracle answer = GT's own inlined event list (T2/T3 fold out of it)
        oracle_ans = {"events": gt["events"]}
        r_ok = evaluate(oracle_ans, gt, args.tol)
        r_empty = evaluate({}, gt, args.tol)
        det = r_ok["detail"]
        ok = (not bad and r_ok["reward"] == 1.0 and r_ok["unified"]["f1"] == 1.0
              and det["t1_localization"]["f1"] == 1.0
              and det["t2_round_winner"]["macro_acc"] == 1.0
              and det["t3_score_track"]["exact_frac"] == 1.0
              and not det["inconsistent_rounds"] and r_empty["reward"] == 0.0)
        print(f"ORACLE unified_f1={r_ok['reward']} "
              f"(decomp t1_f1={det['t1_localization']['f1']} "
              f"t2_macro={det['t2_round_winner']['macro_acc']} "
              f"t3_exact={det['t3_score_track']['exact_frac']})  "
              f"invariant={'ok' if not bad else f'VIOLATED {bad}'}  "
              f"EMPTY reward={r_empty['reward']}  -> {'PASS' if ok else 'FAIL'}")
        raise SystemExit(0 if ok else 1)

    # A malformed answer (BOM, bare list, null, non-dict rows) scores, never crashes:
    # a crash would turn the cell into "judge failed" and drop it from the mean.
    try:
        with open(args.answer, encoding="utf-8-sig") as f:
            answer = json.load(f)
    except Exception as e:
        print(f"answer unreadable ({e}) -> 0", file=sys.stderr)
        answer = {}
    if isinstance(answer, list):
        answer = {"events": answer}
    res = evaluate(answer, gt, args.tol)
    if args.out:
        json.dump(res, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
