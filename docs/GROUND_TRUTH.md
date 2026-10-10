# Ground truth

Every reference tuple in this benchmark comes from the game itself — a replay parser or a pinned
engine — never from a human annotator and never from a model. That is the property that makes the
corpus extensible: a new held-out case costs compute, not labelling, so the 20 published cases per
environment can be refreshed without renegotiating what a label means.

Two mechanisms:

| | How $V_t$ is obtained | Environments |
|---|---|---|
| **Parser** | the game already computes the quantity; the adapter reads it out of the replay, demo, trace, or annotation | CS2, DOTA 2, SC2, GFootball, Badminton |
| **Evaluator** | the game does not define a win probability, so a pinned engine supplies one for every prefix | Go, Chess, Gomoku, Doudizhu |

A parser environment has no engine to pin and no evaluator noise — `score_after` in GFootball is
the engine's own score. An evaluator environment has to pin the engine **and** its search budget,
because a win probability is only reproducible at a fixed configuration; those pins are listed per
environment below.

Sources were last accessed **2026-09-04**.

---

## What every GT file contains

```
tasks/<game>/gt/<case_id>_gt.json
```

One file per case. The reference tuples live under `events` — **the same root key in all nine
games**, and the same key an answer file uses — and each row carries T1's perception fields, T2's
update label, and T3's resulting state side by side. Everything else in the
file is provenance and is not scored: engine pins, thresholds, case type, per-class counts, audits.

Each file also carries a top-level `_canary` string. Please keep it: it is how contamination gets
detected later, and it costs a training pipeline nothing to drop these files.

Row counts, which are also the strict-F1 denominators:

| Game | Cases | Reference tuples | per case |
|---|---|---|---|
| Go        | 20 | 5,516 | 276 |
| CS2       | 20 | 3,703 | 185 |
| Chess     | 20 | 1,761 | 88 |
| SC2       | 20 | 1,358 | 68 |
| Badminton | 20 | 701 | 35 |
| Gomoku    | 20 | 808 | 40 |
| DOTA 2    | 20 | 597 | 30 |
| Doudizhu  | 20 | 429 | 21 |
| GFootball | 20 | 405 | 20 |
| **Total** | **180** | **15,278** | |

### Two conventions that are easy to misread

**A T1-only row is not a missing label.** Go and Chess emit `capture` rows alongside `move` rows. A
capture is a perception event with no $V_t$ update of its own — the move that caused it already
carries that — so its `wr_swing` and `leader_after` are `null` by design (378 of 5,516 rows in Go,
331 of 1,761 in Chess). Those rows are scored at T1 and excluded from the T2/T3 denominators.

**Doudizhu abstains near a band boundary.** A row whose label sits within 2σ of its band edge
carries `null` on that tier and `t2_unresolved` / `t3_unresolved` set (79 rows at T2, 47 at T3, out
of 429). The PIMC estimate cannot separate those from the other side of the band, so scoring them
would be scoring sampling noise. The model **cannot tell which rows those are** and must answer
every row — abstention removes undecidable labels from the numerator; it does not make the task
easier.

---

## T3 is not one metric

T3 asks the same question everywhere — *where does this leave the match* — but what counts as an
answer follows the $V_t$ of the environment:

| T3 answer | Metric | Environments |
|---|---|---|
| a three-way class label | macro accuracy | Go, Chess, Gomoku, Doudizhu, DOTA 2, SC2 |
| an exact integer score pair | exact-pair accuracy | GFootball, Badminton |
| the cumulative score pair after the round | exact fraction | CS2 |

`score.py` prints all three in one T3 column and each judge records which it computed. A class
label where the score is the state would be a weaker question, and an exact pair where $V_t$ is a
win probability would be unanswerable.

T2's class vocabulary is not uniform either — it is whatever a change in that $V_t$ can be:
`{A, B, equal}` on a win probability, four classes (`taken` / `extended` / `levelled` / `narrowed`)
on a score differential, two (`T` / `CT`) on a CS2 round. Chance level therefore differs by game:
0.333 on three classes, 0.25 on four, 0.5 on two. Every judge macro-averages over per-class recall
precisely so a constant answer lands on chance instead of on the majority share — which on Go's T2
would otherwise read **0.87** (4,481 of the 5,138 labelled rows are `equal`).

---

## Case stratification

