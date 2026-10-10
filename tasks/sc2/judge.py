#!/usr/bin/env python3
"""sc2_tiers_judge.py — family judge for SC2 engagement-tiers (Video-Arena), matching the
eval_registry contract: CLI --answer/--gt/--out/--tol (--primary ignored), writes reward.json
in the `tiers` shape that eval_registry.subscores() reads.

Deterministic, pure code. One ordered list of engagement events; each row a 5-tuple
(game_clock_s, type, entity, fight_result[T2], leader_after[T3]). Headline reward = strict
all-or-nothing F1 (all four keys + |dt|<=tol, maximum 1:1). T1 = (type,entity) localization F1;
T2/T3 = macro-averaged accuracy over the localized events (mean recall over {A,B,equal}).
"""
import argparse, heapq, json


def n(s):
    return str(s if s is not None else "").strip().lower()


def evs(x):
    """The event rows: a list under "events" (or a bare list), dict rows only -- a
    stray non-dict row can never match, so counting it would only dent precision."""
    x = x.get("events", []) if isinstance(x, dict) else x
    return [e for e in x if isinstance(e, dict)] if isinstance(x, list) else []


def t_of(e):
    """Row time in seconds; raises for a missing / non-numeric / NaN / inf time."""
    v = float(e.get("game_clock_s", e.get("t")))
    if not abs(v) < 1e18:
        raise ValueError("non-finite time")
    return v


def _differ(a, g):
    """How many of GT row g's fields pred row a gets wrong (time aside): the tie-break
    between GT rows at the same distance."""
    return sum(1 for k, v in g.items()
               if k not in ("game_clock_s", "t") and n(a.get(k)) != n(v))


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


def match(gt, pred, fields, tol):
    """Maximum 1:1 time-matched matching on `fields` + |dt|<=tol: the LARGEST set of
    disjoint pairs, so row order cannot move the score -- and, of the matchings that
    large, the one with the least total |dt| (then the fewest disagreeing fields, for
    an exact time tie), so T2/T3 read off the pairs compare each row with its nearest
    GT partner. Rows are put in (time, content) order first, so the pairs are
    deterministic too. A row without a usable time cannot match but still counts in
    precision."""
    rows = []
    for a in pred:
        try:
            rows.append((t_of(a), json.dumps(a, sort_keys=True, default=str), a))
        except Exception:
            continue
    rows.sort(key=lambda r: r[:2])
    gkeys = [tuple(n(g.get(f)) for f in fields) for g in gt]
    gts = [t_of(g) for g in gt]
    big = 1 + sum(len(g) for g in gt)       # > any matching's total field disagreement
    cand = []
    for at, _, a in rows:
        ak = tuple(n(a.get(f)) for f in fields)
        cand.append([(round(abs(gts[i] - at) * 1e6) * big + _differ(a, g), i)
                     for i, g in enumerate(gt) if gkeys[i] == ak and abs(gts[i] - at) <= tol])
    owner = _assign(cand, len(gt))
    pairs = [(gt[i], rows[k][2]) for i, k in enumerate(owner) if k >= 0]
    tp = len(pairs)
    p = tp / len(pred) if pred else 0.0
    r = tp / len(gt) if gt else 0.0
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0
    return f1, tp, pairs


def macro(pairs, field, classes=("a", "b", "equal")):
    from collections import Counter
    tot = Counter(); hit = Counter()
    for g, a in pairs:
        c = n(g.get(field))
        if c not in classes:
            continue
        tot[c] += 1
        if n(a.get(field)) == c:
            hit[c] += 1
    recs = [hit[c] / tot[c] for c in classes if tot[c]]
    return sum(recs) / len(recs) if recs else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--answer", required=True)
    ap.add_argument("--gt", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--tol", type=float, default=3.0)
    ap.add_argument("--primary", default=None)  # accepted, ignored (single coupled F1)
    a = ap.parse_args()

    def load(p):
        try:
            return json.load(open(p, encoding="utf-8-sig"))
        except Exception:
            return {}
    gt = evs(load(a.gt)); pred = evs(load(a.answer))
    STRICT = ["type", "entity", "fight_result", "leader_after"]
    strict_f1, tp, _ = match(gt, pred, STRICT, a.tol)
    l0, _, _ = match(gt, pred, [], a.tol)
    t1_f1, _, loc = match(gt, pred, ["type", "entity"], a.tol)
    l2, _, _ = match(gt, pred, ["type", "entity", "fight_result"], a.tol)
    reward = {
        "reward": round(strict_f1, 4),
        "tiers": {
            "t1_localization": {"f1": round(t1_f1, 4)},
            "t2_impact": {"macro_acc": round(macro(loc, "fight_result"), 4)},
            "t3_situation": {"macro_acc": round(macro(loc, "leader_after"), 4)},
        },
        "detail": {
            "matched_strict": tp, "ans_rows": len(pred), "gt_rows": len(gt), "tol_s": a.tol,
            "relax_f1": {"L0_time": round(l0, 4), "L1_type_entity": round(t1_f1, 4),
                          "L2_fight_result": round(l2, 4), "L3_strict": round(strict_f1, 4)},
            "n_localized": len(loc),
        },
    }
    json.dump(reward, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(json.dumps({"reward": reward["reward"], **reward["tiers"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
