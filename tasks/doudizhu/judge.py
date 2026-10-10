#!/usr/bin/env python3
"""
Deterministic verifier for the Video-Arena Dou Dizhu THREE-TIER task.
Pure code - no VLM/LLM judge. Same time-matched F1 + macro_acc design as the
Go/Chess/Gomoku/GFootball scorers.

V(t) mechanism B: V = landlord win probability under PIMC - deals are sampled
uniformly from those consistent with the LANDLORD's information set (its own hand
and the bottom three face up, every play so far, both farmer card counts) and
each is played out by the released DouZero-WP nets. The two farmer hands are face
DOWN in the video and never revealed, so V is a function of exactly what is on
screen, while being readable from no single frame.

Scoring = maximum 1:1 time-matched F1: predicted rows are matched to GT rows
with the SAME key and |Δt| ≤ TOL (3.0s), taking the largest such matching.
reward = strict F1. Order-independent; one wrong/missing row costs one event.

A GT row may carry null on T2 and/or T3: the oracle's own estimate was within
2 sigma of that band's boundary, so it abstained rather than ship a coin flip.
Null tiers are excluded from that tier's denominator and from the strict
agreement test. The model is not told which rows those are and must answer all of
them, so this removes undecidable labels without making the task easier.

Three tiers over the same video. Same ladder as every other Video-Arena game --
T1 is the event, T2 is the CHANGE in V the event caused, T3 is the resulting
overall situation:
    T1  event localization    -> (player, move_type, num_cards)      per big play
    T2  event impact (dV)     -> impact in {landlord, farmers, neutral}
    T3  situation AFTER (V)   -> leader_after in {landlord, farmers, equal}

T3 is the situation AFTER the event: the state the T2 deltas accumulate into.
Because V is continuous here (|dV| spans 0.0005..0.895) the banded T3 is not a
deterministic function of the banded T2. A row emitting `leader_before` (the
situation before the play, a different quantity) is NOT accepted as T3; it is
counted in detail.answer_rows_using_leader_before so a wrong answer format shows
up as a format error rather than as a model that cannot read the position.

Answer format (JSON written by the agent):
    {"events": [
        {"idx":1,"game_clock_s":88.0,"player":"landlord","move_type":"straight",
         "num_cards":5,"impact":"landlord","leader_after":"landlord"}, ...]}

Usage:
    python judge.py --answer answer.json --gt gt.json [--out reward.json]
                    [--tol 3.0] [--primary auto|strict|t1]
"""
import argparse, heapq, json, sys
from collections import defaultdict

# seconds. 3.0 == half of the 6s move window, so ANY timestamp inside the window
# in which the move is actually on screen matches, and the tolerance is the same
# +/-3s used by every other game. It still cannot span two MATCHABLE moves: a
# match also requires the identical T1 key (player, move_type, num_cards), and
# across the released 20 cases the nearest pair of consecutive events sharing that
# key is 18.0s apart (6x TOL). Consecutive events can be 6.0s apart, but never
# with the same key.
TOL = 3.0

PLAYER_ALIAS = {
    "LANDLORD": "landlord", "地主": "landlord", "L": "landlord",
    "FARMER_LEFT": "farmer_left", "FARMER-L": "farmer_left",
    "FARMERL": "farmer_left", "LEFT": "farmer_left", "左农民": "farmer_left",
    "FARMER_RIGHT": "farmer_right", "FARMER-R": "farmer_right",
    "FARMERR": "farmer_right", "RIGHT": "farmer_right", "右农民": "farmer_right",
}

SIDE_ALIAS = {
    "LANDLORD": "landlord", "地主": "landlord",
    "FARMERS": "farmers", "FARMER": "farmers", "农民": "farmers",
    "EQUAL": "equal", "EQ": "equal", "EVEN": "equal", "TIE": "equal",
    "均势": "equal", "平": "equal",
    "NEUTRAL": "neutral", "NONE": "neutral", "无": "neutral", "中性": "neutral",
}

