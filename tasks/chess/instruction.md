# Task: Reconstruct a chess game from a board-only video AND judge the strategy

You are given ONE video: a real chess game replayed on a clean digital board at the players' **real per-move pace** (long thinks and quick exchanges are preserved), played back at **1:1 speed — one video second is one real game second**. The board shows only pieces being moved and captured, **White at the bottom**. There are **no move list, no eval bar, and no clock/names**. Nothing on screen tells you the move order, the destination squares, or who is winning — you must read everything from the pieces themselves, in the order they move.

For **every** event you must also report **`game_clock_s`**: the video timestamp (in seconds, from the start of the video) at which that piece appears on its new square. The starting position is shown for the first ~2 seconds, then the first ply appears. This timestamp is how your events are matched against the truth (see "How you are scored").

This task has **three tiers** over the same video, each needing a wider window of reasoning than the last:

- **T1 — Localize the events.** Read every played ply and every captured piece, in order. (Smallest window: look at what just changed on the board.)
- **T2 — Judge each ply's impact.** For every ply, decide whether that move meaningfully shifted who is winning. (Medium window: compare the position just before and just after the ply.)
- **T3 — Judge the cumulative situation.** For every ply, decide who is ahead *after* that ply lands. (Largest window: integrate this ply together with the whole game so far.)

## Coordinate convention (fixed — the board is shown in this orientation)
- **White at the bottom.** Files **A–H left→right**, ranks **1–8 bottom→top**.
- Therefore **A1 is the bottom-left** square, **H8 the top-right**.
- A ply's `coord` is its **destination square**, e.g. `E4`, `G1`, `H8`.
- **Castling** is written as the **king's** destination: `G1` / `C1` (White), `G8` / `C8` (Black). The rook moves as part of the same ply — do **not** emit a separate row for it.

## The two strategy labels (T2 / T3)
Both labels are functions of one objective quantity: **White's win probability** `p ∈ [0,1]` for a position — the share of games White would win from it if both sides then played at the same near-optimal strength. `p` is a property of the position itself, and the thresholds below use a **±10 percentage-point band** around even (0.50). None of this is shown on screen; you must infer it from the pieces.

- **`leader_after`** (T3, one per ply) — who is ahead *after* this ply lands, using White's win probability `p_after` of the resulting position (this ply fused with the whole game so far):
  - `"W"`  if `p_after > 0.60`  (White clearly ahead)
  - `"B"`  if `p_after < 0.40`  (Black clearly ahead)
  - `"equal"` otherwise (within ±10 points of even)

  (`leader` is accepted as a synonym; `leader_before` — the situation *entering* the ply — is not accepted and earns no T3 credit.)

- **`wr_swing`** (T2, one per ply) — did *this ply* meaningfully shift the balance, using the change `Δ = p_after − p_before`:
  - `"W"`  if `Δ ≥ +0.10`  (the move helped **White** by ≥10 points)
  - `"B"`  if `Δ ≤ −0.10`  (the move helped **Black** by ≥10 points)
  - `"equal"` otherwise (no big shift)

  `wr_swing` is **who benefited**, not who played. A strong White move that gains ≥10 points is `"W"`; a **Black blunder** that hands White ≥10 points is *also* `"W"`; likewise a strong Black move, or a White blunder that gives it away, is `"B"`. Most quiet moves are `"equal"` — big swings are rare and cluster around captures, tactics, and blunders.

Capture rows never carry strategy fields.

## What to produce: ONE ordered sequence of plies AND captures
Reconstruct the game as a single chronological list where **each played ply and each captured piece is its own row**, and every **move** row also carries the two strategy labels. <!--B-->Write `answer.json`:<!--/B--><!--A-->The answer is one JSON object:<!--/A-->

```json
{
  "events": [
    {"type": "move",    "idx": 1, "color": "W", "coord": "E4", "game_clock_s": 2.0,  "leader_after": "equal", "wr_swing": "equal"},
    {"type": "move",    "idx": 2, "color": "B", "coord": "E5", "game_clock_s": 4.0,  "leader_after": "equal", "wr_swing": "equal"},
    {"type": "move",    "idx": 3, "color": "W", "coord": "C4", "game_clock_s": 6.0,  "leader_after": "equal", "wr_swing": "equal"},
    {"type": "move",    "idx": 4, "color": "B", "coord": "F6", "game_clock_s": 9.4,  "leader_after": "equal", "wr_swing": "equal"},
    {"type": "move",    "idx": 5, "color": "W", "coord": "D5", "game_clock_s": 16.6, "leader_after": "equal", "wr_swing": "W"},
    {"type": "capture", "idx": 1, "color": "B", "coord": "D5", "game_clock_s": 16.6},
    ...
  ]
}
```

