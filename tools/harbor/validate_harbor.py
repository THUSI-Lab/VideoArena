#!/usr/bin/env python3
"""Offline validation of a generated Harbor dataset -- no Docker, no network, no Harbor needed
(Python 3.11+ for tomllib, plus PyYAML).

    python tools/harbor/validate_harbor.py harbor/video-arena [--limit N] [--quick]

Four independent checks per task, for Protocol B and Protocol A datasets alike (the task's
[metadata].protocol says which):

  structure   task.toml parses, the name matches the directory, artifacts/mounts are present
  integrity   tests/judge.py and tests/gt.json are the published ones, the solution is the GT
              projection, environment/ (all the sandbox sees) holds no GT, judge or answer,
              and the instruction is the protocol's own rendering (for A: it also passes the
              Protocol A pre-flight check once the single-reply tail is appended)
  scorer      the verifier scores end to end, through the real judge subprocess: the oracle
              answer scores 1.0, the pre-seeded empty answer 0.0, malformed 0.0, absent 0.0
  harbor      (only when `harbor` is importable) task.toml validates against Harbor's own model

Exit code 1 if anything failed. --limit N stops after N tasks per game; --quick skips the
scorer check, leaving structure + integrity + Harbor model validation.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import va_core as C            # noqa: E402
import score as S              # noqa: E402

FAILS = []
CHECKED = 0


def fail(task, msg):
    FAILS.append("%-34s %s" % (task, msg))


def check_structure(task, d):
    """Shape of the task directory and of task.toml, plus the compose mounts."""
    toml_path = os.path.join(d, "task.toml")
    if not os.path.isfile(toml_path):
        fail(task, "no task.toml")
        return None
    with open(toml_path, "rb") as fh:
        try:
            cfg = tomllib.load(fh)
        except Exception as exc:
            fail(task, "task.toml does not parse: %s" % exc)
            return None
    if cfg.get("task", {}).get("name") != "video-arena/%s" % task:
        fail(task, "task.name is not video-arena/%s" % task)
    if cfg.get("artifacts") != ["/app/answer.json"]:
        fail(task, "artifacts must be exactly ['/app/answer.json']")
    for section, key in (("agent", "timeout_sec"), ("verifier", "timeout_sec"),
                         ("environment", "docker_image"), ("environment", "workdir")):
        if key not in cfg.get(section, {}):
            fail(task, "[%s].%s is missing" % (section, key))
    if cfg.get("verifier", {}).get("environment_mode") != "separate":
        fail(task, "[verifier].environment_mode must be 'separate'")
    proto = cfg.get("metadata", {}).get("protocol")
    if proto not in ("A", "B"):
        fail(task, "[metadata].protocol must be 'A' or 'B', not %r" % proto)
        return None
    for rel in ("instruction.md", "environment/docker-compose.yaml", "tests/test.sh",
                "tests/score_harbor.py", "tests/judge.py", "tests/gt.json",
                "solution/solve.sh", "solution/answer.json") \
            + (("environment/instruction.md",) if proto == "B" else ()):
        if not os.path.isfile(os.path.join(d, rel)):
            fail(task, "missing %s" % rel)
    compose_path = os.path.join(d, "environment/docker-compose.yaml")
    if os.path.isfile(compose_path):
        with open(compose_path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        main = ((doc or {}).get("services") or {}).get("main") or {}
        targets = {v.get("target"): v for v in main.get("volumes") or [] if isinstance(v, dict)}
        mounts = ("/app/match.mp4", "/app/instruction.md") if proto == "B" \
            else ("/app/match.mp4",)
        for target in mounts:
            vol = targets.get(target)
            if not vol or vol.get("type") != "bind" or vol.get("read_only") is not True:
                fail(task, "compose does not bind %s read-only" % target)
        if sorted(targets) != sorted(mounts):
            fail(task, "compose mounts %s, expected exactly %s" % (sorted(targets), mounts))
        game, cid = cfg["metadata"]["game"], cfg["metadata"]["case_id"]
        src = (targets.get("/app/match.mp4") or {}).get("source", "")
        if not src.endswith("/%s/media/%s.mp4" % (game, cid)):
            fail(task, "compose mounts the wrong render: %s" % src)
        env_net = cfg.get("environment", {}).get("network_mode")
        agent_net = cfg.get("agent", {}).get("network_mode")
        if env_net != "no-network" and agent_net != "no-network" \
                and main.get("network_mode") != "none":
            fail(task, "nothing keeps the agent offline: neither Harbor nor Docker isolates it")
    return cfg


def check_integrity(task, d, cfg):
    """The published files are copied unchanged, and nothing secret reached the sandbox."""
    game = cfg["metadata"]["game"]
    cid = cfg["metadata"]["case_id"]
    meta = C.resolve(game, cid)
    with open(os.path.join(d, "tests/judge.py"), "rb") as a, open(meta["judge"], "rb") as b:
        if a.read() != b.read():
            fail(task, "tests/judge.py differs from tasks/%s/judge.py" % game)
    with open(os.path.join(d, "tests/gt.json"), encoding="utf-8") as a, \
            open(meta["gt"], encoding="utf-8") as b:
        if json.load(a) != json.load(b):
            fail(task, "tests/gt.json differs from the published ground truth")

    # The sandbox sees environment/ (compose, plus the instruction under B) and nothing else.
    proto = cfg["metadata"]["protocol"]
    expected = ["docker-compose.yaml", "instruction.md"] if proto == "B" \
        else ["docker-compose.yaml"]
    sandbox = sorted(os.listdir(os.path.join(d, "environment")))
    if sandbox != expected:
        fail(task, "environment/ must hold only %s: %s" % (" and ".join(expected), sandbox))

    if proto == "B":
        want = C.instruction_for("agentic", meta["instruction"]).replace("\r\n", "\n")
        with open(os.path.join(d, "environment/instruction.md"), "rb") as fh:
            if fh.read().decode("utf-8") != want:
                fail(task, "environment/instruction.md is not the Protocol B instruction")
    else:
        want = C.instruction_for("video", meta["instruction"]).replace("\r\n", "\n")
        with open(os.path.join(d, "instruction.md"), "rb") as fh:
            got = fh.read().decode("utf-8")
        if got != want:
            fail(task, "instruction.md is not the Protocol A instruction")
        try:            # what the agent will send, on a dummy two-frame grid
            C.protocol_a_prompt(got, 2.0, [0.0, 1.0], 1.0, meta["root_key"])
        except ValueError as exc:
            fail(task, "Protocol A pre-flight: %s" % exc)

    with open(meta["gt"], encoding="utf-8") as fh:
        oracle = S.oracle_answer(game, json.load(fh))
    with open(os.path.join(d, "solution/answer.json"), encoding="utf-8") as fh:
        if json.load(fh) != oracle:
            fail(task, "solution/answer.json is not the GT projected onto the answer schema")
    return oracle


def check_scorer(task, d, oracle):
    """Run the verifier scoring path for real, in a temp tree that mocks the container paths."""
    tmp = tempfile.mkdtemp(prefix="va-harbor-check-")
    try:
        tests = os.path.join(tmp, "tests")
        shutil.copytree(os.path.join(d, "tests"), tests)
        runner = os.path.join(tests, "score_harbor.py")
        with open(runner, encoding="utf-8") as fh:
            src = fh.read()
        logs = os.path.join(tmp, "logs", "verifier").replace("\\", "/")
        answer = os.path.join(tmp, "app", "answer.json").replace("\\", "/")
        src = src.replace("/logs/verifier", logs).replace("/app/answer.json", answer)
        with open(runner, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(src)
        os.makedirs(os.path.dirname(answer), exist_ok=True)
        reward_json = os.path.join(logs, "reward.json")

        with open(os.path.join(tests, "case.json"), encoding="utf-8") as fh:
            root = json.load(fh)["root_key"]
        cases = [
            ("oracle", json.dumps(oracle), 1.0),
            ("empty seed", json.dumps({root: []}), 0.0),
            ("malformed", '{"events": [', 0.0),
            ("wrong type", json.dumps([1, 2, 3]), 0.0),
            ("absent", None, 0.0),
        ]
        for label, payload, want in cases:
            if payload is None:
                if os.path.exists(answer):
                    os.remove(answer)
            else:
                with open(answer, "w", encoding="utf-8") as fh:
                    fh.write(payload)
            proc = subprocess.run([sys.executable, runner], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
            if proc.returncode != 0:
                fail(task, "scorer (%s) exited %s: %s"
                     % (label, proc.returncode, proc.stderr.strip()[-200:]))
                continue
            try:
                with open(reward_json, encoding="utf-8") as fh:
                    got = json.load(fh)
            except Exception as exc:
                fail(task, "scorer (%s) wrote no usable reward.json: %s" % (label, exc))
                continue
            if sorted(got) != ["reward", "t1_f1", "t2_macro_acc", "t3_macro_acc"]:
                fail(task, "scorer (%s) reward keys are %s" % (label, sorted(got)))
            if abs(float(got["reward"]) - want) > 1e-9:
                fail(task, "scorer (%s) reward %s, expected %s" % (label, got["reward"], want))
            if label == "oracle" and any(float(v) != 1.0 for v in got.values()):
                fail(task, "oracle did not reach 1.0 on every tier: %s" % got)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_harbor_model(task, d):
    """Validate task.toml against Harbor's own model, when Harbor is installed here."""
    try:
        from harbor.models.task.config import TaskConfig
    except Exception:
        return "skip"
    try:
        with open(os.path.join(d, "task.toml"), encoding="utf-8") as fh:
            cfg = TaskConfig.model_validate_toml(fh.read())
        if cfg.task.name != "video-arena/%s" % task:
            fail(task, "Harbor model disagrees with the task name")
    except Exception as exc:
        fail(task, "Harbor TaskConfig rejected task.toml: %s" % exc)
    return "checked"


