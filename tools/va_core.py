#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""va_core.py -- the Video-Arena corpus contract: what a case is, and how it is scored.

Everything a driver needs is here and nothing else is. Four pieces:

  1. resolve(game, case_id)   -- the files that make up a case.
  2. instruction_for(...)     -- the task text for ONE protocol (see the marker note below).
  3. extract_answer(...)      -- pull the answer object out of a Protocol A chat reply.
  4. run_judge(...)           -- invoke the game's judge as a subprocess, read reward.json.

Pure standard library. No network, no model, no credentials: this module is about the
corpus, and it must be importable on a machine that has never seen an API key.

GT never reaches the model. resolve() returns the GT path because the OFFLINE scorer needs
it; a driver that hands r["gt"] to a prompt or into a sandbox has broken the benchmark, not
this module.
"""
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TASKS = os.path.join(ROOT, "tasks")

# ---- the scoring contract ------------------------------------------------------------
# The nine games share one layout, one headline metric, and ONE answer root key. They differ
# only in which judge computes it, and the two judges differ only in their CLI and in where
# they put the tier decomposition:
#
#   F1  judge.py --answer/--gt/--out/--tol --primary strict
#       tier decomposition under reward["tiers"]
#   F2  the same CLI WITHOUT --primary (its headline is always the single coupled F1)
#       tier decomposition under reward["detail"]
#
# TOL is the +/- seconds a predicted event time may differ from the reference and still
# match. It is 3.0 for every game; a run at another tolerance is a diagnostic, not a
# benchmark score.
TOL = 3.0

GAMES = ["go", "chess", "gomoku", "doudizhu", "cs2", "dota2", "sc2", "gfootball", "badminton"]

# ONE root key, every game: {"events": [ ...one complete tuple per row... ]}. A wrong root key is
# scored as an empty answer, indistinguishable from a model that saw nothing. Kept as a name
# rather than inlined because the runners record it per cell.
ROOT_KEY = {g: "events" for g in GAMES}

# The two judge families differ in where the tier block lands in reward.json and whether the
# judge accepts --primary. This is a property of the SCORER, not of the answer, so it is
# declared here rather than derived from the root key.
FAMILY = {"go": "F1", "chess": "F1", "gomoku": "F1", "doudizhu": "F1",
          "gfootball": "F1", "badminton": "F1",
          "cs2": "F2", "dota2": "F2", "sc2": "F2"}
REGIME = {"go": "board", "chess": "board", "gomoku": "board", "doudizhu": "card",
          "cs2": "fps", "dota2": "moba", "sc2": "rts",
          "gfootball": "sports", "badminton": "sports"}


def case_ids(game):
    """The published 20 case ids for `game`, in the frozen order of cases.json."""
    with open(os.path.join(TASKS, game, "cases.json"), encoding="utf-8") as fh:
        return list(json.load(fh)["case_ids"])


def resolve(game, case_id):
    """The files of one case. `video` and `instruction` are the ONLY model inputs."""
    d = os.path.join(TASKS, game)
    return {
        "game": game,
        "case_id": case_id,
        "video": os.path.join(d, "media", case_id + ".mp4"),
        "gt": os.path.join(d, "gt", case_id + "_gt.json"),        # offline scorer only
        "instruction": os.path.join(d, "instruction.md"),
        "answer_seed": os.path.join(d, "answer_seed.json"),
        "judge": os.path.join(d, "judge.py"),
        "root_key": ROOT_KEY[game],
        "family": FAMILY[game],
        "tol": TOL,
        "primary": None if FAMILY[game] == "F2" else "strict",
    }


# ---- protocol-specific instruction text ----------------------------------------------
# One instruction.md per game serves both protocols, because the TASK is identical and only
# the DELIVERY differs: Protocol B writes answer.json in a sandbox over many turns, Protocol
# A returns one JSON object in one chat reply. The parts that differ are marked in the file:
#
#   <!-- B-ONLY:START -->  ... whole lines ...  <!-- B-ONLY:END -->      agentic only
#   <!-- A-ONLY:START -->  ... whole lines ...  <!-- A-ONLY:END -->      video only
#   <!--B-->inline span<!--/B-->        <!--A-->inline span<!--/A-->
#
# Marking rather than shipping two files is what keeps the protocols provably the same task:
# the per-game guidance lives once, and a change to the task reaches both protocols or
# neither. Always send instruction_for(protocol, path), never the raw file -- the raw file
# tells a model with no filesystem to write a file, and vice versa.
_MARK_RE = re.compile(r"<!--\s*/?[AB](?:-ONLY:(?:START|END))?\s*-->")
_KEEP = {"agentic": ("B", "A"), "video": ("A", "B"), "B": ("B", "A"), "A": ("A", "B")}


def instruction_for(protocol, path):
    """Instruction text for ONE protocol: "agentic"/"B" or "video"/"A".

    Fails closed. A surviving marker means the file is malformed (unbalanced or mistyped),
    and raising is strictly better than sending the other protocol's delivery text -- or a
    raw HTML comment -- to a model.
    """
    keep, drop = _KEEP[protocol]
    with open(path, "rb") as fh:
        t = fh.read().decode("utf-8")
    t = re.sub(r"(?ms)^<!-- " + drop + r"-ONLY:START -->\r?\n.*?^<!-- " + drop
               + r"-ONLY:END -->\r?\n?", "", t)
    t = re.sub(r"(?s)<!--" + drop + r"-->.*?<!--/" + drop + r"-->", "", t)
    t = re.sub(r"(?m)^<!-- " + keep + r"-ONLY:(?:START|END) -->\r?\n", "", t)
    t = re.sub(r"<!--" + keep + r"-->|<!--/" + keep + r"-->", "", t)
    left = _MARK_RE.findall(t)
    if left:
        raise ValueError("%s: unbalanced protocol marker(s) survived: %s" % (path, left[:4]))
    return t


# ---- answer extraction (Protocol A) --------------------------------------------------
def _balanced_objects(text):
    """Yield (offset, source) for every TOP-LEVEL balanced {...}, ignoring braces inside
    strings. Top-level only, so a single answer ROW that happens to carry the root key
    cannot masquerade as the whole answer; an object left unclosed by a truncated response
    is never yielded at all."""
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0:
                yield start, text[start:i + 1]


def answer_candidates(text, root_key="events"):
    """Every parseable top-level JSON object in `text` carrying `root_key`, in order.

    More than one is worth recording: the Protocol A prompt asks for exactly one object, so
    a second candidate means the reply held a schema sketch, a per-segment dump, or the same
    answer twice.
    """
    out = []
    for start, src in _balanced_objects(text or ""):
        try:
            obj = json.loads(src)
        except Exception:
            continue
        if isinstance(obj, dict) and root_key in obj:
            rows = obj[root_key]
            out.append({"start": start, "obj": obj,
                        "n_rows": len(rows) if isinstance(rows, (list, dict)) else 0})
    return out


def extract_answer(text, root_key="events"):
    """Pull the answer object out of a chat reply: a fenced json block, a bare object with
    `root_key`, or a bare list (wrapped under it).

    Among all top-level objects carrying `root_key`, take the one with the MOST rows; ties
    go to the one appearing LAST. A model that prints a one-row schema sketch before its
    real answer must not be scored on the sketch, and a model that emits several fences must
    not be scored on an empty early one.
    """
    if not text:
        raise ValueError("empty model response")
    cands = answer_candidates(text, root_key)
    if cands:
        return max(cands, key=lambda c: (c["n_rows"], c["start"]))["obj"]
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and root_key in obj:
            return obj
        if isinstance(obj, list):
            return {root_key: obj}
    except Exception:
        pass
    raise ValueError("no JSON object with a %r key found in response" % root_key)


# ---- Protocol A delivery ---------------------------------------------------------------
# The single-reply tail appended to the A-rendered instruction, verbatim from the paper's
# Protocol A prompt (appendix, "the frozen prompts"). The native runner and the Harbor agent
# both build their prompt here, so the two paths send the same text.
A_TAIL_HEAD = "---\nYou are given the match as "
_A_TAIL = A_TAIL_HEAD + """{n} frames, uniformly sampled from a {dur}-second video \
(one frame every {dt} video-seconds). Frame k (0-indexed) is at video_ts[k], in seconds:
video_ts = [{ts}]. Use these exact values for timestamps; do not recompute them.

