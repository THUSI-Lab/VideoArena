# The two protocols

Video-Arena asks the same 180 cases twice. The **task** is identical; only the **delivery**
differs. Protocol A measures video understanding in a single pass; Protocol B measures agentic
tool use over many turns. Reporting them side by side is the point, so anything that differs
beyond delivery breaks the comparison.

---

## What is shared

**One instruction per game, rendered per protocol.** `tasks/<game>/instruction.md` carries the
event vocabulary, coordinate and zone conventions, the $V_t$ definition with its dead band, the
row schema, and the ±3 s matching rule. That is 70–85% of each file and it is byte-identical
across the two protocols — otherwise the two result tables are not the same question asked twice.

The remaining lines are delivery, and they are *marked* rather than duplicated into two files:

```
<!-- B-ONLY:START -->  ... whole lines ...  <!-- B-ONLY:END -->      agentic only
<!-- A-ONLY:START -->  ... whole lines ...  <!-- A-ONLY:END -->      video only
<!--B-->inline span<!--/B-->        <!--A-->inline span<!--/A-->
```

Always send `va_core.instruction_for(protocol, path)`, never the raw file:

```python
import va_core as C
r = C.resolve("chess", "cZWtZEBE")
text = C.instruction_for("video",   r["instruction"])   # Protocol A
text = C.instruction_for("agentic", r["instruction"])   # Protocol B
```

It fails closed. A surviving marker means the file is malformed, and raising beats sending the
other protocol's delivery text — or a raw HTML comment — to a model.

> **Why this matters, measured.** Before the markers existed, the Protocol A driver sent the
> whole file, so a model with no filesystem was told to incrementally write `answer.json`. On one
> 27B model, 43 of 100 cells returned nothing but the empty-list literal that the pre-seed
> section *prints*, and 9 more emitted a hallucinated tool call and stopped. 52 cells scored 0.0
> with no format error recorded — indistinguishable from a real attempt at the task.

**One tolerance.** ±3 s for every game, every protocol. A run at another tolerance is a
diagnostic, not a benchmark score.

**Ground truth never reaches the model.** Not in a prompt, not in a sandbox, not through a helper. `va_core.resolve()` returns the GT path because the offline scorer needs it. The Protocol B sandbox is created outside the repo for the same reason: a sandbox inside it had `tasks/<game>/gt/` one relative path away.

---

## Protocol A — video understanding

One chat call. Frames plus instruction in a single user turn; the reply must contain one JSON
object. No tools, no filesystem, no second turn, no follow-up question.

### The sampling grid

**1 FPS, capped at 256 frames spread uniformly over the whole match, longest side 768 px, never
upscaled, JPEG `-q:v 4`.** One grid for every model and every game.

- **Why 1 FPS.** These are real matches, idle most of the time — a move every few-to-many
  seconds. Sampling denser than the event cadence yields duplicate stills: no new information,
  more tokens.
- **Why one shared cap instead of each vendor's own image limit.** A per-model frame budget makes
  every row of a results table a different measurement, which is the opposite of what a benchmark
  is for. Where a provider's per-request cap is below 256, degrade *and record it* — a degraded
  cell must be readable as degraded, not quietly averaged in.
- **Why never upscale.** Go renders at 612², Chess 548², Gomoku 504² — all below 768. Enlarging
  them doubles the per-frame token cost for exactly zero added information. SC2 halves from 1536²
  to 768²; Doudizhu, GFootball, and Badminton reduce from 1280×720 to 768×432.
- **Frames are individual still images**, one per timestamp, in temporal order. Never a contact
  sheet, a tiled composite, or any other mosaic.

Timestamps are authoritative and are handed over with the frames, as an explicit `video_ts`
array in the prompt (`va_core.protocol_a_prompt()`): frame *k* is at `t = k / fps`, counting
from 0.

**Request size.** 256 busy frames at `-q:v 4` can exceed a gateway's request-body cap (Go at
612², about 34 MB once base64-encoded). Keep the 256 frames and raise `--jpeg-q` for that cell
(`-q:v 6` brings Go to about 28 MB); `run.json` records `sampling.jpeg_q`, so the cell stays
readable as degraded.

