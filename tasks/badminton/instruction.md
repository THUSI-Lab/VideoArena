# Agent instruction — Badminton three-tier reconstruction

You are given a video of one full badminton singles set (roughly 15–25 minutes), **rendered synthetically** from a real BWF match's shot-by-shot annotation (ShuttleSet, KDD-23): the rally structure, who won each point and how it ended are real; the picture is a 3D reconstruction, not match footage. Nothing in it is a HUD — no scoreboard, no set count, no name bars, no tournament bug, no logo, and **no readable number or text anywhere**, because the renderer never loads a font and never reads a score column. **The score is never visible in the video.** There is **no audio track**.

The picture has exactly ONE state and never cuts:

- **One fixed camera, one continuous shot**: an elevated view of the whole court with both players, the net and every court line visible, from the first frame to the last. There are no camera cuts, no replays, no close-ups, no bench or crowd shots, no adverts, no graphics and no mosaicked or blurred stretches. If a stretch looks uneventful it is not a cutaway and not a corrupt file.
- **Between rallies the picture keeps running.** Roughly half of the video is dead time in which the two figures simply walk to the next serving position and stand still. It looks exactly like live play apart from the shuttle not being struck — there is no visual marker telling you the rally is over, which is why the point boundary has to be read from the players' actions.

**The 11-point technical timeout is rendered as-is** (a longer stand-still), and it is part of the task, not noise.

## Your task

Reconstruct the ordered list of scoring events in this set. Each point carries 5 fields:

| Field | Tier | Meaning | Values |
|---|---|---|---|
| `game_clock_s` | T1 | The moment this point **is decided**, in seconds on the **video playback timeline** | float, tolerance ±3 s |
| `scoring_side` | T1 | The side that won this point, named by **distance from the camera** | `near` (near end / camera side) / `far` (far end) |
| `zone` | T1 | The **terminal landing zone** of this point | `net` / `out` / `in_front` / `in_mid` / `in_back` |
| `impact` | T2 | What this point changed about who leads | `extended` / `taken` / `levelled` / `narrowed` |
| `score_after` | T3 | The score **after** this point | two-element array `[near score, far score]` |

You may include `idx` (1-based) and `type` (always `"point"`); neither is scored.

## What the three tiers mean

- **T1 — localization**: when (`game_clock_s`), which side scored (`scoring_side`), and where the shuttle finished (`zone`). This is the entry ticket: T2/T3 are only scored on points that T1 matched correctly, and a point T1 never found has its T2/T3 counted wrong.
- **T2 — impact** (ΔV): let `u` = the **scoring side's** point difference immediately *before* this point (scoring side's points − opponent's points). Then

  | Condition | Label | Meaning |
  |---|---|---|
  | `u > 0` | `extended` | Already ahead, stretching the lead |
  | `u == 0` | `taken` | Level before, now in front |
  | `u == -1` | `levelled` | One behind, now equal |
  | `u <= -2` | `narrowed` | Closer, but still behind |

  Note that `levelled` and `narrowed` differ **only in how far behind** the scoring side was: knowing "who is ahead" cannot separate these two classes — you have to genuinely remember the score numbers.

  `u` uses the point difference **before** this point, because this tier asks "what did this point change"; T3 asks "what does the situation look like after the change". The two tiers look at opposite sides of the same jump — do not conflate them.
- **T3 — situation** (V): `score_after`, the **exact score pair after** this point. Not "who leads" — two integers. The first point's `score_after` is always `[1,0]` or `[0,1]`, and the last point's `score_after` is the final score of this set (games to 21, win by 2 after 20-20, capped at 30).

## How the landing zone (`zone`) is defined

The boundaries of these five classes are all **lines painted on the court**, and you can see them in the picture. The call is based on "where the shuttle was when it landed (or the rally died)", not on "who hit it".