Cases are not a random sample of matches. Each environment selects 20 from a larger pool so the
corpus contains matches that are hard for different reasons, and so no single class dominates the
T2/T3 labels. Eight of the nine environments stratify by **trajectory shape**, derived from the $V_t$
trajectory itself:

- **blowout** — one side ahead essentially throughout
- **tense** — repeated lead changes
- **reversal** (CS2 and DOTA 2 call it **comeback**) — the eventual winner trailed

| Game | blowout | tense | reversal / comeback |
|---|---|---|---|
| Go        | 5 | 10 | 5 |
| Chess     | 3 | 11 | 6 |
| Gomoku    | 6 | 10 | 4 |
| Doudizhu  | 7 | 7  | 6 |
| CS2       | 8 | 5  | 7 |
| DOTA 2    | 7 | 6  | 7 |
| GFootball | 7 | 6  | 7 |
| Badminton | 6 | 7  | 7 |

**SC2 is the exception** and its `CASE_TYPES.json` carries no trajectory label: resource-equivalent
value rarely reverses inside a game, so within one match `leader_after` is effectively two-class
{leader, equal} and a reversal class would be empty. It balances on **which side led** instead — 10
A-led and 10 B-led — which is what keeps its dataset-pooled T3 from collapsing onto one class
(A 568 / B 531 / equal 259 across 1,358 events).

**The rule that assigns a trajectory type is per-environment, not shared.** It depends on the shape
of that $V_t$: a score-difference $V_t$ makes *reversal* nearly impossible to detect from a trough
(GFootball uses "winner ever trailed" plus a peak threshold), while a win-probability $V_t$ makes
*blowout* the fragile one (Doudizhu keys on the winner-perspective trough at 0.12 / 0.22). Always
smooth before taking an extremum.