**Running it.** `--workers N` runs cells concurrently (the paper uses 5 for Protocol A and 3 for
Protocol B, per system); `--limit N` takes the first N cases of each game; `--extra-body` merges
a JSON object into the request, e.g. `'{"chat_template_kwargs": {"enable_thinking": true}}'`
for a Qwen model on vLLM. The same measurement runs on Harbor through
`tools/harbor/protocol_a_agent.py`, see `docs/HARBOR.md`.

### The recall ceiling

A uniform grid over a long match cannot localize every event within ±3 s no matter how good the
model is. With frame spacing *d*, the highest reachable T1 recall is `min(1, 2·TOL/d)`.

`run_protocol_a.py` records `t1_recall_ceiling` for every cell. **Read it before attributing a low
T1 to the model** — measured per-environment at n=256 frames, the ceiling ranges from 0.597
(DOTA 2) to 0.869. A 46-minute match sampled at 256 frames has ~11 s between frames, so a ±3 s
window covers little more than half the timeline.

### Output budget

A long match produces hundreds of rows, and a truncated reply loses the tail of the answer, not
just its formatting. Set `--max-tokens` generously (the default is 48000).

> Output length is **not** deterministic at temperature 0: one identical request has returned
> 13.6k, 34.6k, and ≥48k output tokens on separate calls. Truncation is therefore probabilistic,
> so decide your re-run rule *before* you look at the results, not after.

### Answer extraction

The reply should hold exactly one JSON object. When it holds more, `va_core.extract_answer()`
takes the one with the **most rows**, ties going to the one appearing **last**, and
`answer_candidates()` reports how many there were.

This is deliberately charitable rather than positional. Measured: a model that fenced a one-row
schema sketch before its real answer was being scored on the sketch (1 row out of 59); another
emitted four fences per reply and was scored 0.0 on an empty early one while later fences held 16
and 40 real rows. Neither was a model failure — both were the extractor choosing the wrong anchor.
Record `n_json_blocks > 1` rather than resolving it silently.

---

## Protocol B — agentic

The whole video goes into a sandbox with a coding agent. It works for many turns, samples frames
however it likes, writes its own parsers, and builds up `answer.json` incrementally.

### The sandbox — exactly three files

A fresh folder with a random name under the system temp dir (or `--sandbox-root`), **outside the repo and never reused**: its path carries no case id, and a re-run starts from a new folder. `prompt.txt` sits next to it, not in it. A re-run of a cell also first deletes the answer, `run.json` and logs its predecessor left, so a run that dies early can never be scored on an earlier run's answer.

| File | What it is |
|---|---|
| `match.mp4` | the video, the only input — renamed, so the case id is not a hint |
| `instruction.md` | the task, rendered for Protocol B |
| `answer.json` | a **byte-for-byte** copy of the game's `answer_seed.json`: root key, empty list |

**Byte-for-byte, not re-serialized.** A driver that rebuilds the seed can get the root key wrong,
and the resulting 0.0 looks exactly like a model that cannot write JSON.

### The machine — nothing is installed for the agent

The prompt tells the agent that ffmpeg, Python and the video tools are already installed, and
forbids installing anything. Natively the agent runs on *your* machine, so that machine must
already carry the toolset; `run_protocol_b.py` installs nothing. Use the Harbor image's set
(`tools/harbor/Dockerfile`), so a native number and a Harbor number measure the same agent:

```bash
# Debian/Ubuntu; on macOS: brew install ffmpeg tesseract
sudo apt-get install -y ffmpeg tesseract-ocr tesseract-ocr-eng
pip install numpy scipy pandas pillow opencv-python-headless scikit-image \
            imageio imageio-ffmpeg av matplotlib pytesseract tqdm
```

The runner preflights this before the first cell. A missing `ffmpeg`, `ffprobe` or `python3`
on PATH is a hard error, because without them every cell still runs and still scores, and the
result reads like a measurement. Missing Python modules or tesseract only warn.
`--skip-preflight` turns the check off when `--agent-cmd` starts the agent in a container of its
own. On Harbor the image provides all of this and the sandbox is offline.