This is one reply and there are no tools: no sandbox, no file to write, no second turn, \
and nothing runs after you stop. You may reason here first if it helps.

Your answer is the LAST ```json fenced block in this reply. It must hold the COMPLETE JSON
object with the single `{root_key}` key -- every row you want scored, in that one block.
Print no other JSON anywhere in the reply: no schema sketch, no partial dump, no repeat of
the answer. Nothing outside that last block is read.
"""
_CANARY_RE = re.compile(r"\n*<!-- CANARY:[^\n]*-->\s*$")     # a file marker, not prompt text
_AGENTIC_TEXT = ("answer.json", "match.mp4", "ffmpeg", "ffprobe", "sandbox")


def protocol_a_prompt(instruction, duration_s, ts, fps, root_key="events"):
    """The whole Protocol A text: the A-rendered instruction (instruction_for("video", ...)),
    then the single-reply tail carrying the grid's explicit video_ts array. It passes
    check_protocol_a_prompt() before it is returned."""
    tail = _A_TAIL.format(n=len(ts), dur="%.1f" % duration_s, dt="%.2f" % (1.0 / fps),
                          ts=", ".join("%.2f" % t for t in ts), root_key=root_key)
    body = _CANARY_RE.sub("", instruction.replace("\r\n", "\n")).rstrip("\n")
    prompt = body + "\n\n" + tail
    check_protocol_a_prompt(prompt, root_key)
    return prompt


def check_protocol_a_prompt(prompt, root_key="events"):
    """The pre-flight check, run before any tokens are spent: reject a Protocol A prompt that
    still carries agentic delivery text, lacks the single-reply tail, or whose tail does not
    name the schema's root key."""
    body, head, tail = prompt.rpartition(A_TAIL_HEAD)
    if not head:
        raise ValueError("Protocol A prompt has no single-reply tail")
    leaked = [w for w in _AGENTIC_TEXT if w in body]
    if leaked:
        raise ValueError("Protocol A prompt carries agentic delivery text: %s" % leaked)
    if "This is one reply and there are no tools" not in tail \
            or "`%s`" % root_key not in tail:
        raise ValueError("Protocol A tail is incomplete or does not name the root key %r"
                         % root_key)


# ---- scoring -------------------------------------------------------------------------
def run_judge(game, answer_path, gt_path, out_path, tol=TOL):
    """Score one case: run the game's judge as a SUBPROCESS and return the reward dict.

    A subprocess, not an import: the judges are nine independent single-file programs that
    share module names, and invoking them exactly the way their CLI documents them is what
    lets anyone reproduce a published number by hand.
    """
    meta = resolve(game, "_")
    cmd = [sys.executable, meta["judge"], "--answer", answer_path, "--gt", gt_path,
           "--out", out_path, "--tol", str(tol)]
    if meta["primary"]:
        cmd += ["--primary", meta["primary"]]
    subprocess.run(cmd, check=True, capture_output=True, text=True,
                   encoding="utf-8", errors="replace")
    with open(out_path, encoding="utf-8") as fh:
        return json.load(fh)


def subscores(reward):
    """Headline + per-tier diagnostics out of a reward.json, across both judge families.

    The headline is always reward["reward"] (strict tuple F1). The tier keys are NOT
    uniform, and guessing wrong would blank a column -- and a blank reads as "not measured"
    rather than "we looked in the wrong place". So: try F1's `tiers` block, fall back to
    F2's `detail`, and inside each accept every key a shipped judge actually writes.

    T3's metric varies by game: board / card / MOBA / RTS score a class label (macro
    accuracy over {A, B, equal}); badminton and gfootball score an exact integer score pair
    (exact_pair_acc); cs2 scores the exact cumulative pair (exact_frac). All three live in
    the same column because all three answer "did it track the state".
    """
    t = reward.get("tiers")
    if t:
        t3 = t.get("t3_situation") or t.get("t3_score") or {}
        t3v = t3.get("macro_acc")
        if t3v is None:
            t3v = t3.get("exact_pair_acc")
        return {"reward": reward.get("reward"),
                "t1_f1": (t.get("t1_localization") or {}).get("f1"),
                "t2_macro_acc": (t.get("t2_impact") or {}).get("macro_acc"),
                "t3_macro_acc": t3v}
    d = reward.get("detail", {})
    t2 = d.get("t2_round_winner") or d.get("t2_minute_gain_event") or d.get("t2_impact") or {}
    t3 = (d.get("t3_score_track") or d.get("t3_leader_after_event")
          or d.get("t3_situation") or {})
    t3v = t3.get("exact_frac")
    if t3v is None:
        t3v = t3.get("macro_acc")
    return {"reward": reward.get("reward"),
            "t1_f1": (d.get("t1_localization") or {}).get("f1"),
            "t2_macro_acc": t2.get("macro_acc"),
            "t3_macro_acc": t3v}


def t1_recall_ceiling(duration_s, n_frames, tol=TOL):
    """Highest T1 recall a uniform `n_frames` grid can reach on a `duration_s` video.

    With frame spacing d = duration / n_frames, an event lies within `tol` of some sampled
    frame at most min(1, 2*tol/d) of the time. Protocol A on a long match is therefore
    capped BELOW 1 by its own sampling grid, and recording that alongside the score is what
    separates "the model missed the event" from "no frame was close enough to it".
    """
    if not n_frames or duration_s <= 0:
        return 0.0
    d = float(duration_s) / float(n_frames)
    return min(1.0, 2.0 * float(tol) / d) if d > 0 else 1.0


def case_meta(game):
    """The build-time tags of every case of `game`, keyed by case id (CASE_TYPES.json).

    Read-only passthrough: the generation scripts use it for task metadata and nothing in the
    scoring path touches it. The id key is `id` for the four games whose cases.json carries it
    and `case_id` for the rest.
    """
    path = os.path.join(TASKS, game, "CASE_TYPES.json")
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    out = {}
    for row in doc.get("cases", []):
        key = row.get("id", row.get("case_id"))
        if key:
            out[key] = row
    return out


def build_harbor_prompt(max_turns=200, timeout_s=7200):
    """Protocol B's turn-1 prompt, written for the Harbor sandbox (working directory /app).

    Same contract as tools/run_protocol_b.py: one deliverable, `answer.json`, filled in as the
    run goes; a text reply is not an answer. State it with the sandbox's own paths so the agent
    never has to guess where `instruction.md` and `match.mp4` are, and so the no-network rule is
    stated where it can be obeyed.
    """
    minutes = round(timeout_s / 60)
    budget = ("You have at most %d tool-call turns and about %d minutes of wall-clock time. "
              "When either limit is reached you are stopped immediately." % (max_turns, minutes))
    return """You are a HEADLESS autonomous agent -- there is no human to talk to. Communicate ONLY by
calling tools (running commands, reading and writing files); a text reply is not an answer. Your
one deliverable is the file `answer.json` in `/app`.

Read `/app/instruction.md` for the task and the exact `answer.json` schema. Your only input is the
video `/app/match.mp4`. The video plays at 1:1 real speed; `instruction.md` says exactly which
clock each time field you report is on.

Solve it yourself, from the video. Do not use the network for anything, and there is none: no
downloading or installing packages, models or weights, no calls to any external service, API or
model, and no looking this match up. Everything you need is already installed -- ffmpeg and
ffprobe, Python with numpy/OpenCV/Pillow and the usual numeric stack, tesseract for on-screen
text. The shell, that Python, and those video tools are your only tools.

`answer.json` already exists in `/app` -- created before your first turn, holding the schema's
root key and an empty list. Do not delete it, do not rename it, and do not change the root key.
Overwrite it in place -- in full, valid JSON every time -- as soon as you have something to put
in it, and keep overwriting it as you learn more. It must be valid JSON at every moment: rewrite
it in full each time (read -> add -> write back), never leave it half-written. Each rewrite should
contain everything the previous one had, plus the new rows; if you are deep into the video and the
file is not getting longer, you are overwriting instead of appending.

Whatever is in `answer.json` when the run stops is what gets scored. If you are cut off, every row
already written still counts; rows kept only in your reasoning, to be written out at the end,
score zero.

%s Everything else -- including how you sample the video -- is up to you.""" % budget


if __name__ == "__main__":
    # Self-check: enumerate every case and confirm each of its files is on disk.
    missing = 0
    for g in GAMES:
        ids = case_ids(g)
        for cid in ids:
            r = resolve(g, cid)
            for k in ("video", "gt", "instruction", "judge", "answer_seed"):
                if not os.path.isfile(r[k]):
                    print("MISSING %s/%s %s: %s" % (g, cid, k, r[k]))
                    missing += 1
        print("%-10s %2d cases  family=%s  root_key=%s" % (g, len(ids), FAMILY[g], ROOT_KEY[g]))
    print("missing files: %d" % missing)
    raise SystemExit(1 if missing else 0)
