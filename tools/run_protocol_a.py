#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_protocol_a.py -- minimal Protocol A (video understanding) reference runner.

Protocol A is ONE chat call. The video is reduced to a uniform grid of JPEG frames, the
frames and the instruction go into a single user turn, and the model's reply must contain
one JSON object. No tools, no filesystem, no second turn.

    export VA_API_KEY=...        # or --api-key
    python tools/run_protocol_a.py --model gpt-5.6 --base-url https://api.example.com/v1 \
        --game chess --case all --out runs/gpt56-A --reasoning-effort high
    python tools/score.py --answers runs/gpt56-A/answers

Requires: ffmpeg + ffprobe on PATH (frame sampling), and an OpenAI-compatible
/chat/completions endpoint that accepts image_url parts. Nothing else -- the HTTP call is
urllib, so there is no SDK to pin.

This is a REFERENCE implementation: it shows the measurement, not our production harness.
It has no retry policy, no rate limiting and no cost accounting; --workers runs cells
concurrently (the paper uses 5 per system for Protocol A). What it
does have is the part that must not vary between systems -- the sampling grid, the single
turn, and the answer extraction -- because those are the benchmark, and a driver that
changes them is measuring something else.

    THE SAMPLING GRID (one grid, every model, every game):
        1 FPS, capped at 256 frames spread uniformly over the whole match, longest side
        768px, never upscaled.

    1 FPS because the board or map is idle most of the time (denser sampling returns
    duplicate stills); one shared cap so every row of a results table is the same
    measurement. Where a provider's image limit is below 256, degrade and RECORD it
    (run.json carries n_frames). Where its request-size cap is the binding limit (256 busy
    frames at -q:v 4 can exceed 30 MB once base64-encoded), raise --jpeg-q instead and keep
    the 256 frames; run.json carries sampling.jpeg_q. No upscaling: several boards render
    below 768px (go 612, chess 548, gomoku 504).

THE PROMPT is va_core.protocol_a_prompt(): the A-rendered instruction, then the paper's
single-reply tail, which hands over the grid's explicit video_ts array. The Harbor agent
(tools/harbor/protocol_a_agent.py) builds it with the same function, on the same grid.

On a long match the grid itself caps T1 recall at min(1, 2*TOL/spacing), independent of the
model; run.json carries t1_recall_ceiling for every cell.

WHAT A CELL EXPORTS. A reply that arrived but holds no answer object (empty, prose only, or
cut off before the object closed) is the model's result: the empty answer seed is exported
for it, so it scores 0.0 and stays in the mean, with status "no_answer" in run.json. A call
that never produced a reply (ffmpeg, HTTP, timeout) exports nothing and stays missing -- it
measured the endpoint, not the model. A re-run of a cell first deletes the answer, run.json
and reply an earlier run left, so a failed re-run is never scored on its predecessor.
"""
import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import va_core as C  # noqa: E402

TARGET_FPS = 1.0
MAX_FRAMES = 256
MAX_WIDTH = 768
JPEG_Q = 4                      # ffmpeg -q:v, 2 (best) .. 31 (worst)

FFMPEG = os.environ.get("VA_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("VA_FFPROBE", "ffprobe")

# The grid helpers below are pure argument builders where possible: the Harbor agent runs the
# same ffprobe/ffmpeg commands inside the task container, so both paths sample identically.
def probe_args(path, entries, ffprobe=FFPROBE):
    return [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", entries,
            "-of", "csv=p=0", path]


def probe(path, entries):
    out = subprocess.check_output(probe_args(path, entries), text=True).strip()
    return out.splitlines()[0] if out else ""


def video_duration(path):
    return float(probe(path, "format=duration").strip() or 0.0)


def parse_rate(rate):
    """ffprobe's r_frame_rate ("30000/1001") as a float; 0.0 when unreadable."""
    num, _, den = rate.strip().partition("/")
    try:
        d = float(den) if den else 1.0
        return float(num) / d if d else 0.0
    except ValueError:
        return 0.0


def video_fps(path):
    return parse_rate(probe(path, "stream=r_frame_rate"))


def grid_fps(dur, native_fps, max_frames=MAX_FRAMES):
    """The grid's rate: min(TARGET_FPS, max_frames/duration), additionally bounded by the
    container rate, so ffmpeg is never asked to fabricate frames a source does not present
    (chess renders natively at 1 FPS)."""
    fps = min(TARGET_FPS, max_frames / dur) if dur > 0 else TARGET_FPS
    if native_fps and fps > native_fps:
        fps = native_fps
    return fps


