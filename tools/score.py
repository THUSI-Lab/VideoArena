#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""score.py -- score a directory of answers against Video-Arena and print the table.

    python tools/score.py --answers runs/my-model
    python tools/score.py --answers runs/my-model --game cs2 --per-case
    python tools/score.py --oracle                        # feed GT back in: every cell 1.0
    python tools/score.py --empty                         # empty answers: every cell 0.0

The answers directory mirrors the corpus:

    <answers>/<game>/<case_id>.json          one answer per case

Each file is the object the instruction asks for -- {"events": [...]}, the same root key in
every one of the nine games. A MISSING file is reported as missing and left out of the mean:
"did not run" is not a measurement of the model. A file that EXISTS is always scored, whatever
is in it -- an answer with no rows (the untouched seed a runner exports for a Protocol B agent
that never wrote or deleted its answer), or one that is not valid JSON at all, scores 0.0. Those
are things the model did, and dropping them would let a worse agent post a higher mean. Both are
tallied per game (`empty`, `unparseable`) so a 0.0 that came from no answer stays visible.

Reported per game and overall:

    F1_strict   the headline: fraction of fully-correct event tuples (all non-temporal
                fields exact, |dt| <= 3s, maximum 1:1) -- the mean over scored cases
    T1          event localization F1        (perceive)
    T2          state-update accuracy        (interpret)
    T3          resulting-state accuracy     (track)

Means are over the cases that actually produced a reward, and the denominator is printed
next to every number. A mean over 17 of 20 cases is a different quantity from a mean over
20, and the only way to keep that readable is to say n.
"""
import argparse
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import va_core as C  # noqa: E402


def mean(xs):
    xs = [x for x in xs if x is not None]
    return (sum(xs) / len(xs)) if xs else None


def fmt(x, w=6):
    return ("%*.*f" % (w, 4, x)) if isinstance(x, (int, float)) else ("%*s" % (w, "--"))


def oracle_answer(game, gt):
    """GT re-expressed as an answer, for --oracle. Answers carry only the fields a model is
    asked for, so this is a projection of GT onto the answer schema -- which is exactly what
    makes it a real end-to-end check of the whole scoring path rather than of the judge alone.
    """
    rk = C.ROOT_KEY[game]
    if rk not in gt:
        raise KeyError("%s GT has no %r key" % (game, rk))
    out = {rk: gt[rk]}
    # dota2 accepts OPTIONAL dense tier blocks alongside the event list. They are not part of
    # the headline, but --oracle has to exercise the paths that read them, so the projection
    # carries them when the GT has them. (cs2 reads T2/T3 ONLY from the event rows, so its
    # GT-side `t2_round_winners` must not ride along: it would be ignored at best.)
    for k in ("t2_leader_track", "t3_outcome"):
        if k in gt:
            out[k] = gt[k]
    return out


def answer_state(game, path):
    """"rows" / "empty" (parses, but no rows under the root key) / "unparseable"."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            obj = json.load(fh)
    except Exception:
        return "unparseable"
    rows = obj.get(C.ROOT_KEY[game]) if isinstance(obj, dict) else obj
    return "rows" if isinstance(rows, list) and rows else "empty"