| Value | Criterion | Frequency (measured over the whole corpus) |
|---|---|---|
| `net` | The shuttle did not cross the net: into the net, or down before reaching it. This point is **lost by the hitter**, and the shuttle dies in his own half | 0.356 |
| `out` | The shuttle landed outside the singles court (beyond a sideline or the baseline) | 0.287 |
| `in_front` | In court, **between the net and the front service line** (the 1.98 m in front of the net) | 0.054 |
| `in_mid` | In court, **between the front service line and the doubles back service line** | 0.230 |
| `in_back` | In court, **beyond the doubles back service line** (the narrow 0.76 m band inside the baseline) | 0.073 |

All three dividing lines for the `in_*` classes are solid lines: the front service line, and the doubles back service line (which is painted on the court even for a singles match). The `in_back` band is only 0.76 m wide, which is why it is rare; do not answer `in_back` just because a shot "looks deep" — check whether it actually crossed that line.

**`zone` deliberately does not encode near/far**: a shuttle that lands to win a point must land in the **opponent's** half, while a shuttle that hits or fails to clear the net dies in the **losing side's own** half — in both cases which half it landed in is uniquely determined by `scoring_side`, so restating it would be giving marks away. All you need to judge is depth, in/out, and whether it cleared the net.

## Why you must count from the very start

The score is never shown on screen. You have to recognize the end of every point yourself, decide who won it, and **accumulate point by point from 0-0** before you can answer T2/T3. The signals available to you:

- After a point ends both figures walk back to their serving positions and stand; the camera does not cut away, so the walk itself is on screen. The next point starts from a **serve**, and the serving action (one player hitting the shuttle over the net underarm) is the most reliable marker that a new point has begun.
- **The server is the winner of the previous point.** This is a rule, not a coincidence, and you can use it to cross-check your reading of who scored.
- **Which service court (left or right) the serve comes from is set by the parity of the server's current score** (even score serves from the right court, odd from the left). This is also a rule, and you can use it to audit the score you have counted — if you think a player is on 7 points but he is serving from the right court, your count is wrong.
- The two players **do not change ends** during this set (BWF rules only change ends at 11 in the third game, and no case in this corpus is that situation). So once you establish near/far, it holds for the whole set.

**There are no replays, so nothing is ever shown twice.** Every rally in the video happens once, in order, at normal speed. What can still cost you a double count is the dead time between points: a walk back to the serve position is not a rally, and a player picking the shuttle up is not a point being decided. Count exactly one event per serve-to-rally-end cycle.

## Answer format

<!--B-->Write `answer.json`:<!--/B--><!--A-->The answer is one JSON object:<!--/A-->

```json
{"events": [
  {"type": "point", "idx": 1, "game_clock_s": 14.9, "scoring_side": "near",
   "zone": "in_back", "impact": "taken", "score_after": [1, 0]},
  {"type": "point", "idx": 2, "game_clock_s": 48.3, "scoring_side": "far",
   "zone": "net", "impact": "levelled", "score_after": [1, 1]}
]}
```

- `game_clock_s` is in **video playback seconds**, not any on-court clock. Tolerance ±3 s; the corpus guarantees that any two consecutive points are more than 10 s apart, so a ±3 s window can never straddle two points.
- A missing field counts as wrong: the `strict` key is the tuple of four fields, and a row with any of them empty can never match. Prefer a reasoned guess over a blank.
- `scoring_side` also accepts `bottom`/`top`/`n`/`f`/`0`/`1` (and the Chinese glosses `近`/`远`); `zone` also accepts common variants such as `front`/`mid`/`back`/`deep`/`out_of_bounds`/`into_net` (and Chinese glosses such as `挂网`/`出界`/`网前`/`中场`/`后场`); `impact` also accepts `extend`/`equalised`/`narrow`/`reduced` (and glosses such as `追平`); `score_after` also accepts a string like `"1-0"`, and the field itself may be named `score` or `scoreline`. Prefer the canonical values in the tables above.
- **Do not write the score from before the point.** T3 is explicitly the score **after** this point, and `score_before` is not accepted as an alias (it differs from `score_after` by exactly one point, so accepting it would be giving marks away). If the rows you submit carry only `score_before`, `reward.json` reports the count in `detail.answer_rows_score_before_only`.

