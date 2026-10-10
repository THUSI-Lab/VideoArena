#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_protocol_b.py -- minimal Protocol B (agentic) reference runner.

Protocol B hands the WHOLE video to a coding agent in a sandbox folder and lets it work for
many turns: sample frames however it likes, write its own parsers, and build up `answer.json`
incrementally. It is the same task as Protocol A and a different capability -- tool use and
self-directed sampling instead of a fixed frame grid.

This script builds the sandbox and runs YOUR agent in it. It deliberately does not bundle a
harness: every coding agent has its own CLI, and hardcoding one would make the reference
implementation useless for the others. You supply the command; this script supplies the part
that must not vary between systems -- what is in the sandbox, what the agent is told, and
what gets collected afterwards.

    python tools/run_protocol_b.py --game chess --case all --out runs/agentX-B \\
        --agent-cmd "claude -p @PROMPT_FILE@ --max-turns 200" [--workers 3]
    python tools/score.py --answers runs/agentX-B/answers

Placeholders in --agent-cmd, substituted per cell:

    @PROMPT_FILE@   path to prompt.txt      (the turn-1 user message)
    @PROMPT@        the prompt text itself, for a CLI that takes it inline
    @SANDBOX@       the sandbox folder       (also the command's working directory)

--agent-cmd is split with POSIX quoting on every OS ("a b" is one argument, the quotes are
removed); on Windows a backslash is literal, so C:\\tools\\agent.exe needs no escaping.

Prefer @PROMPT_FILE@. A multi-line prompt passed as one argv element is truncated at the
first newline by several shells and batch shims, and a truncated prompt still SCORES -- the
agent re-derives the task from instruction.md and the cell looks like a valid measurement.

THE SANDBOX -- a fresh folder with a random name under the system temp dir (or
--sandbox-root), outside the repo and never reused, holding exactly these files:

    match.mp4         the video, the only input (renamed, so the case id is not a hint)
    instruction.md    the task, rendered for Protocol B
    answer.json       a BYTE-FOR-BYTE copy of answer_seed.json: the root key, empty list

The sandbox is outside the repo so the ground truth is not a relative path away and the
folder name does not spell the case id; prompt.txt sits next to the sandbox, not in it.
An agent with full-disk access can still go looking for the repo -- that is caught by the
transcript audit below, not by the folder layout.

Pre-seeding rather than letting the agent create the file is what makes "it never answered"
measurable: if the final bytes still hash to the seed, the agent never wrote an answer, and
that is a different result from an answer that scored 0.0. It is still SCORED 0.0 -- the
runner exports the untouched seed for a never_wrote or deleted answer, so the cell stays in
the mean (leaving it out would let an agent that never answers post a higher mean than one
that answers badly) -- and run.json records which of the three happened.

A re-run of a cell first deletes the answer, run.json and logs an earlier run left, so a run
that dies early can never be scored on its predecessor's answer. On timeout the agent's whole
process tree is killed, not just the command you gave: an agent CLI is a launcher, and its
children would otherwise keep running -- and keep writing -- after the cell was collected.

THE MACHINE -- the agent runs on THIS machine, and the prompt forbids installing anything,
so its toolset is whatever is already here. Nothing is installed for you. To match the Harbor
image (tools/harbor/Dockerfile), and so that native and Harbor numbers are comparable:

    ffmpeg + ffprobe on PATH, tesseract (with English data), Python 3 with
    pip install numpy scipy pandas pillow opencv-python-headless scikit-image \
                imageio imageio-ffmpeg av matplotlib pytesseract tqdm

Before the first cell the runner checks ffmpeg, ffprobe and python3 on PATH (a hard error:
without them every cell would still run, and still score, as if it were a measurement) and
warns about missing Python packages and tesseract. --skip-preflight turns the check off,
e.g. when --agent-cmd starts the agent in a container of its own.

WHAT THIS DOES NOT DO: enforce the no-network rule. The agent's own brain is an HTTPS call,
so there is no configuration where the sandbox is offline and the run still happens. The
prompt states the rule, and a serious evaluation AUDITS the agent's transcript afterwards
for package installs, downloads, and lookups of the match. An audit is the honest mechanism
here; a claim of isolation would not be true. Your harness's transcript format is your own,
so the audit lives in your harness -- not here.
"""
import argparse
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import va_core as C  # noqa: E402

# The turn-1 message. It carries only the DELIVERY contract -- the task itself is
# instruction.md, which the agent is told to read. Deliberately identical across systems
# except for the one budget sentence, which is a harness fact rather than a wording choice:
# a harness that cannot count turns must not be made to claim a turn cap.
PROMPT = """You are a HEADLESS autonomous agent -- there is no human to talk to.
Communicate ONLY by calling tools (running commands, reading and writing files); a text
reply is not an answer. Your one deliverable is the file `answer.json` in this folder.

Read `instruction.md` in this folder for the task and the exact `answer.json` schema. The
only input is the video `match.mp4` in this folder. The video plays at 1:1 real speed;
`instruction.md` says exactly which clock each time field you report is on.

Solve it yourself, from the video. Do not use the network for anything: no downloading or
installing packages, models or weights, no calls to any external service, API or model, and
no looking this match up. The shell, the Python already installed, and the video tools
already on this machine are your only tools. Any network use voids the run, which is then
scored zero whatever `answer.json` contains.

`answer.json` is already in this folder -- it was created before your first turn, with the
schema's root key and an empty list. Do not delete it, do not rename it, and do not change
the root key. Overwrite it in place -- in full, valid JSON every time -- as soon as you have
something to put in it, and keep overwriting it as you learn more. It must be valid JSON at
every moment: rewrite it in full each time (read -> add -> write back), never leave it
half-written. Each rewrite should contain everything the previous one had, plus the new rows;
if you are deep into the video and the file is not getting longer, you are overwriting
instead of appending.

Whatever is in `answer.json` when the run stops is what gets scored. If you are cut off,
every row already written still counts; rows kept only in your reasoning, to be written out
at the end, score zero.

{budget} Everything else -- including how you sample the video -- is up to you."""


def build_prompt(max_turns, timeout_s):
    minutes = round(timeout_s / 60)
    if max_turns:
        budget = ("You have at most %d tool-call turns and about %d minutes of wall-clock "
                  "time. When either limit is reached you are stopped immediately."
                  % (max_turns, minutes))
    else:
        budget = ("You have about %d minutes of wall-clock time. When that limit is reached "
                  "you are stopped immediately." % minutes)
    return PROMPT.format(budget=budget)


def seed_answer(sandbox, game, root_key):
    """Copy answer_seed.json into the sandbox as answer.json, byte for byte.

    Byte-for-byte rather than json.dump: a driver that re-serializes the seed can get the
    root key wrong, and the resulting 0.0 looks exactly like a model that cannot write JSON.
    Fails closed on a seed that is missing, unparseable, keyed differently from the judge, or
    carries a row -- each of those would be charged to the agent.
    """
    src = C.resolve(game, "_")["answer_seed"]
    with open(src, "rb") as fh:
        raw = fh.read()
    seed = json.loads(raw.decode("utf-8"))
    if root_key not in seed:
        raise ValueError("%s: seed keys %s do not include the judge's root key %r"
                         % (game, list(seed), root_key))
    for k, v in seed.items():
        if isinstance(v, list) and v:
            raise ValueError("%s: seed key %r is not empty -- a seeded row is part of the "
                             "answer" % (game, k))
    dst = os.path.join(sandbox, "answer.json")
    shutil.copyfile(src, dst)
    st = os.stat(dst)
    return {"seed_sha256": hashlib.sha256(raw).hexdigest(), "seeded_size": st.st_size}


def answer_state(sandbox, baseline):
    """Three outcomes, told apart by the digest rather than by the score:

      deleted       the agent removed or renamed the file (the prompt says not to)
      never_wrote   still byte-identical to the seed: the agent never answered, which is a
                    budget or behaviour failure, not "answered and scored 0"
      wrote         the bytes changed, so score whatever is in there

    The first two both score 0.0 (the runner exports the seed for them); this field is what
    tells them apart afterwards. The digest decides, not mtime or size: rewriting the seed
    with identical content is still not an answer.
    """
    p = os.path.join(sandbox, "answer.json")
    if not os.path.isfile(p):
        return {"state": "deleted", "final_sha256": None, "final_size": None}
    with open(p, "rb") as fh:
        raw = fh.read()
    dig = hashlib.sha256(raw).hexdigest()
    return {"state": "never_wrote" if dig == baseline["seed_sha256"] else "wrote",
            "final_sha256": dig, "final_size": len(raw)}


def split_cmd(cmd):
    """--agent-cmd -> argv, with POSIX quoting on every OS. shlex's Windows mode
    (posix=False) keeps the quote characters INSIDE the token, so `--system "be terse"`
    reached the agent as two arguments `"be` and `terse"`. Backslash is literal on Windows,
    so a path like C:\\tools\\agent.exe survives unescaped."""
    lex = shlex.shlex(cmd, posix=True)
    lex.whitespace_split = True
    lex.commenters = ""
    if os.name == "nt":
        lex.escape = ""
    return list(lex)


def kill_tree(p):
    """Kill `p` AND everything it started. subprocess.run(timeout=) kills only the direct
    child, and an agent CLI is a launcher: the model loop, its shell and that shell's ffmpeg
    are descendants, and they kept running -- and kept writing answer.json -- after the cell
    had been collected."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        try:
            os.killpg(p.pid, signal.SIGKILL)        # start_new_session: pgid == pid
        except (ProcessLookupError, PermissionError):
            pass
    try:
        p.kill()
    except OSError:
        pass
    p.wait()


def run_agent(cmd, cwd, stdout, stderr, timeout_s):
    """Run the agent as the root of its own process tree and return its exit code. On
    timeout -- or anything else that interrupts this script, Ctrl-C included -- the whole
    tree is killed before the exception propagates."""
    kw = {} if os.name == "nt" else {"start_new_session": True}
    p = subprocess.Popen(cmd, cwd=cwd, stdout=stdout, stderr=stderr, **kw)
    try:
        return p.wait(timeout=timeout_s)
    except BaseException:
        kill_tree(p)
        raise


def clear_cell(cell, answer):
    """Delete what an earlier run of this cell left behind, so nothing stale can be read
    as this run's result: a re-run that dies before collecting would otherwise leave the
    PREVIOUS answer in answers/ to be scored, next to a run.json that does not describe it."""
    for p in (answer, os.path.join(cell, "run.json"), os.path.join(cell, "prompt.txt"),
              os.path.join(cell, "agent_stdout.txt"), os.path.join(cell, "agent_stderr.txt")):
        if os.path.isfile(p):
            os.remove(p)
    shutil.rmtree(os.path.join(cell, "sandbox"), ignore_errors=True)


def remove_tree(path):
    """rmtree that waits out Windows releasing a killed process's file handles."""
    for _ in range(10):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return True
        time.sleep(1)
    return False


def collect(sandbox, rec, seed_path, dst, root_key):
    """Export the cell's answer to answers/ and fill rec: exported / n_rows / unparseable.

      wrote                    the agent's file, as-is -- even if it does not parse (score.py
                               then scores it 0.0 and tallies it as unparseable; withholding
                               it would turn "left a broken file" into "missing", which drops
                               the cell from the mean)
      never_wrote / deleted    the untouched seed -> 0.0, still in the denominator
      the agent never started  (stop_reason harness_error, or no sandbox) nothing: the cell
                               did not measure the model, so it stays missing
    """
    rec["exported"] = None
    rec["n_rows"] = None
    state = rec.get("state")
    src = os.path.join(sandbox, "answer.json")
    if state == "wrote":
        shutil.copyfile(src, dst)
        rec["exported"] = "answer"
        try:
            with open(src, encoding="utf-8-sig") as fh:
                obj = json.load(fh)
            rows = obj.get(root_key) if isinstance(obj, dict) else obj
            rec["n_rows"] = len(rows) if isinstance(rows, list) else 0
        except Exception as e:
            rec["unparseable"] = "%s: %s" % (type(e).__name__, str(e)[:200])
    elif state in ("never_wrote", "deleted") and rec.get("stop_reason") != "harness_error":
        shutil.copyfile(seed_path, dst)
        rec["exported"] = "seed"
        rec["n_rows"] = 0


PRINT_LOCK = threading.Lock()


def run_cell(a, g, cid, prompt, argv, sandbox_root, timeout_s):
    """One cell: build the sandbox, run the agent, collect. Returns the run.json record."""
    r = C.resolve(g, cid)
    cell = os.path.join(a.out, "cells", g, cid)
    dst = os.path.join(a.out, "answers", g, cid + ".json")
    clear_cell(cell, dst)
    os.makedirs(cell, exist_ok=True)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    # A fresh, randomly named folder per cell, outside the repo: no case id in the
    # agent's working directory, no relative path back to the ground truth, and
    # nothing left over from another cell or an earlier run of this one.
    work = tempfile.mkdtemp(prefix="va_", dir=sandbox_root)
    sandbox = os.path.join(work, "sandbox")
    t0 = time.time()
    # `sandbox` is recorded for the transcript audit: it is the one directory the
    # agent had any business reading.
    rec = {"protocol": "B", "game": g, "case_id": cid, "root_key": r["root_key"],
           "tol_s": C.TOL, "max_turns": a.max_turns or None,
           "timeout_min": a.timeout_min, "agent_cmd": a.agent_cmd,
           "sandbox": sandbox}
    try:
        try:
            # the sandbox: video (renamed), Protocol-B instruction, pre-seeded answer
            os.makedirs(sandbox)
            shutil.copyfile(r["video"], os.path.join(sandbox, "match.mp4"))
            with open(os.path.join(sandbox, "instruction.md"), "w",
                      encoding="utf-8", newline="\n") as fh:
                fh.write(C.instruction_for("agentic", r["instruction"]))
            baseline = seed_answer(sandbox, g, r["root_key"])
            rec.update(baseline)
            # next to the sandbox (not in it); a copy stays with the cell's records
            prompt_file = os.path.join(work, "prompt.txt")
            for pf in (prompt_file, os.path.join(cell, "prompt.txt")):
                with open(pf, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(prompt)
            rec["prompt_sha256"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()

            cmd = [w.replace("@PROMPT_FILE@", prompt_file)
                    .replace("@PROMPT@", prompt)
                    .replace("@SANDBOX@", sandbox)
                   for w in argv]
            with open(os.path.join(cell, "agent_stdout.txt"), "wb") as so, \
                    open(os.path.join(cell, "agent_stderr.txt"), "wb") as se:
                rc = run_agent(cmd, sandbox, so, se, timeout_s)
            rec["exit_code"] = rc
            rec["stop_reason"] = "ok" if rc == 0 else "error"
        except subprocess.TimeoutExpired:
            rec["exit_code"] = None
            rec["stop_reason"] = "timeout"
        except Exception as e:
            rec["stop_reason"] = "harness_error"
            rec["error"] = "%s: %s" % (type(e).__name__, str(e)[:400])

        # Collect whatever the agent left, whether it finished, timed out, or
        # crashed -- a cut-off run's already-written rows still count.
        st = (answer_state(sandbox, rec) if "seed_sha256" in rec
              else {"state": "no_sandbox"})
        rec.update(st)
        try:
            collect(sandbox, rec, r["answer_seed"], dst, r["root_key"])
        except OSError as e:
            rec["collect_error"] = "%s: %s" % (type(e).__name__, str(e)[:200])
        if a.keep_sandbox and os.path.isdir(sandbox):
            shutil.copytree(sandbox, os.path.join(cell, "sandbox"))
    finally:
        if not remove_tree(work):
            rec["sandbox_not_removed"] = work
    rec["wall_s"] = round(time.time() - t0, 1)
    with open(os.path.join(cell, "run.json"), "w", encoding="utf-8") as fh:
        json.dump(rec, fh, ensure_ascii=False, indent=2)
    with PRINT_LOCK:
        print("%-10s %-38s %-13s %-11s export=%-6s n_rows=%s %s"
              % (g, cid, rec.get("stop_reason"), rec.get("state"), rec.get("exported"),
                 rec.get("n_rows"),
                 rec.get("error", rec.get("unparseable", rec.get("collect_error", "")))),
              flush=True)
    return rec


# The agent's toolset, mirroring tools/harbor/Dockerfile. TOOLS are fatal when missing; the
# Python modules and tesseract only warn, because an agent can solve many cells without them.
TOOLS = ("ffmpeg", "ffprobe")
PY_MODULES = ("numpy", "scipy", "pandas", "PIL", "cv2", "skimage", "imageio", "av",
              "matplotlib", "pytesseract", "tqdm")


def preflight():
    """Check this machine has the toolset the prompt says is "already installed".

    Returns a list of fatal problems; prints warnings for the soft ones.
    """
    fatal = ["%s is not on PATH" % t for t in TOOLS if not shutil.which(t)]
    py = shutil.which("python3") or shutil.which("python")
    if not py:
        fatal.append("no python3 (or python) on PATH")
        return fatal
    probe = ("import importlib.util as u; "
             "print(' '.join(m for m in %r if u.find_spec(m) is None))" % (PY_MODULES,))
    try:
        missing = subprocess.run([py, "-c", probe], capture_output=True, text=True,
                                 timeout=60).stdout.split()
    except (OSError, subprocess.SubprocessError):
        missing = list(PY_MODULES)
    if missing:
        print("WARNING: %s lacks Python modules %s; the Harbor image has them "
              "(see tools/harbor/Dockerfile)" % (py, " ".join(missing)), file=sys.stderr)
    if not shutil.which("tesseract"):
        print("WARNING: tesseract is not on PATH; the Harbor image has it", file=sys.stderr)
    return fatal


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent-cmd", required=True,
                    help="command to run in the sandbox; see the placeholders above")
    ap.add_argument("--game", action="append", required=True, help="game (repeatable) or 'all'")
    ap.add_argument("--case", default="all", help="case id, or 'all' (default)")
    ap.add_argument("--limit", type=int, default=0,
                    help="with --case all: only the first N cases of each game (frozen order)")
    ap.add_argument("--out", required=True, help="output root; answers land in <out>/answers")
    ap.add_argument("--max-turns", type=int, default=200,
                    help="turn cap to STATE in the prompt (default 200); pass 0 for a harness "
                         "that cannot count turns, so the prompt claims only the wall clock")
    ap.add_argument("--timeout-min", type=int, default=120,
                    help="wall-clock cap per cell, in minutes (default 120)")
    ap.add_argument("--sandbox-root",
                    help="where the per-cell sandboxes are created (default: the system temp "
                         "dir); must be outside the repo")
    ap.add_argument("--workers", type=int, default=1,
                    help="cells run concurrently, each in its own sandbox (default 1; the "
                         "paper uses 3)")
    ap.add_argument("--skip-preflight", action="store_true",
                    help="do not check this machine for ffmpeg/ffprobe/python3 and the Python "
                         "video stack (e.g. when --agent-cmd runs the agent in its own container)")
    ap.add_argument("--keep-sandbox", action="store_true",
                    help="copy the sandbox into the cell folder afterwards (video included -- "
                         "about 60MB..500MB per cell)")
    a = ap.parse_args()

    games = C.GAMES if "all" in a.game else a.game
    for g in games:
        if g not in C.GAMES:
            ap.error("unknown game %r" % g)
    sandbox_root = os.path.realpath(a.sandbox_root or tempfile.gettempdir())
    repo = os.path.realpath(C.ROOT)
    try:
        inside = os.path.commonpath([sandbox_root, repo]) == repo
    except ValueError:                                  # different drives
        inside = False
    if inside:
        ap.error("--sandbox-root %s is inside the repo: the agent could reach tasks/*/gt/"
                 % sandbox_root)
    if not a.skip_preflight:
        fatal = preflight()
        if fatal:
            ap.error("this machine lacks the agent's toolset: %s. Install it (see the "
                     "docstring, or tools/harbor/Dockerfile), or pass --skip-preflight if the "
                     "agent runs elsewhere" % "; ".join(fatal))
    timeout_s = a.timeout_min * 60
    prompt = build_prompt(a.max_turns or None, timeout_s)
    argv = split_cmd(a.agent_cmd)

    cells = [(g, cid) for g in games
             for cid in ((C.case_ids(g)[:a.limit or None]) if a.case == "all"
                         else [a.case])]
    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as pool:
        list(pool.map(lambda gc: run_cell(a, gc[0], gc[1], prompt, argv, sandbox_root,
                                          timeout_s), cells))

    print("\nanswers in %s\n  python tools/score.py --answers %s"
          % (os.path.join(a.out, "answers"), os.path.join(a.out, "answers")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
