# Agent instruction — GFootball three-tier reconstruction

You are given a HUD-free video of a GFootball (football/soccer) match, shot from the engine's default **broadcast camera — an angled 3D view, not top-down**: no scoreboard, no clock, no goal captions, and no announcement text of any kind (they are switched off inside the engine, so nothing is blacked out either). **The score is never visible in the video.** A **minimap is on screen**, bottom-centre: it shows all 22 player dots and the ball, and no score — use it, it is not a leak. The two teams never swap ends and the picture is never mirrored or frozen. Players are coloured 3D figures with unnumbered shirts, but the engine floats the **last name** of the **current ball carrier** above his head — that is the only channel through which you can get a player's identity.

## Your task

Reconstruct the ordered list of every goal in the match. Each goal carries 5 fields:

| Field | Tier | Meaning | Values |
|---|---|---|---|
| `game_clock_s` | T1 | The second of this goal on the **video playback timeline** | float, tolerance ±3 s |
| `scoring_side` | T1 | **The side whose score went up** (see the own-goal convention below) | `L` (left team) / `R` (right team) |
| `scorer_name` | T1 | The scorer's last name, spelled as the floating caption spells it | string, e.g. `Turing`, `da Vinci` |
| `impact` | T2 | What this goal changed about who leads | `extended` / `taken` / `levelled` / `narrowed` |
| `score_after` | T3 | The score **after** this goal went in | two-element array `[left goals, right goals]` |

You may include `idx` (1-based) and `type` (always `"goal"`); neither is scored.

## What the three tiers mean

- **T1 — localization**: when (`game_clock_s`), which side scored (`scoring_side`), and who touched it last (`scorer_name`). This is the entry ticket: T2/T3 are only scored on goals that T1 matched correctly, and a goal T1 never found has its T2/T3 counted wrong.
- **T2 — impact** (ΔV): let `u` = the **scoring side's** goal difference immediately *before* this goal (scoring side's goals − opponent's goals). Then

  | Condition | Label | Meaning |
  |---|---|---|
  | `u > 0` | `extended` | Already ahead, stretching the lead |
  | `u == 0` | `taken` | Level before, now in front |
  | `u == -1` | `levelled` | One behind, now equal |
  | `u <= -2` | `narrowed` | Closer, but still behind |

  Note that `levelled` and `narrowed` differ **only in how far behind** the scoring side was: knowing "who is ahead" cannot separate these two classes — you have to genuinely remember the score numbers.

  `u` uses the goal difference **before** the goal, because this tier asks "what did this goal change"; T3 asks "what does the situation look like after the change". The two tiers look at opposite sides of the same jump — do not conflate them.
- **T3 — situation** (V): `score_after`, the **exact score pair after** this goal. Not "who leads" — two integers. The first goal of the match always has `score_after` of `[1,0]` or `[0,1]`, and the last goal's `score_after` is the final score of the match.

## Why you must track the whole match

The score is never shown on screen and there are no goal captions. You have to recognize each goal yourself (the ball crossing the goal line, followed by a restart from the centre circle, is the most reliable signal), decide **which goal it went into**, and accumulate from zero — only then can you answer T2/T3. The two teams never swap ends, so once you establish the mapping "ball went into the right-hand goal → the left team scored", it holds for the whole match.

## Own-goal convention (important)

`scoring_side` is defined as **the side whose score went up**, i.e. the side this goal benefits, which is **not necessarily** the team `scorer_name` plays for. On an own goal the two are opposite: the caption names the last player to touch the ball (who belongs to the side that conceded), while `scoring_side` is still the beneficiary. Always decide this from **which goal the ball went into** — never work backwards from "what colour is the celebrating player" or "which team is the captioned player on". These goals are rare in the corpus (measured: 1 own goals out of 405, 0.25%) but they do occur.

## Answer format

<!--B-->Write `answer.json`:<!--/B--><!--A-->The answer is one JSON object:<!--/A-->

```json
{"events": [
  {"type": "goal", "idx": 1, "game_clock_s": 128.4, "scoring_side": "L",
   "scorer_name": "Turing", "impact": "taken", "score_after": [1, 0]},
  {"type": "goal", "idx": 2, "game_clock_s": 402.9, "scoring_side": "R",
   "scorer_name": "Franklin", "impact": "levelled", "score_after": [1, 1]}
]}
```