def main():
    ap = argparse.ArgumentParser(description="Validate a generated Harbor Video-Arena dataset.")
    ap.add_argument("dataset", help="dataset directory written by tools/make_harbor.py")
    ap.add_argument("--limit", type=int, default=0, help="first N tasks per game")
    ap.add_argument("--quick", action="store_true", help="skip the (slow) scorer check")
    args = ap.parse_args()

    dataset = os.path.abspath(args.dataset)
    tasks = sorted(name for name in os.listdir(dataset)
                   if os.path.isdir(os.path.join(dataset, name)))
    if not tasks:
        raise SystemExit("no tasks in %s" % dataset)

    per_game = {}
    harbor_skipped = False
    global CHECKED
    for task in tasks:
        game = task.split("-")[0]
        per_game[game] = per_game.get(game, 0) + 1
        if args.limit and per_game[game] > args.limit:
            continue
        d = os.path.join(dataset, task)
        cfg = check_structure(task, d)
        if cfg is None:
            continue
        oracle = check_integrity(task, d, cfg)
        if not args.quick and oracle is not None:
            check_scorer(task, d, oracle)
        if check_harbor_model(task, d) == "skip":
            harbor_skipped = True
        CHECKED += 1

    print("%d tasks checked in %s (%s)"
          % (CHECKED, dataset, ", ".join("%s=%d" % kv for kv in sorted(per_game.items()))))
    if harbor_skipped:
        print("note: harbor is not importable here; task.toml was checked with tomllib only")
    if FAILS:
        print("\n%d FAILURE(S):" % len(FAILS))
        for line in FAILS[:60]:
            print("  " + line)
        if len(FAILS) > 60:
            print("  ... and %d more" % (len(FAILS) - 60))
        raise SystemExit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
