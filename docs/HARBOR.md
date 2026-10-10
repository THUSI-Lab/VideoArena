# Video-Arena on Harbor

Both protocols packaged as [Harbor](https://github.com/harbor-framework/harbor) datasets: 180
tasks each, one per case, scored by the same `judge.py` files as `tools/score.py`. Protocol B
(agentic, the default) is described first; Protocol A (one request, no tools) reuses the same
image, verifier and reward keys, see [Protocol A on Harbor](#protocol-a-on-harbor).

## What a trial is

| | |
|---|---|
| **Sandbox** | the prebuilt image `video-arena/base` (`tools/harbor/Dockerfile`), workdir `/app`, holding exactly the three Protocol B files: `match.mp4` and `instruction.md` (both mounted read-only) and `answer.json`, the empty seed `{"events": []}`, writable. The agent runs as the image's unprivileged user `agent` (`[agent].user`); the verifier runs as root |
| **Prompt** | the task's `instruction.md`: the Protocol B turn-1 prompt, pointing at `/app` |
| **Budget** | 200 tool-call turns (the agent's own flag, see below) and 7200 s of wall clock (`[agent].timeout_sec`) |
| **Network** | none, see [Network modes](#network-modes) |
| **Scoring** | `/app/answer.json` is the only artifact collected. A **separate** verifier container (same image) receives `tests/`, which holds the judge and the ground truth, and writes `reward.json` |

The agent's container never holds the ground truth, the judge or the solution: they exist only in
the verifier container. An agent that runs out of time is still scored on whatever `answer.json`
holds at the limit. A missing or unparseable answer scores 0.0 and stays in the mean, exactly as
in `score.py`.

**Reward keys** (`/logs/verifier/reward.json`): `reward` (the strict-tuple headline),
`t1_f1`, `t2_macro_acc`, `t3_macro_acc`. A tier the judge did not report is recorded as 0.0,
because Harbor accepts only finite numbers.

**Pre-installed in the image**, because nothing can be installed once the trial is offline:

- `ffmpeg`/`ffprobe` and `tesseract` (English)
- Python 3.11 with numpy, scipy, pandas, Pillow, OpenCV (headless), scikit-image, imageio,
  PyAV, matplotlib, pytesseract and tqdm
- `tmux`, needed by Harbor's terminal agents
- the usual shell tools: `jq`, `bc`, `file`, `less`, `vim`

It holds no game engines, no engine weights and no ground truth.

## Running it

All commands run on a Linux Docker host that has a checkout of this repo, `tasks/*/media`
included.

```bash
# 1. the sandbox image (for a cluster, push it to a registry and pass --base-image in step 2)
docker build -t video-arena/base:latest tools/harbor

# 2. the dataset: a few MB of task folders, no video; then an offline self-check
#    (the check needs Python 3.11+ and PyYAML, no Docker, about 2 minutes)
python tools/make_harbor.py --out harbor/video-arena
python tools/harbor/validate_harbor.py harbor/video-arena

# 3. where the renders are: each task mounts $VA_MEDIA_ROOT/<game>/media/<case_id>.mp4
export VA_MEDIA_ROOT="$PWD/tasks"

# 4. the oracle must score 1.0 on all 180 trials
harbor run -p harbor/video-arena -a oracle

# 5. a model
harbor run -p harbor/video-arena -a terminus-2 -m <provider/model> --ak max_turns=200 -n 4
```

If `VA_MEDIA_ROOT` is unset, Compose refuses to start the trial. It does not start it without
the video. To pin an absolute path into the compose files instead, generate with
`--host-media /abs/path/to/tasks`. Each trial gets 4 CPUs and 8 GB of RAM.

## Network modes

`make_harbor.py --net` chooses how "offline" is enforced:

| `--net` | Harbor `[environment]` / `[agent]` | compose `network_mode: none` | for |
|---|---|---|---|
| `strict` (default) | `no-network` / `no-network` | yes | agents whose model calls leave from the host: `terminus-2`, `oracle` |
| `docker` | `public` / unset | yes | the same, on Docker hosts without Harbor egress control |
| `phase` | `public` / `no-network` | no | installed agents that run inside the container: `claude-code`, `codex`, ... |

- **`strict`** has two layers. Docker gives the container no interface but loopback, and Harbor's
  no-network policy applies on top. Harbor enforces that policy through its egress-control
  sidecar, which needs nftables support on the Docker host. If its kernel probe fails, Harbor
  **rejects** the trial rather than running it open. Use `docker` on such hosts.
- **`docker`** builds the same sandbox, with Docker as the only isolation layer. No probe is
  involved.
- **`phase`** is for agents that need the model API from inside the sandbox. The network is open
  only while the agent installs itself. During the run the sandbox is `no-network`, and the only
  hosts it can reach are the ones you allow:

  ```bash
  python tools/make_harbor.py --out harbor/video-arena-phase --net phase
  harbor run -p harbor/video-arena-phase -a claude-code -m <model> --ak max_turns=200 \
      --allow-agent-host api.anthropic.com
  ```

  This mode needs Harbor egress control, like `strict`.

  With `ANTHROPIC_BASE_URL` set (a gateway or a self-hosted model), `claude-code` passes `-m`
  through verbatim, so give the bare model id the endpoint expects (`-m my-model`, not
  `-m anthropic/my-model`).

## Protocol A on Harbor

```bash
python tools/make_harbor.py --out harbor/video-arena-a --protocol A     # --net strict
python tools/harbor/validate_harbor.py harbor/video-arena-a
PYTHONPATH=tools/harbor harbor run -p harbor/video-arena-a \
    -a protocol_a_agent:ProtocolA -m openai/<model> -n 5 --ak reasoning_effort=high
```

The model never sees the container. The agent (`tools/harbor/protocol_a_agent.py`) samples the
paper's grid with ffmpeg *inside* the offline sandbox, using the same commands as
`run_protocol_a.py`, copies the frames out, builds the prompt with
`va_core.protocol_a_prompt()` and makes the one OpenAI-compatible `/chat/completions` call from
the host (`OPENAI_API_KEY` and `OPENAI_BASE_URL`). The extracted answer is written to
`/app/answer.json`; a reply without one leaves the empty seed, which scores 0.0. The verifier and
reward keys are those of Protocol B.

Agent options (`--ak`): `reasoning_effort`, `enable_thinking` (sent as `chat_template_kwargs`,
for a Qwen model on vLLM), `max_frames`, `jpeg_q`, `max_tokens` (48000), `temperature` (0),
`timeout`. Each trial's agent logs hold `run.json`, `prompt.txt`, `reply.txt` and the frames.

A Protocol A task differs from a Protocol B task in three places: `instruction.md` is the
A-rendered game instruction (the agent appends the frame timestamps and the single-reply
tail), `environment/` mounts only `match.mp4`, and `[agent].timeout_sec` is 3600.

## Differences from `tools/run_protocol_b.py`

- **No network is enforced, not only audited.** `run_protocol_b.py` runs your agent on your
  machine, so the README's rule there is "audited, not enforced". Here the sandbox has no route
  out (in `strict` and `docker`), or only a route to the model API (in `phase`). The transcript
  audit in `docs/PROTOCOLS.md` still applies to what the agent asks its model.
- **Fixed `/app` instead of a random temp folder.** The random path exists to keep the ground
  truth out of reach. Here the ground truth is not in the agent's container at all.
- **Turn cap.** As in the reference runner, the turn cap comes from the agent's own flag:
  `--ak max_turns=200` for `terminus-2` and `claude-code`. A `terminus-2` turn is one model
  reply, which may batch several commands. Do not use `--agent-timeout-multiplier` or
  `--timeout-multiplier` for reported numbers: they change the budget.
- **No `wrote` / `never_wrote` / `deleted` status.** Harbor records only the reward. All three
  cases score 0.0, as in `score.py`.
- **Aggregation.** Report the mean of the nine per-game means, with n, as in "Reporting a number"
  in the README. A trial that ends in a harness exception (as opposed to an agent timeout) is
  `missing`, not 0.0.

## A task folder

```
<game>-<case_id>/
    task.toml                  Harbor config: metadata, budget, image, network, artifacts
    instruction.md             B: the agent's prompt; A: instruction_for("video")
    environment/
        docker-compose.yaml    the image + read-only mounts of match.mp4 (and, B, instruction.md)
        instruction.md         B only: the game's task text (instruction_for("agentic"))
    tests/                     verifier only
        test.sh  score_harbor.py  judge.py  gt.json  case.json
    solution/                  oracle only
        solve.sh  answer.json  (the ground truth projected onto the answer schema)
```

The dataset is generated, not committed (`harbor/` is in `.gitignore`). Regenerate it with
`make_harbor.py --force` after any change under `tasks/`.