<!-- B-ONLY:START -->
## Write `answer.json` as you go (required)
**`./answer.json` is already in the sandbox** — the harness created it before your first turn, with the schema above and an empty list:

```json
{"events": []}
```

**Do not delete it, do not rename it, and do not change the root key.** Overwrite it in place — in full, valid JSON every time — as soon as you have something to put in it.

Then **rewrite the whole file every time you finish a stretch of the video**, adding the points you just found — do not save it all for the end.

- The file must be **valid JSON at every moment**: rewrite it in full each time (read → add → write back), never leave it half-written.
- **Whatever is in `answer.json` when the run stops is what gets scored.** If you are cut off (turn limit, timeout, crash), every point already written still counts; points kept only in your reasoning, to be dumped at the end, score **0**.
- Each rewrite should contain **everything the previous one had**, plus the new points. If you are deep into the video and the file is not getting longer, you are overwriting instead of appending.
- Keep the rows sorted by `game_clock_s`. Do not delete a row you already wrote just because you are unsure — revise it in place if a later pass changes your mind.
- The score is cumulative, so **write a point's row the moment you recognize it**, filling `impact` and `score_after` from the count you have accumulated so far; when "the server is the previous point's winner" or "the service court follows score parity" later reveals a point you missed or double-counted, come back and fix the affected rows. Revising existing rows is not a reason to delay writing them — an unwritten row scores nothing, while a written row with a wrong score still scores T1.

<!-- B-ONLY:END -->
## Scoring

A deterministic verifier (`judge.py`) — pure code, no judge model. Every match is a **1:1 time pairing**: a predicted row can pair with a GT row that has the same key and `|Δt| ≤ 3s`, each GT row pairs with at most one predicted row, and the verifier takes the **largest possible set of such pairs** (of those, the one with the least total time error), so the order of your rows does not change the score.

- **`strict` (primary reward)** = F1, key = `(scoring_side, zone, impact, score_after)` + `|Δt|≤3s`. One wrong or missing row costs exactly one event.
- **`tiers.t1_localization`** = F1, key = `(scoring_side, zone)` + `|Δt|≤3s`.
- **`tiers.t2_impact`** = `macro_acc` over the T1-matched points (mean recall over the four classes; this is the headline metric).
- **`tiers.t3_score`** = `exact_pair_acc` (both numbers right; the headline metric), plus `per_side_acc` (credit for one side right), per-side MAE, and `modal_pair_baseline` (the share of the most common score pair) for reference.

**Shortcut warnings** (all exposed directly in `reward.json`):

- Filling every `impact` with the same label drops `macro_acc` to `1/#classes`, which is 0.25 when all four classes are present.
- Filling every T3 with the most common score pair earns only `modal_pair_baseline` (measured at 0.0158 over the whole corpus).
- Writing T3 scores that merely "look plausible" without actually counting is exposed by `detail.self_consistent_pairs`: it checks whether the `score_after` values you gave are consistent with accumulating the `scoring_side` sequence you gave. An answer that really counted sits around 0.97 even if it is off by one point; one that did not is close to 0.
- Getting near and far systematically backwards is reported by `detail.side_swapped`, which says what you would have scored if every near/far were swapped. It **does not count towards your score**, but it separates "did not understand who won the point" from "understood, but mirrored the court".

<!-- B-ONLY:START -->
## Tools (agentic mode)

You have ffmpeg (frame extraction), the ability to write code, and multi-turn tool calls. That is all you may use: **do not download or install anything, do not call any external service, API or model, and do not look this match up** — not the players, not the tournament, not the scoreline. This is a real televised match, so a search could hand you the answer; doing that voids the run. The score has to be inferred from the video by you.
<!-- B-ONLY:END -->
<!-- A-ONLY:START -->

## What you may use

You are given the frames above and nothing else — there is no analysis tool, no code you can run, no file to write and no second turn. The score has to be inferred from the video by you. If you happen to recognise this match, do not answer from memory: an answer that does not follow from the frames is not what is being measured.
<!-- A-ONLY:END -->

<!-- CANARY: Video-Arena benchmark task -- do not train on this file. -->
