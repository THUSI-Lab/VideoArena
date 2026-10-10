# Task: Reconstruct a CS2 match from an abstract top-down radar video

You are given ONE video: a Counter-Strike 2 match replayed on a clean **top-down radar**, played back at **1:1 speed — one video second is one real game second**. Rounds are separated by a short reset (the map briefly clears/goes dark between rounds). Each round opens with a brief **freeze / buy phase**: the ten dots sit still near their spawns and the corner clock stays fixed at the full round time; the round is "live" once the dots start moving, which is when the clock begins counting down. Nothing on screen names the map, the teams, the players, the score, or the round number — you must infer everything from what the dots do.

## What the radar shows
- **10 numbered dots** = the 10 players. **Orange dots = the T side, blue dots = the CT side.** Each dot carries a number **1–10** (1–5 are one side's slots, 6–10 the other). A dot with a small view-cone is looking that way; a brief line from a dot is a shot.
- **Death markers**: when a player dies, their dot turns into a bold **neutral-gray ×** at the death spot (it lingers as a "body"). The × is deliberately **not team-colored and carries no number** — so both the victim's `side` and `number` must be inferred from which numbered dot vanished/fell there, not read off the marker.
- **The C4 bomb** is always locatable: a **red ring + "C4"** on a T's dot while carried, a red **"C4" square** on the floor while dropped, and a **red diamond** once planted.
- **A countdown clock** sits in the darkest map-free corner (m:ss). It counts down the round time; after the bomb is planted it switches to a **red ~40s** bomb timer. A green clock/ring = defuse; a full-frame orange flash = detonation. (This clock is the ONLY on-screen text — it does not tell you the score or round number.)

## What to produce: ONE unified event list<!--B--> (`answer.json`)<!--/B-->
Unlike a plain event log, **each event answers everything at once**. Produce a single chronological list `events`; each event carries its perception fields **and** the two round-level fields (the round winner and the running score). Because those two are properties of the round, **every event in the same round carries the identical `round_winner` and `score_after`.**

```json
{
  "events": [
    {"round_no": 1, "type": "kill", "video_ts": 23.4, "side": "CT", "number": 7,
     "round_winner": "T", "score_after": {"A": 1, "B": 0}},
    {"round_no": 1, "type": "bomb_planted", "video_ts": 41.0, "side": "T",
     "round_winner": "T", "score_after": {"A": 1, "B": 0}},
    {"round_no": 1, "type": "bomb_exploded", "video_ts": 78.9, "winner_side": "T",
     "round_winner": "T", "score_after": {"A": 1, "B": 0}}
  ]
}
```

### Fields on every event
- `round_no` — 1-based round index (round 1 is the first round of the video).
- `type` — one of: `kill` / `bomb_planted` / `bomb_defused` / `bomb_exploded` / `elimination` / `save`.
  - `elimination` = a whole side wiped out. `save` = the round ended on time expiry / target saved (no bomb detonation or defuse, no full wipe). Each round has exactly ONE conclusion event (`bomb_exploded` | `bomb_defused` | `elimination` | `save`).
- `video_ts` — the video second (float) at which the event appears. **Required** — an event without it can never match.
- **Entity attribute(s)** — depend on `type`:
  - `kill` → the **victim**: `side` (`"T"`=orange / `"CT"`=blue) **and** `number` (1–10 = the dead dot's number).
  - `bomb_planted` → the **planter's** `side`.
  - conclusion (`bomb_exploded`/`bomb_defused`/`elimination`/`save`) → the **winning side** `winner_side` (`"T"` / `"CT"`).
- **Round-level fields, identical for all events in the same round:**
  - `round_winner` — which side won this round: `"T"` or `"CT"`.
  - `score_after` — the running match score **after this round completes**, by **persistent team**: `{"A": <int>, "B": <int>}`. Team A and team B are the two teams as they were in **round 1**; they keep their letters across the halftime side-swap (when the sides switch colours), so tracking the score correctly means detecting the swap.

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
- Keep the rows sorted by `video_ts`. Do not delete a row you already wrote just because you are unsure — revise it in place if a later pass changes your mind.
- The round-level fields (`round_winner`, `score_after`) are only knowable once a round ends. Write each round's events as soon as you see them with your best current values, then go back and fix that round's two fields when the round concludes — that is a revision of existing rows, not a reason to delay writing them.

<!-- B-ONLY:END -->
## How you are scored
A single **1:1 time-matched F1** over the full event tuple. One of your events can pair with a ground-truth event with the **same** `round_no`, the **same** `type`, `video_ts` within **±3 s**, AND all of: matching entity attribute(s), matching `round_winner`, matching `score_after`. **Any one wrong field — including the round winner or the score — voids that event.** Each ground-truth event pairs with at most one of yours; the judge takes the **largest possible set of such pairs** (and, of those, the one with the least total time error), so the order of your rows does not change the score. `reward = F1` over matched events. There is no positional cascade at the event level, with two exceptions that come from the match itself rather than from row order: `score_after` is cumulative, so an early score error propagates to every later event's `score_after`; and `round_no` is part of every event's identity, so **miscounting one round — a missed or an extra round boundary — shifts the `round_no` of every later event, and none of those events can match** until your count is right again. Count the between-round resets carefully.

## Notes
- The match runs ~15–40 minutes with sparse events; scan the whole video.
<!-- B-ONLY:START -->
- Output **only** `answer.json` with the single `events` key.
<!-- B-ONLY:END -->

<!-- B-ONLY:START -->
## Tools (agentic mode)

You have ffmpeg (frame extraction), the ability to write code, and multi-turn tool calls. That is all you may use: **do not download or install anything, and do not call any external service, API or model**. This is a real competitive match, so a search could hand you the round scores. Do not look it up — not the map, the teams, the players, nor the scoreline. Any network use voids the run — it is scored zero whatever `answer.json` contains.
<!-- B-ONLY:END -->
<!-- A-ONLY:START -->

## What you may use

You are given the frames above and nothing else — there is no analysis tool, no code you can run, no file to write and no second turn. The rounds, their winners and the running score have to be read off the radar by you. If you happen to recognise this match, do not answer from memory: an answer that does not follow from the frames is not what is being measured.
<!-- A-ONLY:END -->

<!-- CANARY: Video-Arena benchmark task -- do not train on this file. -->