def ffmpeg_args(video, pattern, fps, ffmpeg=FFMPEG, jpeg_q=JPEG_Q):
    vf = "fps=%.6f,scale='min(%d,iw)':-1" % (fps, MAX_WIDTH)
    return [ffmpeg, "-y", "-loglevel", "error", "-i", video, "-vf", vf,
            "-q:v", str(jpeg_q), pattern]


def grid_ts(n, fps):
    """video_ts of the n kept frames: frame k is at k / fps."""
    return [round(i / fps, 2) for i in range(n)]


def sample_frames(video, out_dir, max_frames=MAX_FRAMES, jpeg_q=JPEG_Q):
    """Uniform grid -> (paths, timestamps, fps, duration). See the module docstring for the
    policy. The hard clamp after extraction is not redundant: ffmpeg's fps filter overshoots
    by a frame or two on rounding, and providers enforce their image cap inclusively.
    """
    os.makedirs(out_dir, exist_ok=True)
    dur = video_duration(video)
    fps = grid_fps(dur, video_fps(video), max_frames)
    subprocess.run(ffmpeg_args(video, os.path.join(out_dir, "f_%05d.jpg"), fps,
                               jpeg_q=jpeg_q), check=True)
    paths = [os.path.join(out_dir, f) for f in sorted(os.listdir(out_dir))
             if f.startswith("f_") and f.endswith(".jpg")]
    if len(paths) > max_frames:
        paths = paths[:max_frames]
    return paths, grid_ts(len(paths), fps), fps, dur


def data_uri(path):
    with open(path, "rb") as fh:
        return "data:image/jpeg;base64," + base64.b64encode(fh.read()).decode()


def chat(base_url, api_key, model, text, frame_paths, max_tokens, temperature, timeout,
         reasoning_effort=None, extra_body=None):
    """One OpenAI-compatible /chat/completions call. Returns (reply_text, usage_dict).
    extra_body is merged into the request as-is (e.g. a vLLM server's chat_template_kwargs)."""
    content = [{"type": "text", "text": text}]
    for p in frame_paths:
        content.append({"type": "image_url", "image_url": {"url": data_uri(p)}})
    req = {"model": model, "messages": [{"role": "user", "content": content}],
           "max_tokens": max_tokens, "temperature": temperature}
    if reasoning_effort:
        req["reasoning_effort"] = reasoning_effort
    req.update(extra_body or {})
    body = json.dumps(req).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:     # keep the provider's reason, not just the code
        raise RuntimeError("HTTP %s from the endpoint (request %.1f MB): %s"
                           % (e.code, len(body) / 1e6,
                              e.read()[:300].decode("utf-8", "replace"))) from None
    choice = (out.get("choices") or [{}])[0]
    return (choice.get("message") or {}).get("content") or "", {
        "usage": out.get("usage"), "finish_reason": choice.get("finish_reason")}


PRINT_LOCK = threading.Lock()