### Why `answer.json` is pre-seeded

It makes "never answered" measurable. Hash the file at the end against the seed:

| State | Meaning |
|---|---|
| `wrote` | the bytes changed — score whatever is in there |
| `never_wrote` | still byte-identical to the seed — the agent never wrote an answer |
| `deleted` | removed or renamed, which the prompt forbids |

`never_wrote` and `deleted` **score 0.0 and stay in the denominator** — the runner exports the untouched seed for them, and `score.py` counts them in its `0-row` column. Leaving them out would let an agent that never answers post a higher mean than one that answers badly. They are still different results from an answer that scored 0.0: `never_wrote` is a budget or behaviour failure, and `run.json` records which state happened. The digest decides — an agent that rewrites the seed with identical content still has not answered. Only a cell whose agent never started (`stop_reason: harness_error`) is left `missing`.

> **Measured, and worth designing for.** `never_wrote` is largely a *commit-threshold* effect, not
> an accuracy effect. One 27B model at a 100-turn cap returned `never_wrote` on 20 of 20 cells;
> the same model at 200 turns returned nonzero scores on 12 of 20, because its single late commit
> (around turn 150) finally fit inside the budget. Rows kept in reasoning and written "at the end"
> score zero when the run is cut off, which is exactly why the prompt insists on rewriting the
> file as the agent goes.

### The budget — 200 turns, 120 minutes

The reference budget is **200 tool-call turns and 120 minutes of wall clock per cell** — the runner's defaults, `--max-turns 200 --timeout-min 120` — and the turn-1 prompt states both. The runner enforces the wall clock itself: at the limit it kills the agent's whole process tree, not just the command it launched, because an agent CLI is a launcher whose children would otherwise keep running, and writing, after the cell was collected. The turn cap can only be enforced by your agent's own flag, so pass it the same number. A harness that cannot count turns runs with `--max-turns 0`, and the prompt then claims only the wall clock.

### The no-network rule

The prompt forbids installs, downloads, external services and models, and looking the match up.
**This is audited, not enforced.** No sandbox can be offline while the agent's own brain is an
HTTPS call, so a serious evaluation scans the agent's transcript afterwards for package installs,
downloads, and match lookups. Claiming isolation would not be true.

Two properties an egress audit needs:

- **Anchor patterns to real syntax.** A bare `curl` matches the word inside any prose the agent
  quotes; `curl` followed by a URL-ish token does not.
- **Scan only agent-authored lines.** Strings quoted inside a *tool result* (e.g. a library error
  message) are not the agent's actions and must not void a cell.

### Reproducibility

**Protocol B is not reproducible cell-by-cell.** The agent's trajectory is the random variable.
The same cell, same conditions, two clean runs: **0.0645 and 0.0323** — T1 stable at 1.0, all the
movement in T2/T3. Cite environment means with n. A per-cell `±` costs three times the matrix and
still would not make a single cell a stable number.

---

## Reporting a comparable number

1. **Quote the denominator.** `score.py` prints `n_scored/n_cases` beside every mean. A cell that never ran — a Protocol A call that never got a reply (ffmpeg, HTTP, timeout), a Protocol B agent that never started — is `missing`, never 0.0. A cell that ran and left no answer — Protocol B `never_wrote` / `deleted`, Protocol A `no_answer` (a reply with no answer object in it) — scores 0.0, stays in the mean, and is counted in the `0-row` column.
2. **The overall row is the mean of the nine environment means**, not of 180 cases — so one
   unscored cell cannot silently reweight the games against each other. An environment whose every
   cell failed *drops out of that mean*; when comparing two systems, diff them on the **shared**
   environment set, or the comparison inverts. (Measured: an env-mean comparison read 0.62× when
   the shared-set comparison read 2.0×.)
3. **Record per cell**: protocol, model id, frames or turns actually used, stop reason, answer
   state, row count, wall time, and the instruction digest that was *delivered*. Both reference
   runners write a `run.json` with these.
4. **State the reasoning effort** if the model has one, and read it from the run record rather
   than from your config file.
5. **Don't compare across tolerances or sampling grids.** Both are part of the metric.
