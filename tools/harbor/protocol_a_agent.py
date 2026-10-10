# -*- coding: utf-8 -*-
"""Harbor agent for Protocol A: one request, the sampled frames, no tools.

    PYTHONPATH=tools/harbor harbor run -p harbor/video-arena-a \
        -a protocol_a_agent:ProtocolA -m openai/<model> -n 5 \
        --ak reasoning_effort=high          # or: --ak enable_thinking=true (Qwen on vLLM)

The model never sees the container. The agent samples the paper's grid with ffmpeg INSIDE the
offline task sandbox (the same commands as tools/run_protocol_a.py), copies the frames out,
builds the prompt with va_core.protocol_a_prompt() and makes the one OpenAI-compatible
/chat/completions call from the host. The extracted answer -- or the empty seed for a reply
without one, which scores 0.0 like the native runner -- is written to /app/answer.json, and
Harbor's verifier scores it exactly as under Protocol B.

Endpoint: -m openai/<model> with OPENAI_API_KEY and OPENAI_BASE_URL (or --ae KEY=VALUE).
Per trial, the agent logs hold run.json, prompt.txt and reply.txt.
"""
import asyncio
import json
import os
import shlex
import sys
import tempfile
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))                    # tools/
import run_protocol_a as RA  # noqa: E402
import va_core as C  # noqa: E402

from harbor.agents.base import BaseAgent  # noqa: E402
from harbor.agents.model_connection import ModelConnectionSpec  # noqa: E402
from harbor.agents.options import AgentOptions  # noqa: E402

VIDEO = "/app/match.mp4"
ANSWER = "/app/answer.json"
FRAMES = "/tmp/va_frames"
ROOT_KEY = "events"         # every game's answer_seed.json: {"events": []}


class ProtocolAOptions(AgentOptions):
    max_frames: int = RA.MAX_FRAMES
    jpeg_q: int = RA.JPEG_Q                 # raise for a provider's request-size cap
    max_tokens: int = 48000
    temperature: float = 0.0
    reasoning_effort: str | None = None
    enable_thinking: bool | None = None     # sent as chat_template_kwargs (Qwen on vLLM)
    timeout: int = 1800


class ProtocolA(BaseAgent):
    options_model = ProtocolAOptions
    MODEL_CONNECTION = ModelConnectionSpec(default_provider="openai")

    @staticmethod
    def name():
        return "video-arena-protocol-a"

    def version(self):
        return "1.0.0"

    async def setup(self, environment):
        pass

    async def _sh(self, environment, argv, timeout=600):
        res = await environment.exec(shlex.join(argv), timeout_sec=timeout)
        if res.return_code != 0:
            raise RuntimeError("%s failed (%s): %s"
                               % (argv[0], res.return_code, (res.stderr or "")[-400:]))
        return (res.stdout or "").strip()

    async def run(self, instruction, environment, context):
        o = self.options
        conn = self.model_connection
        model = self.model_name.split("/", 1)[1] if "/" in self.model_name else self.model_name
        root_key = ROOT_KEY
        extra = ({"chat_template_kwargs": {"enable_thinking": o.enable_thinking}}
                 if o.enable_thinking is not None else None)
        logs = Path(self.logs_dir)
        logs.mkdir(parents=True, exist_ok=True)
        rec = {"protocol": "A", "model": model, "root_key": root_key, "tol_s": C.TOL,
               "sampling": {"target_fps": RA.TARGET_FPS, "max_frames": o.max_frames,
                            "max_width": RA.MAX_WIDTH, "jpeg_q": o.jpeg_q},
               "max_tokens": o.max_tokens, "temperature": o.temperature,
               "reasoning_effort": o.reasoning_effort, "extra_body": extra}
        t0 = time.time()
        answer = None
        try:
            # the grid, sampled in the sandbox with the native runner's own commands
            dur = float((await self._sh(environment, RA.probe_args(
                VIDEO, "format=duration", "ffprobe"))).splitlines()[0] or 0.0)
            native = RA.parse_rate((await self._sh(environment, RA.probe_args(
                VIDEO, "stream=r_frame_rate", "ffprobe"))).splitlines()[0])
            fps = RA.grid_fps(dur, native, o.max_frames)
            await self._sh(environment, ["mkdir", "-p", FRAMES])
            await self._sh(environment, RA.ffmpeg_args(
                VIDEO, FRAMES + "/f_%05d.jpg", fps, "ffmpeg", o.jpeg_q), timeout=1800)
            fdir = logs / "frames"
            await environment.download_dir(FRAMES, fdir)
            paths = sorted(str(p) for p in fdir.glob("f_*.jpg"))[:o.max_frames]
            ts = RA.grid_ts(len(paths), fps)
            rec.update(n_frames=len(paths), fps=round(fps, 6), duration_s=round(dur, 2),
                       t1_recall_ceiling=round(C.t1_recall_ceiling(dur, len(paths)), 4))

            prompt = C.protocol_a_prompt(instruction, dur, ts, fps, root_key)
            (logs / "prompt.txt").write_text(prompt, encoding="utf-8")
            reply, meta = await asyncio.to_thread(
                RA.chat, conn.base_url, conn.api_key or "", model, prompt, paths,
                o.max_tokens, o.temperature, o.timeout, o.reasoning_effort, extra)
            rec.update(meta)
            (logs / "reply.txt").write_text(reply, encoding="utf-8")
            rec["n_json_blocks"] = len(C.answer_candidates(reply, root_key))
            try:
                answer = C.extract_answer(reply, root_key)
            except ValueError as e:
                # a reply without an answer object is the model's result: the seed, 0.0
                rec.update(status="no_answer", error="%s: %s" % (type(e).__name__, str(e)[:400]))
            else:
                rows = answer.get(root_key)
                rec.update(status="ok", n_rows=len(rows) if isinstance(rows, list) else 0)
            usage = meta.get("usage") or {}
            context.n_input_tokens = usage.get("prompt_tokens")
            context.n_output_tokens = usage.get("completion_tokens")
        except Exception as e:
            rec.update(status="error", error="%s: %s" % (type(e).__name__, str(e)[:400]))
            raise
        finally:
            rec["wall_s"] = round(time.time() - t0, 1)
            (logs / "run.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2),
                                           encoding="utf-8")
            context.metadata = {k: rec.get(k) for k in
                                ("status", "n_frames", "n_rows", "n_json_blocks",
                                 "finish_reason", "duration_s", "t1_recall_ceiling")}

        if answer is not None:      # otherwise /app/answer.json stays the image's empty seed
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                             encoding="utf-8") as fh:
                json.dump(answer, fh, ensure_ascii=False)
            try:
                await environment.upload_file(fh.name, ANSWER)
            finally:
                os.unlink(fh.name)