Row fields:
- `type`: `"move"` (a piece moved) or `"capture"` (a piece was removed).
- `idx`: for a **move**, the ply number (1, 2, 3, … over the whole game). For a **capture**, the capture ordinal — numbered 1, 2, 3, … over the whole game. **At most one capture per ply.**
- `color`: for a **move**, the color that moved. For a **capture**, the color of the piece that was removed.
- `coord`: a move's **destination square** (king's destination for castling); a capture's **square the removed piece stood on** (for en passant, the captured pawn's square, not the moving pawn's destination). Convention above.
- `game_clock_s`: the video second at which this event appears (for a capture, the same instant as the ply that removed it). **Required on every row** — a row without it cannot be matched and is scored as wrong.
- `leader_after`, `wr_swing`: **move rows only** — the two strategy labels above, each one of `"B"`, `"W"`, `"equal"`.

### Ordering rules (important)
- Rows are in game order. Emit each ply as a `move` row.
- **If a ply captures**, immediately after that ply's `move` row emit **one `capture` row** for the removed piece — before the next ply. A ply captures at most one piece.

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
- Keep the rows in game order (ascending `game_clock_s`, a capture right after the ply that caused it). Do not delete a row you already wrote just because you are unsure — revise it in place if a later pass changes your mind.
- `leader_after` and `wr_swing` need more of the game than the ply itself. Write every move row as soon as you read the ply, filling both labels with your best current judgement, then revise them on a later pass. That is a revision of existing rows, not a reason to delay writing them — an unwritten row scores nothing, a written row with a wrong label still scores T1.

<!-- B-ONLY:END -->
## How you are scored
Scoring is **time-matched F1**, not a positional cascade. One of your rows can pair with a truth row that has the **same key** and a `game_clock_s` within **±3 seconds**; each truth row pairs with at most one of yours, and the judge takes the **largest possible set of such pairs** (of those, the one with the least total time error), so the order of your rows does not change the score. There is **no cascade**: getting one ply wrong or missing one row costs exactly that one event — everything before and after it can still match.

- **Primary reward — `strict` F1.** Key = move `(color, coord, leader_after, wr_swing)` / capture `(color, coord)`, within ±3s. `reward = F1` over matched rows. A row without `game_clock_s` can never match.
- **T1 — localization F1.** Key = `(type, color, coord)` within ±3s (strategy labels dropped).
- **T2 — impact.** Over the truth plies you localized (T1-matched within ±3s), the fraction with the correct `wr_swing`, reported as plain accuracy and as **macro accuracy** (mean recall over `B`/`W`/`equal`).
- **T3 — situation.** Same, for `leader_after`.

Because most plies are `"equal"`, always answering `"equal"` scores the quiet moves on plain accuracy but collapses to **0.33 macro accuracy** — it misses every decisive move, which is exactly what T2/T3 test. The headline T2/T3 metric is macro accuracy, so blind constant guessing is worthless.

## Notes
- Watch for captures: a piece vanishes and the mover lands on (or, for en passant, beside) its square. Track promotions — a pawn reaching the last rank becomes another piece on its destination square (still one `move` row, `coord` = the promotion square).
- Games run tens of minutes across dozens to 100+ plies; events are sparse during long thinks. You will need to scan the entire video carefully.
<!-- B-ONLY:START -->
- Output only `answer.json`.
<!-- B-ONLY:END -->

<!-- B-ONLY:START -->
## Tools (agentic mode)

You have ffmpeg (frame extraction), the ability to write code, and multi-turn tool calls. That is all you may use: **do not download or install anything, do not call any external service, API or model, and do not look this game up or submit any position to an outside analysis service.** There is no chess engine here and no GPU, and you may not fetch one — the win-probability labels have to be inferred from the board by you. Any network use voids the run, which is then scored zero whatever `answer.json` contains.
<!-- B-ONLY:END -->
<!-- A-ONLY:START -->

## What you may use

You are given the frames above and nothing else — there is no chess engine, no code you can run, no file to write and no second turn. The win-probability labels have to be read off the board by you. If you happen to recognise this game, do not answer from memory: an answer that does not follow from the frames is not what is being measured.
<!-- A-ONLY:END -->

<!-- CANARY: Video-Arena benchmark task -- do not train on this file. -->