TYPE_ALIAS = {
    "BOMB": "bomb", "炸弹": "bomb",
    "ROCKET": "rocket", "KING_BOMB": "rocket", "KINGBOMB": "rocket", "王炸": "rocket",
    "STRAIGHT": "straight", "SERIAL_SINGLE": "straight", "CHAIN": "straight",
    "SINGLE_CHAIN": "straight", "顺子": "straight",
    "PAIR_CHAIN": "pair_chain", "SERIAL_PAIR": "pair_chain",
    "DOUBLE_CHAIN": "pair_chain", "连对": "pair_chain",
    "TRIO_CHAIN": "trio_chain", "SERIAL_TRIPLE": "trio_chain", "飞机": "trio_chain",
    "PLANE_SINGLE": "plane_single", "SERIAL_3_1": "plane_single",
    "PLANE": "plane_single", "飞机带单": "plane_single",
    "PLANE_PAIR": "plane_pair", "SERIAL_3_2": "plane_pair", "飞机带对": "plane_pair",
    "QUAD_TWO": "quad_two", "FOUR_TWO": "quad_two", "四带二": "quad_two",
    "QUAD_TWO_PAIRS": "quad_two_pairs", "FOUR_TWO_PAIRS": "quad_two_pairs",
    "四带两对": "quad_two_pairs",
}


def _norm(x, table):
    if x is None:
        return None
    s = str(x).strip()
    if not s:
        return None
    u = s.upper().replace(" ", "").replace("-", "_")
    if u in table:
        return table[u]
    if s in table:
        return table[s]
    return s.strip().lower()


def norm_int(x):
    try:
        return int(x)
    except Exception:
        return x


def norm_time(r):
    for k in ("game_clock_s", "t_video_s", "t_seconds", "t", "time_s"):
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
            "player": _norm(r.get("player", r.get("seat", "")), PLAYER_ALIAS),
            "move_type": _norm(r.get("move_type", r.get("type", "")), TYPE_ALIAS),
            "num_cards": norm_int(r.get("num_cards", r.get("n_cards", None))),
            "game_clock_s": norm_time(r),
            "impact": _norm(r.get("impact", r.get("wr_swing")), SIDE_ALIAS),
            # `leader` is accepted as a synonym; `leader_before` is NOT (it names
            # a different quantity - see the module docstring).
            "leader_after": _norm(r.get("leader_after", r.get("leader")),
                                  SIDE_ALIAS),
            "_used_leader_before": ("leader_after" not in r
                                    and "leader" not in r
                                    and "leader_before" in r),
        })
    return rows


def gt_has_strategy(gt):
    return any(r["leader_after"] is not None for r in gt)


def k_t1(r):
    return (r["player"], r["move_type"], r["num_cards"])


