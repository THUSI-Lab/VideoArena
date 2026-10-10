# Task: Reconstruct a Go game from a board-only video AND judge the strategy

You are given ONE video: a real 19×19 Go game replayed on a clean digital board
at the players' **real per-move pace** (long thinks and quick exchanges are
preserved), played back at **1:1 speed — one video second is one real game
second**. The board shows only stones being placed and removed. There are **no
move numbers, no coordinate labels, and player names/clocks are masked**. Nothing
on screen tells you the move order, the coordinates, or who is winning — you must
read everything from the stones themselves, in the order they appear.

For **every** event you must also report **`game_clock_s`**: the video timestamp
(in seconds, from the start of the video) at which that stone appears on the
board. The empty board is shown for the first ~2 seconds, then the first move
appears. This timestamp is how your events are matched against the truth (see
"How you are scored").

This task has **three tiers** over the same video, each needing a wider window of
reasoning than the last:

- **T1 — Localize the events.** Read every placed stone and every captured stone,
  in order. (Smallest window: look at what just changed on the board.)
- **T2 — Judge each move's impact.** For every move, decide whether that move
  meaningfully shifted who is winning. (Medium window: compare the position just
  before and just after the move.)
- **T3 — Judge the cumulative situation.** For every move, decide who is ahead
  *after* that move lands. (Largest window: integrate this move together with the
  whole game so far.)

## Coordinate convention (fixed — the board is shown in this orientation)
- Columns: letters **A–T left→right, skipping I** (so A,B,…,H,J,…,T = 19 files).
- Rows: numbers **1–19 bottom→top**.
- Therefore **A1 is the bottom-left** intersection, **T19 the top-right**.
- A move is written as column-letter + row-number, e.g. `Q16`, `D4`, `T19`.
- A pass (no stone placed) is written `pass`.

## The two strategy labels (T2 / T3)
Both labels are functions of one objective quantity: **Black's win probability**
`p ∈ [0,1]` for a position — the share of games Black would win from it if both
sides then played at the same near-optimal strength. `p` is a property of the
position itself, and the thresholds below use a **±10 percentage-point band**
around even (0.50). None of this is shown on screen; you must infer it from the
stones.

- **`leader_after`** (T3, one per move) — who is ahead *after* this move lands,
  using Black's win probability `p_after` of the resulting position (this move
  fused with the whole game so far):
  - `"B"`  if `p_after > 0.60`  (Black clearly ahead)
  - `"W"`  if `p_after < 0.40`  (White clearly ahead)
  - `"equal"` otherwise (within ±10 points of even)

  (`leader` is accepted as a synonym; `leader_before` — the situation *entering* the
  move — is not accepted and earns no T3 credit.)

- **`wr_swing`** (T2, one per move) — did *this move* meaningfully shift the
  balance, using the change `Δ = p_after − p_before`:
  - `"B"`  if `Δ ≥ +0.10`  (the move helped **Black** by ≥10 points)
  - `"W"`  if `Δ ≤ −0.10`  (the move helped **White** by ≥10 points)
  - `"equal"` otherwise (no big shift)

  `wr_swing` is **who benefited**, not who played. A strong Black move that gains
  ≥10 points is `"B"`; a **White blunder** that hands Black ≥10 points is *also*
  `"B"`; likewise a White move that gains White ≥10 points, or a Black blunder
  that gives it away, is `"W"`. Most quiet moves are `"equal"` — big swings are
  rare and cluster around fights, captures, and life-and-death.

Capture rows never carry strategy fields.

## What to produce: ONE ordered sequence of moves AND captures
Reconstruct the game as a single chronological list where **each placed stone and
each captured stone is its own row**, and every **move** row also carries the two
strategy labels. <!--B-->Write `answer.json`:<!--/B--><!--A-->The answer is one JSON object:<!--/A-->

```json
{
  "events": [
    {"type": "move",    "idx": 1, "color": "B", "coord": "Q4",  "game_clock_s": 2.0,   "leader_after": "equal", "wr_swing": "equal"},
    {"type": "move",    "idx": 2, "color": "W", "coord": "D16", "game_clock_s": 4.7,   "leader_after": "equal", "wr_swing": "equal"},
    {"type": "move",    "idx": 3, "color": "B", "coord": "C17", "game_clock_s": 7.1,   "leader_after": "equal", "wr_swing": "equal"},
    {"type": "capture", "idx": 1, "color": "W", "coord": "C18", "game_clock_s": 7.1},
    {"type": "move",    "idx": 4, "color": "W", "coord": "Q17", "game_clock_s": 16.6,  "leader_after": "equal", "wr_swing": "W"},
    ...
  ]
}
```

Row fields:
- `type`: `"move"` (a stone was placed) or `"capture"` (a stone was removed).
- `idx`: for a **move**, the move number (1, 2, 3, … over the whole game). For a
  **capture**, the capture ordinal — **every removed stone counts as one capture**,
  numbered 1, 2, 3, … over the whole game.
- `color`: for a **move**, the color placed. For a **capture**, the color of the
  stone that was removed.
