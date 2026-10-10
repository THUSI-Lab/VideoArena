#!/usr/bin/env python3
"""
Deterministic verifier for the Video-Arena board-game THREE-TIER task
(Go / Chess / Gomoku share this scorer). Pure code — no VLM/LLM judge.

Scoring is TIME-MATCHED F1:
predicted rows are matched 1:1 to GT rows with the SAME key and |Δt| ≤ TOL
seconds (default 3s), taking the LARGEST such matching (augmenting paths), so the
score cannot depend on row order. reward = F1 over matched rows.

Order does not matter and a single wrong/missing row does not affect the others:
a prediction is scored on its own (key, time), so getting move 1 wrong costs
exactly one event.
Time (game_clock_s = elapsed video seconds when the stone appears) disambiguates
repeated coordinates (Go ko recaptures, a point replayed after a capture).

Three tiers over the same video:
    T1  event localization    -> (type, color, coord)            + capture rows
    T2  event impact          -> wr_swing      in {B, W, equal}   (move rows)
    T3  cumulative situation  -> leader_after  in {B, W, equal}   (move rows)

T3 is `leader_after`: who is ahead AFTER this move lands (the engine's absolute
win-rate of the resulting position, banded ±10pts), which fuses this event with
the whole game so far, like DOTA2 taking the leader at the END of the event's
minute and CS2's `score_after`. `leader` is accepted as a synonym; the key
`leader_before` (the situation ENTERING the move) names a different quantity, is
NOT accepted for T3, and is only counted as a format diagnostic.

Answer format (JSON written by the agent):
    {"events": [
        {"type":"move","idx":1,"color":"B","coord":"Q4","game_clock_s":2.0,
         "leader_after":"equal","wr_swing":"equal"},
        {"type":"capture","idx":1,"color":"W","coord":"C18","game_clock_s":57.3},
        ...]}
Capture rows never carry strategy fields. Chess captures at most one stone per
ply; Gomoku has no capture rows at all.

Scores reported:
  strict     — F1 with the FULL key: move needs (color, coord, leader_after,
               wr_swing) + |Δt|≤3s; capture needs (color, coord) + |Δt|≤3s.
               PRIMARY reward when GT carries strategy labels.
  tiers.t1_localization — F1 with key (type, color, coord) + |Δt|≤3s (drops the
               strategy fields). == the localization score. PRIMARY when no strategy.
  tiers.t2_impact / t3_situation — over GT MOVE rows, using the T1 time-match to
               pair predictions: acc_all AND macro_acc (mean per-class recall over
               {B,W,equal}). macro_acc is the shortcut-resistant headline metric
               (always-'equal' / always-'B' collapse to 0.333, oracle = 1.0). A GT
               move with no T1 match counts as wrong for its class.

Usage:
    python judge.py --answer answer.json --gt gt.json [--out reward.json]
                    [--tol 3.0] [--primary auto|strict|t1]
"""
import argparse, heapq, json, re, sys

TOL = 3.0  # seconds

TYPE_ALIAS = {"MOVE": "move", "PLAY": "move", "落子": "move", "STONE": "move",
              "CAPTURE": "capture", "TAKE": "capture", "提子": "capture",
              "CAPTURED": "capture"}


def norm_type(t):
    return TYPE_ALIAS.get(str(t).strip().upper(), str(t).strip().lower())


def norm_color(c):
    c = str(c).strip().upper()
    return {"B": "B", "BLACK": "B", "黑": "B",
            "W": "W", "WHITE": "W", "白": "W"}.get(c, c)


def norm_side(x):
    """Normalize a strategy label into {B, W, equal} (None stays None)."""
    if x is None:
        return None
    s = str(x).strip().upper()
    return {"B": "B", "BLACK": "B", "黑": "B",
            "W": "W", "WHITE": "W", "白": "W",
            "EQUAL": "equal", "EQ": "equal", "E": "equal", "=": "equal",
            "EVEN": "equal", "DRAW": "equal", "TIE": "equal",
            "均": "equal", "平": "equal", "均势": "equal"}.get(s, s.lower())