def strict_match(pred, gt, tol):
    """Strict = T1-matched AND agreeing on every tier the GT row actually labels.

    This is equivalent to a single match on the 5-tuple (player, type, n, impact,
    leader) whenever no two GT events fall within TOL of each other with the SAME
    T1 key, which the released corpus guarantees at TOL=3.0s (nearest same-key
    pair 18.0s apart) - but only this form can express ABSTENTION. A GT row whose label sat within 2 sigma of its
    band boundary carries null on that tier, and null in a 5-tuple key would
    demand the model also answer null, i.e. demand it guess which rows the oracle
    found undecidable. Here a null tier is simply not part of the agreement test,
    so the model answers every row and is judged only where an answer is defined.
    """
    _, _, _, _, _, _, t1_pairs = match(pred, gt, k_t1, tol)
    tp = 0
    for gi, a in t1_pairs:
        g = gt[gi]
        if all(a[f] == g[f] for f in ("impact", "leader_after")
               if g[f] is not None):
            tp += 1
    fp = max(0, len(pred) - tp)
    fn = len(gt) - tp
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0
    return f1, p, r, tp, fp, fn


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
    """Mean per-class recall over the GT classes; kills majority-class guessing."""
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
    _, _, _, _, _, _, t1_pairs = match(pred, gt, k_t1, tol)
    pred_for_gt = {gi: a for gi, a in t1_pairs}
    total = len(gt)
    localized = sum(1 for i in range(total) if i in pred_for_gt)
    has_t2 = any(r["impact"] is not None for r in gt)
    has_t3 = any(r["leader_after"] is not None for r in gt)
    t2_pairs, t3_pairs = [], []
    t2_ok = t3_ok = 0
    # A tier's denominator is the rows GT actually LABELS on that tier. Rows the
    # oracle abstained on (null) are skipped here rather than counted wrong: they
    # were undecidable at 2 sigma, so scoring them would add noise, and counting
    # them as free credit would reward the model for the oracle's uncertainty.
    n_t2 = sum(1 for r in gt if r["impact"] is not None)
    n_t3 = sum(1 for r in gt if r["leader_after"] is not None)
    loc_t2 = loc_t3 = 0
    for i, g in enumerate(gt):
        a = pred_for_gt.get(i)
        if has_t2 and g["impact"] is not None:
            pim = a["impact"] if a else None
            t2_pairs.append((g["impact"], pim))
            loc_t2 += 1 if a else 0
            if pim is not None and pim == g["impact"]:
                t2_ok += 1
        if has_t3 and g["leader_after"] is not None:
            plb = a["leader_after"] if a else None
            t3_pairs.append((g["leader_after"], plb))
            loc_t3 += 1 if a else 0
            if plb is not None and plb == g["leader_after"]:
                t3_ok += 1

    def blk(ok, has, pairs, nlab, nloc):
        if not has:
            return None
        macro, per = _macro_acc(pairs)
        return {"correct": ok, "of_localized": nloc, "of_all": nlab,
                "labelled_rows": nlab, "abstained_rows": total - nlab,
                "acc_localized": round(ok / nloc, 6) if nloc else 0.0,
                "acc_all": round(ok / nlab, 6) if nlab else 0.0,
                "macro_acc": macro, "per_class_recall": per}
    return {"localized_events": localized, "total_events": total,
            "t2_impact": blk(t2_ok, has_t2, t2_pairs, n_t2, loc_t2),
            "t3_situation": blk(t3_ok, has_t3, t3_pairs, n_t3, loc_t3)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--answer", required=True)
    ap.add_argument("--gt", required=True)
    ap.add_argument("--out", default="reward.json")
    ap.add_argument("--tol", type=float, default=TOL)
    ap.add_argument("--primary", default="auto", choices=["auto", "strict", "t1"])
    args = ap.parse_args()
    tol = args.tol

    try:
        with open(args.answer, encoding="utf-8-sig") as f:
            ans = as_rows(json.load(f))
    except Exception as e:
        print("answer unreadable (%s) -> 0" % e, file=sys.stderr)
        ans = []
    with open(args.gt, encoding="utf-8") as f:
        gt = as_rows(json.load(f))

    ws = gt_has_strategy(gt)
    s_f1, s_p, s_r, s_tp, s_fp, s_fn = strict_match(ans, gt, tol)
    t1_f1, t1_p, t1_r, t1_tp, t1_fp, t1_fn, _ = match(ans, gt, k_t1, tol)
    tiers = score_tiers(ans, gt, tol)

    primary = args.primary
    if primary == "auto":
        primary = "strict" if ws else "t1"
    reward = s_f1 if primary == "strict" else t1_f1

    diag = {}
    for t in (5.0, 10.0):
        f = strict_match(ans, gt, t)[0]
        diag["strict_f1_tol%ds" % int(t)] = round(f, 6)
    # Format diagnostics. T3 is the situation AFTER the event; an answer using
    # `leader_before` would otherwise look like a model that cannot read the position.
    diag["answer_rows_using_leader_before"] = sum(
        1 for r in ans if r.get("_used_leader_before"))
    diag["answer_rows_missing_impact"] = sum(1 for r in ans
                                             if r["impact"] is None)
    diag["answer_rows_missing_leader_after"] = sum(1 for r in ans
                                                   if r["leader_after"] is None)

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
            "t3_situation": tiers["t3_situation"],
            "localized_events": tiers["localized_events"],
            "total_events": tiers["total_events"],
        },
        "detail": {"answer_rows": len(ans), "gt_rows": len(gt), **diag},
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(json.dumps(result, ensure_ascii=False))
    return result["reward"]


if __name__ == "__main__":
    main()