def run_cell(a, g, cid):
    """One cell: sample, ask, extract, export. Never raises for a model or endpoint failure;
    run.json records it."""
    r = C.resolve(g, cid)
    cell = os.path.join(a.out, "cells", g, cid)
    dst = os.path.join(a.out, "answers", g, cid + ".json")
    # nothing an earlier run of this cell left may be read as this run's result
    for p in (dst, os.path.join(cell, "run.json"), os.path.join(cell, "reply.txt")):
        if os.path.isfile(p):
            os.remove(p)
    os.makedirs(cell, exist_ok=True)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    fdir = os.path.join(cell, "frames") if a.keep_frames \
        else tempfile.mkdtemp(prefix="va_frames_")
    t0 = time.time()
    rec = {"protocol": "A", "game": g, "case_id": cid, "model": a.model,
           "root_key": r["root_key"], "tol_s": C.TOL,
           "sampling": {"target_fps": TARGET_FPS, "max_frames": a.max_frames,
                        "max_width": MAX_WIDTH, "jpeg_q": a.jpeg_q},
           "max_tokens": a.max_tokens, "temperature": a.temperature,
           "reasoning_effort": a.reasoning_effort, "extra_body": a.extra_body}
    try:
        paths, ts, fps, dur = sample_frames(r["video"], fdir, a.max_frames, a.jpeg_q)
        rec.update(n_frames=len(paths), fps=round(fps, 6), duration_s=round(dur, 2),
                   t1_recall_ceiling=round(C.t1_recall_ceiling(dur, len(paths)), 4))
        prompt = C.protocol_a_prompt(C.instruction_for("video", r["instruction"]),
                                     dur, ts, fps, r["root_key"])
        reply, meta = chat(a.base_url, a.api_key, a.model, prompt, paths,
                           a.max_tokens, a.temperature, a.timeout,
                           a.reasoning_effort, a.extra_body)
        rec.update(meta)
        with open(os.path.join(cell, "reply.txt"), "w", encoding="utf-8") as fh:
            fh.write(reply)
        cands = C.answer_candidates(reply, r["root_key"])
        rec["n_json_blocks"] = len(cands)
        try:
            obj = C.extract_answer(reply, r["root_key"])
        except ValueError as e:
            # The model replied without an answer object: its result, scored 0.0
            # via the empty seed -- not a missing cell.
            shutil.copyfile(r["answer_seed"], dst)
            rec.update(status="no_answer", n_rows=0, answer_exported="seed",
                       error="%s: %s" % (type(e).__name__, str(e)[:400]))
        else:
            rows = obj.get(r["root_key"])
            rec["n_rows"] = len(rows) if isinstance(rows, list) else 0
            with open(dst, "w", encoding="utf-8") as fh:
                json.dump(obj, fh, ensure_ascii=False)
            rec.update(status="ok", answer_exported="reply")
    except Exception as e:
        # Record WHY, per cell. A bare 0.0 cannot be told apart from a refusal, a
        # truncation, or a model that answered in prose -- and those call for
        # different fixes.
        rec["status"] = "error"
        rec["error"] = "%s: %s" % (type(e).__name__, str(e)[:400])
    finally:
        if not a.keep_frames:
            shutil.rmtree(fdir, ignore_errors=True)
    rec["wall_s"] = round(time.time() - t0, 1)
    with open(os.path.join(cell, "run.json"), "w", encoding="utf-8") as fh:
        json.dump(rec, fh, ensure_ascii=False, indent=2)
    with PRINT_LOCK:
        print("%-10s %-38s %-9s n_frames=%s n_rows=%s %s"
              % (g, cid, rec["status"], rec.get("n_frames"), rec.get("n_rows"),
                 rec.get("error", "")), flush=True)
    return rec


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True, help="model id the endpoint expects")
    ap.add_argument("--base-url", required=True, help="OpenAI-compatible base, ending in /v1")
    ap.add_argument("--api-key", default=os.environ.get("VA_API_KEY", ""))
    ap.add_argument("--game", action="append", required=True,
                    help="game to run (repeatable), or 'all'")
    ap.add_argument("--case", default="all", help="case id, or 'all' (default)")
    ap.add_argument("--limit", type=int, default=0,
                    help="with --case all: only the first N cases of each game (frozen order)")
    ap.add_argument("--out", required=True, help="output root; answers land in <out>/answers")
    ap.add_argument("--max-frames", type=int, default=MAX_FRAMES,
                    help="degrade the 256-frame cap for a provider that rejects it; "
                         "recorded in run.json so the cell is readable as degraded")
    ap.add_argument("--jpeg-q", type=int, default=JPEG_Q,
                    help="ffmpeg -q:v for the frames (default %d; higher = smaller). Raise it "
                         "for a provider whose request-size cap rejects the 256-frame grid; "
                         "recorded in run.json" % JPEG_Q)
    ap.add_argument("--max-tokens", type=int, default=48000,
                    help="output budget; a long match needs a large one -- a truncated "
                         "reply loses the tail of the answer, not just its formatting")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--reasoning-effort", default=None,
                    help="sent as reasoning_effort when set; the paper uses high for the GPT, "
                         "Claude and Gemini models and the provider default otherwise")
    ap.add_argument("--extra-body", type=json.loads, default=None,
                    help="JSON object merged into the request, e.g. "
                         "'{\"chat_template_kwargs\": {\"enable_thinking\": true}}' for a "
                         "Qwen model on vLLM; recorded in run.json")
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--workers", type=int, default=1,
                    help="cells run concurrently (default 1; the paper uses 5)")
    ap.add_argument("--keep-frames", action="store_true", help="keep the sampled JPEGs")
    a = ap.parse_args()

    if not a.api_key:
        ap.error("--api-key or VA_API_KEY is required")
    for tool in (FFMPEG, FFPROBE):
        if not shutil.which(tool):
            ap.error("%s not found on PATH (set VA_FFMPEG / VA_FFPROBE)" % tool)
    games = C.GAMES if "all" in a.game else a.game
    for g in games:
        if g not in C.GAMES:
            ap.error("unknown game %r" % g)

    cells = [(g, cid) for g in games
             for cid in ((C.case_ids(g)[:a.limit or None]) if a.case == "all"
                         else [a.case])]
    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as pool:
        list(pool.map(lambda gc: run_cell(a, *gc), cells))

    print("\nanswers in %s\n  python tools/score.py --answers %s"
          % (os.path.join(a.out, "answers"), os.path.join(a.out, "answers")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