def score_case(game, cid, answers_dir, work, mode):
    """Score one case; return (subscores_dict, status, answer_state). status is "ok" or why
    not; answer_state is "rows" / "empty" / "unparseable" for a scored cell."""
    r = C.resolve(game, cid)
    ans_path = os.path.join(answers_dir, game, cid + ".json") if answers_dir else None

    if mode == "oracle":
        with open(r["gt"], encoding="utf-8") as fh:
            payload = oracle_answer(game, json.load(fh))
        ans_path = os.path.join(work, "answer.json")
        with open(ans_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
    elif mode == "empty":
        ans_path = os.path.join(work, "answer.json")
        shutil.copyfile(r["answer_seed"], ans_path)
    else:
        if not os.path.isfile(ans_path):
            return None, "missing", None
    state = answer_state(game, ans_path)
    if state == "unparseable":
        # Scored as the empty seed -- 0.0 by construction -- rather than handed to the judge,
        # so the zero does not depend on each judge's tolerance for garbage input.
        ans_path = os.path.join(work, "answer.json")
        shutil.copyfile(r["answer_seed"], ans_path)

    out = os.path.join(work, "reward.json")
    try:
        reward = C.run_judge(game, ans_path, r["gt"], out, tol=C.TOL)
    except Exception as e:
        detail = getattr(e, "stderr", "") or str(e)
        return None, "judge failed: %s" % detail.strip().splitlines()[-1][:120], None
    return C.subscores(reward), "ok", state


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--answers", help="directory of <game>/<case_id>.json answer files")
    ap.add_argument("--oracle", action="store_true",
                    help="score GT against itself; every cell must be 1.0")
    ap.add_argument("--empty", action="store_true",
                    help="score the empty answer seeds; every cell must be 0.0")
    ap.add_argument("--game", action="append",
                    help="restrict to this game (repeatable); default all nine")
    ap.add_argument("--per-case", action="store_true", help="print every case, not just means")
    ap.add_argument("--json-out", help="write the full result table here")
    a = ap.parse_args()

    mode = "oracle" if a.oracle else ("empty" if a.empty else "answers")
    if mode == "answers" and not a.answers:
        ap.error("one of --answers / --oracle / --empty is required")
    games = a.game or C.GAMES
    for g in games:
        if g not in C.GAMES:
            ap.error("unknown game %r (known: %s)" % (g, ", ".join(C.GAMES)))

    work = tempfile.mkdtemp(prefix="va_score_")
    rows = []
    per_game = []
    try:
        for g in games:
            got = []
            notes = []
            states = {"empty": 0, "unparseable": 0}
            for cid in C.case_ids(g):
                s, status, state = score_case(g, cid, a.answers, work, mode)
                rows.append({"game": g, "case_id": cid, "status": status,
                             "answer_state": state,
                             **(s or {"reward": None, "t1_f1": None,
                                      "t2_macro_acc": None, "t3_macro_acc": None})})
                if status == "ok":
                    got.append(s)
                    if state in states:
                        states[state] += 1
                else:
                    notes.append("%s: %s" % (cid, status))
                if a.per_case:
                    print("  %-10s %-38s %s %s %s %s  %s"
                          % (g, cid, fmt((s or {}).get("reward")),
                             fmt((s or {}).get("t1_f1")),
                             fmt((s or {}).get("t2_macro_acc")),
                             fmt((s or {}).get("t3_macro_acc")),
                             status if status != "ok"
                             else ("" if state == "rows" else state + " -> 0")))
            per_game.append({
                "game": g, "n_scored": len(got), "n_cases": len(C.case_ids(g)),
                "n_empty": states["empty"], "n_unparseable": states["unparseable"],
                "f1_strict": mean([x["reward"] for x in got]),
                "t1_f1": mean([x["t1_f1"] for x in got]),
                "t2_macro_acc": mean([x["t2_macro_acc"] for x in got]),
                "t3_macro_acc": mean([x["t3_macro_acc"] for x in got]),
                "not_scored": notes,
            })
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print()
    print("Video-Arena  mode=%s  tol=%.1fs  metric=strict tuple F1" % (mode, C.TOL))
    # `0-row` = scored cells whose answer had no rows or did not parse (both score 0.0 and
    # are IN the mean); the n column's shortfall is the missing / judge-failed cells, which
    # are not.
    print("%-10s %-7s %6s %6s %6s %6s %6s   %s"
          % ("game", "n", "F1", "T1", "T2", "T3", "0-row", "regime"))
    print("-" * 69)
    for r in per_game:
        print("%-10s %2d/%-4d %s %s %s %s %6d   %s"
              % (r["game"], r["n_scored"], r["n_cases"], fmt(r["f1_strict"]), fmt(r["t1_f1"]),
                 fmt(r["t2_macro_acc"]), fmt(r["t3_macro_acc"]),
                 r["n_empty"] + r["n_unparseable"], C.REGIME[r["game"]]))
    print("-" * 69)
    ok = [r for r in per_game if r["n_scored"]]
    n_scored = sum(r["n_scored"] for r in per_game)
    n_total = sum(r["n_cases"] for r in per_game)
    n_empty = sum(r["n_empty"] for r in per_game)
    n_unparse = sum(r["n_unparseable"] for r in per_game)
    # The overall number is the mean of the ENVIRONMENT means, not of the 180 cases: the
    # per-game case counts are equal today, but an unscored cell must not silently reweight
    # the games against each other.
    print("%-10s %2d/%-4d %s %s %s %s %6d   mean of %d env means"
          % ("ALL", n_scored, n_total,
             fmt(mean([r["f1_strict"] for r in ok])), fmt(mean([r["t1_f1"] for r in ok])),
             fmt(mean([r["t2_macro_acc"] for r in ok])), fmt(mean([r["t3_macro_acc"] for r in ok])),
             n_empty + n_unparse, len(ok)))
    if n_empty or n_unparse:
        print("scored as 0.0 with no usable answer: %d empty, %d unparseable"
              % (n_empty, n_unparse))
    missing = [(r["game"], n) for r in per_game for n in r["not_scored"]]
    if missing:
        print()
        print("not scored (%d):" % len(missing))
        for g, n in missing[:40]:
            print("  %-10s %s" % (g, n))
        if len(missing) > 40:
            print("  ... and %d more" % (len(missing) - 40))

    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as fh:
            json.dump({"mode": mode, "tol": C.TOL, "per_game": per_game, "cases": rows},
                      fh, ensure_ascii=False, indent=2)
        print("\nwrote %s" % a.json_out)

    # --oracle and --empty are assertions, not reports: they must hit their fixed point on
    # every scored cell, and the exit code is what a CI run reads.
    if mode in ("oracle", "empty"):
        want = 1.0 if mode == "oracle" else 0.0
        bad = [r for r in rows if r["status"] != "ok" or r["reward"] != want]
        print("\n%s self-check: %d/%d cells at %.1f -> %s"
              % (mode, len(rows) - len(bad), len(rows), want, "PASS" if not bad else "FAIL"))
        for r in bad[:10]:
            print("  %-10s %-38s reward=%s %s"
                  % (r["game"], r["case_id"], r["reward"],
                     "" if r["status"] == "ok" else r["status"]))
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