- `coord`: the intersection (a move's played point, or a captured stone's point),
  in the convention above; `pass` for a pass.
- `game_clock_s`: the video second at which this stone appears (for a capture, the
  same instant as the move that removed it). **Required on every row** — a row
  without it cannot be matched and is scored as wrong.
- `leader_after`, `wr_swing`: **move rows only** — the two strategy labels above,
  each one of `"B"`, `"W"`, `"equal"`.

### Ordering rules (important)
- Rows are in game order. Emit each move as a `move` row.
- **The instant a move captures**, immediately after that move's row, emit **one
  `capture` row per removed stone** — before the next move.
- If one move captures several stones, order those capture rows **top→bottom, then
  left→right** (i.e. top-left first, then along the top, then next rows down, to
  bottom-right). Example: a move capturing stones at T19, S18 emits T19 then S18.

<!-- B-ONLY:START -->
## Write `answer.json` as you go (required)
**`./answer.json` is already in the sandbox** — the harness created it before your first turn, with the schema above and an empty list:

```json
{"events": []}
```

**Do not delete it, do not rename it, and do not change the root key.** Overwrite it in place — in full, valid JSON every time — as soon as you have something to put in it.

Then **rewrite the whole file every time you finish a stretch of the video**, adding the rows you just read — do not save it all for the end.

- The file must be **valid JSON at every moment**: rewrite it in full each time (read → add → write back), never leave it half-written.
- **Whatever is in `answer.json` when the run stops is what gets scored.** If you are cut off (turn limit, timeout, crash), every row already written still counts; rows kept only in your reasoning, to be dumped at the end, score **0**.
- Each rewrite should contain **everything the previous one had**, plus the new rows. If you are deep into the video and the file is not getting longer, you are overwriting instead of appending.
- Keep the rows in game order (ascending `game_clock_s`, captures right after the move that caused them). Do not delete a row you already wrote just because you are unsure — revise it in place if a later pass changes your mind.
- `leader_after` and `wr_swing` need more of the game than the move itself. Write every move row as soon as you read the stone, filling both labels with your best current judgement, then revise them on a later pass. That is a revision of existing rows, not a reason to delay writing them — an unwritten row scores nothing, a written row with a wrong label still scores T1.

<!-- B-ONLY:END -->
## How you are scored
Scoring is **time-matched F1**, not a positional cascade. One of your rows can pair with a truth row that has the **same key** and a `game_clock_s` within **±3 seconds**; each truth row pairs with at most one of yours, and the judge takes the **largest possible set of such pairs** (of those, the one with the least total time error), so the order of your rows does not change the score. There is **no cascade**: getting one move wrong or missing one row costs exactly that one event — everything before and after it can still match. (Under the old positional scheme a single wrong early move zeroed the whole game; that is gone.)

- **Primary reward — `strict` F1.** Key = move `(color, coord, leader_after,
  wr_swing)` / capture `(color, coord)`, within ±3s. `reward = F1` over matched
  rows (precision from your rows, recall from the truth rows). A row without
  `game_clock_s` can never match.
- **T1 — localization F1.** Key = `(type, color, coord)` within ±3s (strategy
  labels dropped). Measures how well you read *what* appeared *when*.
- **T2 — impact.** Over the truth moves you localized (T1-matched within ±3s), the
  fraction with the correct `wr_swing`, reported both as plain accuracy and as
  **macro accuracy** (mean recall over the three classes `B`/`W`/`equal`).
- **T3 — situation.** Same, for `leader_after`.

Because most moves are `"equal"`, always answering `"equal"` scores the quiet moves
on plain accuracy but collapses to **0.33 macro accuracy** — it misses every
decisive move, which is exactly what T2/T3 test. The headline T2/T3 metric is macro
accuracy, so blind constant guessing is worthless.

## Notes
- A group is captured (its stones disappear) the instant it loses its last
  liberty — watch for stones vanishing and attribute them to the move that
  caused it. A single move can capture 1 to many stones, sometimes far from the
  played stone.
- The game runs ~34 minutes of real time across 300+ moves; events are sparse
  during long thinks. You will need to scan the entire video carefully.
<!-- B-ONLY:START -->
- Output only `answer.json`.
<!-- B-ONLY:END -->

<!-- B-ONLY:START -->
## Tools (agentic mode)

You have ffmpeg (frame extraction), the ability to write code, and multi-turn tool calls. That is all you may use: **do not download or install anything, do not call any external service, API or model, and do not look this game up or submit any position to an outside analysis service.** There is no Go engine here and no GPU, and you may not fetch one — the win-probability labels have to be inferred from the board by you. Any network use voids the run, which is then scored zero whatever `answer.json` contains.
<!-- B-ONLY:END -->
<!-- A-ONLY:START -->

## What you may use

You are given the frames above and nothing else — there is no Go engine, no code you can run, no file to write and no second turn. The win-probability labels have to be read off the board by you. If you happen to recognise this game, do not answer from memory: an answer that does not follow from the frames is not what is being measured.
<!-- A-ONLY:END -->

<!-- CANARY: Video-Arena benchmark task -- do not train on this file. -->