- `game_clock_s` is in **video playback seconds**, not the in-match clock. Tolerance ±3 s.
- A missing field counts as wrong: the `strict` key is the tuple of four fields, and a row with any of them empty can never match. So prefer a reasoned guess over a blank.
- `scoring_side` also accepts `left`/`right`/`home`/`away`/`0`/`1` (and the Chinese glosses `左`/`右`); `impact` also accepts common variants such as `extend`/`equalised`/`equalized`/`narrow`/`reduced` (and Chinese glosses such as `追平`); `score_after` also accepts a string like `"1-0"`, and the field itself may be named `score` or `scoreline`. Prefer the canonical values in the table above.
- **Do not write the score from before the goal.** T3 is explicitly the score **after** the goal, and `score_before` is not accepted as an alias (it differs from `score_after` by exactly one goal, so accepting it would be giving marks away). If the rows you submit carry only `score_before`, `reward.json` reports the count in `detail.answer_rows_score_before_only`.

<!-- B-ONLY:START -->
## Write `answer.json` as you go (required)
**`./answer.json` is already in the sandbox** — the harness created it before your first turn, with the schema above and an empty list:

```json
{"events": []}
```

**Do not delete it, do not rename it, and do not change the root key.** Overwrite it in place — in full, valid JSON every time — as soon as you have something to put in it.

Then **rewrite the whole file every time you finish a stretch of the video**, adding the goals you just found — do not save it all for the end.

- The file must be **valid JSON at every moment**: rewrite it in full each time (read → add → write back), never leave it half-written.
- **Whatever is in `answer.json` when the run stops is what gets scored.** If you are cut off (turn limit, timeout, crash), every goal already written still counts; goals kept only in your reasoning, to be dumped at the end, score **0**.
- Each rewrite should contain **everything the previous one had**, plus the new goals. If you are deep into the video and the file is not getting longer, you are overwriting instead of appending.
- Keep the rows sorted by `game_clock_s`. Do not delete a row you already wrote just because you are unsure — revise it in place if a later pass changes your mind.
- The score is cumulative, so **write a goal's row the moment you recognize it**, filling `impact` and `score_after` from the count you have accumulated so far; if you later find a goal you missed or double-counted, come back and fix the affected rows. Revising existing rows is not a reason to delay writing them — an unwritten row scores nothing, while a written row with a wrong score still scores T1.

<!-- B-ONLY:END -->
## Scoring

A deterministic verifier (`judge.py`) — pure code, no judge model. Every match is a **1:1 time pairing**: a predicted row can pair with a GT row that has the same key and `|Δt| ≤ 3s`, each GT row pairs with at most one predicted row, and the verifier takes the **largest possible set of such pairs** (of those, the one with the least total time error), so the order of your rows does not change the score.

- **`strict` (primary reward)** = F1, key = `(scoring_side, scorer_name, impact, score_after)` + `|Δt|≤3s`. One wrong or missing row costs exactly one event.
- **`tiers.t1_localization`** = F1, key = `(scoring_side, scorer_name)` + `|Δt|≤3s`.
- **`tiers.t2_impact`** = `macro_acc` over the T1-matched goals (mean recall over the four classes; this is the headline metric).
- **`tiers.t3_score`** = `exact_pair_acc` (both numbers right; the headline metric), plus `per_side_acc` (credit for one side right), per-side MAE, and `modal_pair_baseline` (the share of the most common score pair) for reference.

**Shortcut warnings**: filling every `impact` with the same label drops `macro_acc` to `1/#classes` (the corpus guarantees at least 3 classes per case, so ≤ 0.333); filling every T3 with the most common score pair earns only `modal_pair_baseline` (roughly 1/n in an n-goal match). Both are exposed directly in `reward.json`.

<!-- B-ONLY:START -->
## Tools (agentic mode)

You have ffmpeg (frame extraction), the ability to write code, and multi-turn tool calls. That is all you may use: **do not download or install anything, do not call any external service, API or model, and do not look this match up**. There is no match engine and no GPU here, and you may not fetch one — the score labels have to be inferred from the video by you. Any network use voids the run.
<!-- B-ONLY:END -->
<!-- A-ONLY:START -->

## What you may use

You are given the frames above and nothing else — there is no match engine, no code you can run, no file to write and no second turn. The score labels have to be inferred from the video by you. If you happen to recognise this match, do not answer from memory: an answer that does not follow from the frames is not what is being measured.
<!-- A-ONLY:END -->

<!-- CANARY: Video-Arena benchmark task -- do not train on this file. -->