Six `CASE_TYPES.json` files carry numeric thresholds — Go, Chess, Gomoku, CS2, and DOTA 2 the stratification thresholds they used, SC2 its event-segmentation ones. Doudizhu carries the note that its thresholds are *fixed* across the corpus (re-tuning ε per case would stop macro accuracy being comparable between cases), and GFootball and Badminton record only the resulting distribution. All nine carry the per-case label (SC2's is `leading_side`). The $V_t$ definition is in every game's `instruction.md` and in the per-environment sections below; `CASE_TYPES.json` repeats it for Doudizhu, GFootball, Badminton, and SC2.

---

## Per environment

### Go — board · evaluator

| | |
|---|---|
| **Source** | SGF records via the OGS public API, fixture game 27400000 — <https://online-go.com> |
| **GT mechanism** | evaluator. The adapter replays each move and evaluates **every prefix** |
| **Engine pin** | KataGo, network `b18c384nbt`, **1600 visits** per position |
| **$V_t$** | Black win probability, **±10 pp** dead band around 0.50 |
| **Render** | plain 19×19 board, 612², 25 fps. No coordinates, move numbers, names, clocks, capture counters, komi, or evaluation bar |

T1 is the move or capture with its time, colour, and board coordinate. T2 is the quantized signed
change in Black's win rate caused by the move (`wr_swing`). T3 is the win-rate leader immediately
after it (`leader_after`). Selection balances the winning colour 10 B / 10 W.

Go is the largest environment by tuples per case (276), so it is where a model's ability to hold a
whole match in view is most exposed — and where Protocol A's frame grid bites hardest.

### Chess — board · evaluator

| | |
|---|---|
| **Source** | public Lichess game export, fixture `cZWtZEBE` — <https://lichess.org> |
| **GT mechanism** | evaluator, per-ply, with real per-ply timing |
| **Engine pin** | **Stockfish 18, depth 20**, value function `white_expected_score_wdl` |
| **$V_t$** | White-oriented expected score / WDL, **±10 pp** band. Draws map to the neutral label |
| **Render** | plain 8×8 board, 548², 1 fps. No coordinates, move numbers, names, clocks, result text, captured-piece tray, or evaluation bar |

T1 records the **destination square only**, not the origin — the released contract is deliberately
weaker than full move notation. T2 is the quantized change in the White-oriented value; T3 is the
post-ply leader.

The video is natively 1 fps because a ply is a discrete event; Protocol A's sampler is clamped by
the container rate, so it never asks ffmpeg to fabricate frames here.

### Gomoku — board · evaluator

| | |
|---|---|
| **Source** | competition PSQ records (Gomocup) |
| **GT mechanism** | evaluator |
| **Engine pin** | **Rapfi**, 1000 ms per turn, freestyle rule, `black_winrate_sigmoid_eval` (scaling factor 200) — <https://github.com/dhbloo/rapfi> |
| **$V_t$** | **tempo-neutral** smoothed Black win probability, ±10 pp band |
| **Render** | plain 15×15 board, 504², 25 fps. No coordinates, move numbers, names, clocks, or engine annotations |

**Why the smoothing matters.** Gomoku is attack-dominant, so a raw side-to-move win rate swings
back and forth with whose turn it is — a square wave that has nothing to do with who is actually
ahead. Left alone it turns "who leads" into "who moves next", which is a **move-parity shortcut**,
readable off the stone count with no understanding of the position. The release applies
`v_smooth[t] = (v[t] + v[t-1]) / 2` (`trailing2_movepair`) to cancel it, then gates on terminal
consistency. All 20 cases share that pin.

### Doudizhu — card · evaluator

| | |
|---|---|
| **Source** | DouZero-WP greedy self-play deals (τ=0) — <https://github.com/kwai/DouZero>, weights at <https://huggingface.co/palemoky/douzero-baselines> |
| **GT mechanism** | evaluator: **hidden-hand PIMC** — determinize, then roll out |
| **Estimator** | adaptive **400–4000 worlds** per state, greedy rollouts with the released DouZero-WP policy. The PIMC estimator is part of this benchmark, not a DouZero component |
| **$V_t$** | landlord win probability = fraction of sampled deals the landlord wins. Bands: landlord > 0.55, farmers < 0.45, else equal. T2 impact ε = 0.034, T3 band = 0.05 |
| **Render** | tabletop, 1280×720, 24 fps. Landlord hand and the three bottom cards face up; **farmer hands face down**, showing only their counts. No numeric score, timer, move counter, or probability display |

**Hiding the farmer hands is what makes $V_t$ non-degenerate.** With every hand visible the value is
close to a deterministic function of the cards, and the task collapses into card counting. The
evaluator is given exactly the landlord's information set — own hand, the bottom three, all plays
so far, and both farmer card *counts* — and samples uniformly from the deals consistent with it.

That leaves one shortcut worth naming, because it is the one a model will find: *"whoever holds
fewer cards is ahead."* Card counts are public, so it is tempting. Measured over 9,954 events, it
buys only **+0.05–0.09** macro accuracy and is at its worst in the endgame — exactly when the
landlord's hand is also small but the outcome runs the other way. Across this corpus it is capped
at **macro_acc ≤ 0.45**.

T1 is every move that plays ≥1 card, with the player, combination type, and card count; passes are
shown on screen but never reported. Each play occupies a 6 s display window and `game_clock_s` is
its midpoint, so the ±3 s tolerance is exactly half a window — any time inside the window pairs up,
with no ambiguity across plays.

### Counter-Strike 2 — FPS · parser

| | |
|---|---|
| **Source** | public professional-match demo (`.dem`) files published on HLTV; the game is <https://www.counter-strike.net> |
| **GT mechanism** | parser, our own, at the demo's **native 64 Hz** tick rate; positions resampled to 4 Hz for the render |
| **$V_t$** | the **cumulative round score**, by persistent team (A / B) across the halftime side swap |
| **Render** | clean top-down radar, 768², 15 fps, 1:1 real pace. Map geometry, player motion, bomb state, and event-relevant effects preserved; HUD, scoreboard, round counter, kill feed, economy, names, and audio removed |

T1 covers six event types — `kill`, `bomb_planted`, `bomb_defused`, `bomb_exploded`, `elimination`,
`save` — with their time and type-specific attributes. **On a `kill`, the side and the dot number
1–10 identify the victim, not the killer**, because the death marker is rendered without team colour
or number. A `kill` row therefore carries `side` and `number`; the four conclusion types carry
`winner_side` instead, and `bomb_planted` carries the planter's `side`.

T2 is the winner of the round containing the event; T3 is the cumulative score right after that
round. Both are carried **on every event row** and are round-constant, rather than exposed as
separate per-round fields.

The retained bomb/countdown corner clock is the only on-screen number in the whole corpus, and it is
deliberate: it is the round timer, not the state.

> **`score_after` is cumulative, so its errors propagate.** One early round mis-attributed shifts
> every later row's `score_after`, and because strict F1 voids a tuple on any wrong field, a single
> early mistake can cost the rest of the match. This is the intended difficulty — tracking means
> tracking — but read a near-zero CS2 strict F1 against its T1, which is scored independently.

**20 cases**: 8 blowout / 5 tense / 7 comeback, listed in `tasks/cs2/CASE_TYPES.json`.

### DOTA 2 — MOBA · parser

| | |
|---|---|
| **Source** | Valve replays plus OpenDota match records — <https://www.opendota.com>, <https://github.com/odota> |
| **GT mechanism** | parser (manta-based) for events, OpenDota's minute-level gold curve for the state |
| **$V_t$** | the **parsed net-worth advantage** — explicitly *not* a learned win-probability model. `even` = within **±1000 gold** |
| **Render** | the real minimap terrain art with GT-anchored coordinates and real game sprites, 768², 15 fps. HUD, scoreboard, clock, net-worth graph, names, and objective counters removed |

T1 covers six event types — first blood, teamfight, tower, barracks, Roshan, ancient — each with
its own attributes (side, killer or winner, death counts, tier, lane, objective type). T2 quantizes
the change in net-worth advantage over the aligned **minute-level** interval (`minute_gain`); T3 is
the net-worth leader at that interval's end (`leader_after`).

**Time axis.** Every DOTA 2 video starts **8 s before the horn** (`video.preroll_s = 8.0`, speed 1.0, in all 20 cases), so `video_ts = max(0, game_s) + 8` and the event's minute is `m = floor(max(0, video_ts − 8) / 60)` — this reproduces the GT minute for all 597 events, where the older `floor(video_ts / 60)` agreed on only 521. Events the parser dates **before** the horn are clamped to the horn, `video_ts` = 8.0, even when they fall before the video's first frame (game second −8) — eight rows: first blood in dota-02/03/04/05/06/07, plus a pre-game teamfight in dota-04 and dota-06. The dota-04 teamfight (game second −47) carries `deaths: 0` and `winner_side: even`; it is kept as the parser emitted it rather than edited by hand, so read a miss on that one row with this in mind.

Using the parsed gold curve rather than a win model is a deliberate choice: a learned win
probability would make the reference depend on a model, and then the benchmark would be scoring
agreement with that model rather than with the game.

> **Its T2/T3 macro is computed over *matched* events only** (as is SC2's). Low T1 recall therefore
> shrinks the T2/T3 denominator and can **delete the hard classes — especially `equal` — from it**,
> so a T3 number backed by one surviving class is not comparable to one backed by three. The other
> seven judges count an unlocalized event as wrong instead. `score.py` prints T1 next to T2/T3 so
> the two can be read together.

DOTA 2 is the longest environment (47.8 ± 15.4 min), which is why its Protocol A recall ceiling is
the lowest in the corpus at **0.597**.

### StarCraft II — RTS · parser

| | |
|---|---|
| **Source** | engine-generated AI-vs-AI replays (difficulty *Harder*, 6 matchups, 2017 ladder maps). Replay protocol: <https://github.com/Blizzard/s2client-proto> |
| **GT mechanism** | parser — the replay **tracker stream**, read during a `disable_fog` god-view replay pass; deaths from raw `dead_units` (morph-free) |
| **Engine** | SC2 **4.10, base build 75689** (Blizzard Linux research build) via PySC2, rendered with EGL |
| **$V_t$** | **resource-equivalent advantage share** $s=(V_A-V_B)/(V_A+V_B)\in[-1,1]$, band **±0.10**. $V$ = banked minerals + gas, live army value, economy value, technology value |
| **Render** | full-map isometric observer god-view, 1536², **5.6 fps**, real time. Resource counters, production panels, minimap, names, and other direct readouts removed |

T1 is a `battle` or `building_destroyed` event with its time and **coarse map region**: the playable
area is quantized into a 3×3 grid written `r{row}c{col}`, so the label names the region a viewer
would point at rather than a pixel. Thresholds: battles merge deaths within 2.5 s and need ≥150
resource value; a building death needs ≥80. T2 (`fight_result`) is the banded net army value
traded; T3 (`leader_after`) is the banded share 3 s after the event resolves.

Because the source games are engine-generated they were **never public**, which makes this
environment contamination-free by construction.

> **Each released case is ONE 1536² full-map video.** The 3×3 `tile_r{r}c{c}.mp4` files are internal
> to the render pipeline and are not part of the release — and the `entity` grid is a quantization
> of the playable *area*, not a reference to a video tile. Frames are captured every 4 game loops
> (≈0.18 s) so the ±3 s tolerance is physically achievable; the container fps is cosmetic, since
> event timing comes from the tracker stream.

### GFootball — sports · parser

| | |
|---|---|
| **Source** | randomized Google Research Football simulator episodes (bot vs bot) — <https://github.com/google-research/football> |
| **GT mechanism** | parser. Score transitions from the engine's `obs['score']`; the scorer name from the engine's own goal log |
| **$V_t$** | `left_goals - right_goals`, the engine's native score. No evaluator |
| **Render** | the engine's default inclined 3D broadcast view, full **1280×720, 24 fps**, uncropped, real time (1 engine step = 1 frame) |

T1 is the goal with its time, scoring side (`L` / `R`), scorer surname, and own-goal flag. T2 is the
four-class score-differential change — `taken`, `extended`, `levelled`, `narrowed` — from the
pre-goal difference. T3 is the **exact score pair** after the goal.

**The HUD is suppressed inside the engine**, in a patched wheel (`GFOOTBALL_HIDE_HUD=1`), not
painted over afterwards. The minimap and the floating scorer-surname label above the ball carrier
**remain visible** and are part of the released visual contract — the surname label is the only
readable source for `scorer_name`.

Own goals are admitted rather than filtered, so `scoring_side` is the side that *benefits*, which
is not always the side that touched the ball last.

> Reading the scorer correctly took three attempts, and the two failures are instructive about
> trusting an engine field: `ball_owned_player` on the goal frame is `-1` (nobody owns the ball at
> that instant), and "last owner" attributes a one-touch shot to the passer. The release reads the
> engine's own `VA_GOAL_LOG` line, pairs it in order, and **fails rather than falls back** if the
> `score=` in that line disagrees with the score chain.

### Badminton — sports · parser

| | |
|---|---|
| **Source** | **ShuttleSet** stroke records (KDD-23) — <https://github.com/wywyWang/CoachAI-Projects>, <https://arxiv.org/abs/2306.04948> |
| **GT mechanism** | parser: strokes grouped into points, the native score carried through each rally |
| **$V_t$** | `pts_near - pts_far` within the set. No evaluator |
| **Render** | fixed-camera **synthetic 3D**, 1280×720, 15 fps. A synthetic render built from real trajectories, **not** a source replay |
| **Granularity** | one set of a singles match |

T1 is the point with its time, scoring side (`near` / `far`, relative to the fixed camera), and the
terminal landing zone — one of `net`, `out`, `in_front`, `in_mid`, `in_back`. T2 is the four-class
score-differential change; T3 is the exact score pair after the point.

Two properties of this environment shape the task more than the schema does:

- **Two thirds of the video is dead time** (`dead_frac` 0.54–0.72, mean 0.66). Between rallies the
  two figures simply walk to the next serving position. It looks exactly like live play apart from
  the shuttle not being struck, and there is no visual marker for the end of a rally — so the point
  boundary has to be read from the players' actions. Consecutive points are ≥13 s apart across all
  681 intervals in the corpus, so a ±3 s window can never straddle two of them.
- **`in_back` is a 0.76 m band.** All three dividing lines for the `in_*` classes are painted lines
  (the front service line and the doubles back service line, which is on the court even in
  singles). `in_back` is rare; answering it because a shot "looks deep" is wrong unless the shuttle
  actually crossed that line.

Because the renderer is synthetic, its HUD guarantee is **structural** rather than measured: this
module never reads a score column, so no flag can turn one on, and no font is ever loaded, so there
are no glyphs to render.

> **Licence notice travels with the data.** ShuttleSet ships no `LICENSE` of its own; it inherits
> MIT (© 2022 Wei-Yao Wang) from the CoachAI-Projects repository root, which is what makes these
> derived renders redistributable. The full notice is in `tasks/badminton/ATTRIBUTION.md` and must
> accompany any redistribution.
