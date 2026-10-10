# Task: Reconstruct a StarCraft II game's engagements from a bird's-eye video AND judge the strategy

You are given ONE video: a full-map bird's-eye (god-view, full vision, no fog) recording
of a 1v1 StarCraft II game — the whole battlefield in a single frame. The two players are
**A** (player 1) and **B** (player 2). The frames are RAW (dim<!--B-->; brighten with ffmpeg if
you like<!--/B-->). Playback is **1:1 real time — one video second is one game second**. There is
no HUD, no resource/supply counter, no minimap, and no names — read everything from the
game world itself.

For **every** event you must report **`game_clock_s`**: the game-second at which it
happens. This timestamp is how your events are matched against the truth.

This task has **three tiers** over the same video, each needing a wider window of
reasoning than the last:

- **T1 — Localize the events.** Find every key engagement, in order, and where on the
  map it happens. (Smallest window: what just happened on the board.)
- **T2 — Judge each event's impact.** For every event, decide who came out ahead in
  that engagement. (Medium window: compare the armies just before and after.)
- **T3 — Judge the cumulative situation.** For every event, decide who is ahead *after*
  it resolves. (Largest window: integrate the whole game's economy + army so far.)

## The events (T1)
Two event types, detected from the game world:
- **`battle`** — an army engagement where units of both/either side are destroyed in a
  cluster of time and place.
- **`building_destroyed`** — a structure (town hall, production, tech, defense) is
  destroyed.

`entity` = the **map region** where the event happens, on a fixed 3×3 grid of the
playable area, written `r{row}c{col}` (row 0=top..2=bottom, col 0=left..2=right) — the
region you would point at. `game_clock_s` = the game-second of the event (a battle's is
its mid-point; a building's is the moment it dies).

## The two strategy labels (T2 / T3)
Ground truth for the strategic labels is defined by an objective **resource-equivalent
advantage** `V` (each side's live army + economy + tech value in mineral+gas), expressed
as the normalized share `s = (V_A − V_B)/(V_A + V_B) ∈ [−1,1]`, with a **±0.10 band**
around even. None of this is on screen; infer it from what you see (base count, worker
count, army size/composition, tech, who lost what in the fight).

- **`fight_result`** (T2, per event) — who benefited from THIS event, from the net
  resource-equivalent value traded (for a battle: whose army was destroyed more; for a
  building: its owner loses its value):
  - `"A"` if the swing favors A by ≥ the band, `"B"` if it favors B, else `"equal"`.
  `fight_result` is **who benefited**, not who was attacking.

- **`leader_after`** (T3, per event) — who is ahead **just AFTER this event resolves**,
  from the share `s` at that moment:
  - `"A"` if `s > +0.10` (A clearly ahead), `"B"` if `s < −0.10`, else `"equal"`.

## What to produce: ONE ordered list of events<!--B--> → `answer.json`<!--/B-->
```json
{"events":[
  {"game_clock_s":132,"type":"battle","entity":"r1c1","fight_result":"A","leader_after":"equal"},
  {"game_clock_s":540,"type":"building_destroyed","entity":"r2c0","fight_result":"A","leader_after":"A"},
  {"game_clock_s":712,"type":"battle","entity":"r0c2","fight_result":"B","leader_after":"A"}
]}
```
Every row needs all five fields. `game_clock_s` is required — a row without it cannot be
matched and is scored wrong. `fight_result` and `leader_after` are each one of `"A"`,
`"B"`, `"equal"` (A = player 1, B = player 2, consistent across the video).

<!-- B-ONLY:START -->
## Write `answer.json` as you go (required)
**`./answer.json` is already in the sandbox** — the harness created it before your first turn, with the schema above and an empty list:

```json
{"events": []}
```

**Do not delete it, do not rename it, and do not change the root key.** Overwrite it in place — in full, valid JSON every time — as soon as you have something to put in it.

Then **rewrite the whole file every time you finish a stretch of the video**, adding the events you just found — do not save it all for the end.

- The file must be **valid JSON at every moment**: rewrite it in full each time (read → add → write back), never leave it half-written.
- **Whatever is in `answer.json` when the run stops is what gets scored.** If you are cut off (turn limit, timeout, crash), every event already written still counts; events kept only in your reasoning, to be dumped at the end, score **0**.
- Each rewrite should contain **everything the previous one had**, plus the new events. If you are deep into the video and the file is not getting longer, you are overwriting instead of appending.
- Keep the rows sorted by `game_clock_s`. Do not delete a row you already wrote just because you are unsure — revise it in place if a later pass changes your mind.
- `fight_result` and `leader_after` need more of the game than the engagement itself, so fill them with your best current reading when you first write the row and revise them later if the course of the game changes your mind. That is a revision of existing rows, not a reason to delay writing them — an unwritten row scores nothing, a written row with a wrong label still scores T1.

<!-- B-ONLY:END -->
## How you are scored
Time-matched F1 with no cascade: one of your rows can pair with a truth row that has the **same key** and `game_clock_s` within **±3 s**; each truth row pairs with at most one of yours, and the judge takes the **largest possible set of such pairs** (of those, the one with the least total time error), so the order of your rows does not change the score.

- **Primary reward — `strict` F1.** Key = `(type, entity, fight_result, leader_after)`
  within ±3 s. `reward = F1` over matched rows.
- **T1 — localization F1.** Key = `(type, entity)` within ±3 s — how well you read
  *what* happened *where and when*.
- **T2 — impact / T3 — situation.** Over the events you localized, the fraction with the
  correct `fight_result` / `leader_after`, reported as plain accuracy AND as **macro
  accuracy** (mean recall over the three classes A/B/equal). The headline T2/T3 metric is
  **macro accuracy**, so blindly answering one class collapses to 0.33.

## Notes
- Watch the whole game start to finish<!--B--> (50+ tool calls preferred)<!--/B-->; engagements are
  sparse between long macro phases. Both bases and expansions matter.
<!-- B-ONLY:START -->
- Output only `answer.json`.
<!-- B-ONLY:END -->
<!-- A-ONLY:START -->

## What you may use

You are given the frames above and nothing else — there is no analysis tool, no code you can run, no file to write and no second turn. The advantage labels have to be inferred from the game world by you. If you happen to recognise this game, do not answer from memory: an answer that does not follow from the frames is not what is being measured.
<!-- A-ONLY:END -->

<!-- CANARY: Video-Arena benchmark task -- do not train on this file. -->