def norm_coord(c):
    """Board intersection. Accepts Go/Gomoku (A-T skip I / A-O) and chess (A-H)."""
    c = str(c).strip().upper().replace(" ", "")
    if c in ("PASS", "PAS", "-", ""):
        return "PASS"
    m = re.match(r"^([A-HJ-T])(\d{1,2})$", c)      # letter (I skipped) + number
    if m:
        return f"{m.group(1)}{int(m.group(2))}"
    m = re.match(r"^([A-Z])(\d{1,2})$", c)          # generic fallback (e.g. chess I-less n/a)
    return f"{m.group(1)}{int(m.group(2))}" if m else c


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
        typ = norm_type(r.get("type", ""))
        row = {"type": typ,
               "color": norm_color(r.get("color", "")),
               "coord": norm_coord(r.get("coord", r.get("move", ""))),
               "game_clock_s": norm_time(r),
               "leader_after": None, "wr_swing": None,
               "_used_leader_before": False}
        if typ == "move":
            # `leader` is accepted as a synonym for leader_after; `leader_before`
            # is NOT accepted (it names a different quantity — the situation
            # entering the move) and is only diagnosed.
            row["leader_after"] = norm_side(r.get("leader_after", r.get("leader")))
            row["wr_swing"] = norm_side(r.get("wr_swing"))
            row["_used_leader_before"] = ("leader_after" not in r
                                          and "leader" not in r
                                          and "leader_before" in r)
        rows.append(row)
    return rows


def gt_has_strategy(gt):
    return any(r["type"] == "move" and r["leader_after"] is not None for r in gt)


def k_strict(r):
    if r["type"] == "move":
        return ("move", r["color"], r["coord"], r["leader_after"], r["wr_swing"])
    return ("capture", r["color"], r["coord"])


def k_t1(r):
    return (r["type"], r["color"], r["coord"])


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
    """Macro (balanced) accuracy = mean per-class recall over classes present in GT.
    Defeats the majority-class shortcut. pairs = [(gt_label, pred_label_or_None)]."""
    from collections import defaultdict
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
    """T2/T3 over GT MOVE rows, paired to predictions via the T1 (type,color,coord)
    time-match. A GT move not localized within ±tol counts as wrong for its class."""
    _, _, _, _, _, _, t1_pairs = match(pred, gt, k_t1, tol)
    pred_for_gt = {gi: a for gi, a in t1_pairs}
    gt_moves = [(i, g) for i, g in enumerate(gt) if g["type"] == "move"]
    total_moves = len(gt_moves)
    localized = sum(1 for i, _ in gt_moves if i in pred_for_gt)
    has_t2 = any(g["wr_swing"] is not None for _, g in gt_moves)
    has_t3 = any(g["leader_after"] is not None for _, g in gt_moves)
    t2_pairs, t3_pairs = [], []
    t2_ok = t3_ok = 0
    for i, g in gt_moves:
        a = pred_for_gt.get(i)
        psw = a["wr_swing"] if a else None
        pla = a["leader_after"] if a else None
        if has_t2:
            t2_pairs.append((g["wr_swing"], psw))
            if psw is not None and psw == g["wr_swing"]:
                t2_ok += 1
        if has_t3:
            t3_pairs.append((g["leader_after"], pla))
            if pla is not None and pla == g["leader_after"]:
                t3_ok += 1

    def blk(ok, has, pairs):
        if not has:
            return None
        macro, per = _macro_acc(pairs)
        return {
            "correct": ok, "of_localized": localized, "of_all_moves": total_moves,
            "acc_localized": round(ok / localized, 6) if localized else 0.0,
            "acc_all": round(ok / total_moves, 6) if total_moves else 0.0,
            "macro_acc": macro,          # PRIMARY tier metric (shortcut-resistant)
            "per_class_recall": per,
        }
    return {"localized_moves": localized, "total_moves": total_moves,
            "t2_impact": blk(t2_ok, has_t2, t2_pairs),
            "t3_situation": blk(t3_ok, has_t3, t3_pairs)}


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
        print(f"answer unreadable ({e}) -> 0", file=sys.stderr)
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

    # diagnostic F1 at looser tolerances (not the reward)
    diag = {}
    for t in (5.0, 10.0):
        f, *_ = match(ans, gt, k_strict, t)
        diag[f"strict_f1_tol{int(t)}s"] = round(f, 6)
    # Format diagnostic: T3 is the situation AFTER the move. An answer emitting
    # `leader_before` instead gets no T3 credit; surface the count so a format
    # error is distinguishable from genuine misreading of the position.
    diag["answer_rows_using_leader_before"] = sum(
        1 for r in ans if r.get("_used_leader_before"))

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
            "localized_moves": tiers["localized_moves"],
            "total_moves": tiers["total_moves"],
        },
        "detail": {"answer_rows": len(ans), "gt_rows": len(gt), **diag},
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(json.dumps(result, ensure_ascii=False))
    return result["reward"]


if __name__ == "__main__":
    main()
