# Agent instruction — Dou Dizhu three-tier reconstruction

You will watch a video of a Dou Dizhu (斗地主) game. On screen you can see: **the landlord's entire hand (face up)**, the two farmers' hands (**face-down fans — you can count the cards but cannot see their ranks**), the three bottom cards (face up throughout), the seat labels (`LANDLORD` in gold / `FARMER-L` / `FARMER-R`), the cards already played in the current trick, and the **current player** picked out by a gold highlight frame. Every play (including a pass) is displayed for 6 seconds; the game opens with a 12-second static reveal.

How to read the cards: standard English playing-card faces. The rank and suit are printed in the **top-left corner of each card** (rank above, suit below); hearts/diamonds are red, spades/clubs are black. When cards in a hand overlap, only a strip down the left edge of each is visible, so **the top-left rank is the only reliable place to read it**. Fully exposed cards (the last card in each row, the three bottom cards, and the cards played in the centre of the table) also show the complete face — 2–10 in the standard pip layout, A as one large suit symbol, and J/Q/K as face-letter frames.

The backs of the farmers' cards keep a fully opaque white border, and the period of the back pattern deliberately does not divide the overlap step — the point being that a 17-card fan is **still countable** and does not blur into a single mass. The farmers' **remaining card counts** are therefore public information, while their **specific ranks** are not.

**Suits are irrelevant in Dou Dizhu**; they exist only so that multiples of the same rank can be counted at a glance (four of a rank = four suits = a bomb). The two jokers have neither rank nor suit and are printed with `JOKER` running vertically: **black lettering is the little joker, red lettering is the big joker**.

A pass is shown as an empty dark `PASS` panel. When a player acts again within the same trick, that player's panel shows only their **most recent** action.

**Not on screen**: the farmers' actual hands, win probability, any score, a timer, a move counter, or numeric remaining-card counts (you have to count those yourself).

## Your task

Reconstruct the ordered event list of **every play** (passes are not reported). Each event carries:

| Field | Meaning | Values |
|---|---|---|
| `idx` | Event index (1-based) | integer |
| `game_clock_s` | The second at which this play appears in the video | float |
| `player` | Who played | `landlord` / `farmer_left` / `farmer_right` |
| `move_type` | The combination type | see the table below |
| `num_cards` | How many cards this play used | integer |
| `impact` | **T2** which way this play pushed the advantage (ΔV) | `landlord` / `farmers` / `neutral` |
| `leader_after` | **T3** who is ahead **after** this play (V) | `landlord` / `farmers` / `equal` |

### `move_type` values (every play you report must fall into these 14 classes)
`single`, `pair`, `trio`, `trio_single` (trio plus a single), `trio_pair` (trio plus a pair), `straight` (a run of ≥5 consecutive singles), `pair_chain` (≥3 consecutive pairs), `trio_chain` (consecutive trios with no kickers, ≥2), `plane_single` (consecutive trios with single kickers), `plane_pair` (consecutive trios with pair kickers), `bomb` (four of a rank), `rocket` (both jokers), `quad_two` (four plus two singles), `quad_two_pairs` (four plus two pairs).

**The one thing you do not report is a pass.** Reporting a pass as an event counts as a false positive and lowers your precision.

## What the three tiers mean

The three tiers are one ladder, not three separate things: **T1 is the key event itself, T2 is the change this event caused in V, and T3 is the overall state of V after the event has happened.** Every game in the suite (football / Go / Dou Dizhu / CS2 / DOTA2 …) shares these semantics.

- **T1 — localize (t, c, a)**: when, who, which combination type, how many cards. This is the entry ticket, and all of it can be counted directly off the screen.
- **T2 — impact (ΔV)**: how much this play moved the landlord's win probability. `|ΔV| ≤ 0.034` → `neutral`; `ΔV > +0.034` → `landlord`; `ΔV < −0.034` → `farmers`.
- **T3 — situation (V)**: which side of the band the landlord's win probability V falls on **after** this play. `V > 0.55` → `landlord`; `V < 0.45` → `farmers`; in between → `equal`.

T3 asks about **after**, not before: it is the state that T2's increments accumulate **to**, as in the unified semantics above. Because Dou Dizhu's V is continuous (measured `|ΔV|` spans 0.0005–0.895), the binned T3 is **not** a deterministic function of the binned T2.

The thresholds are much smaller than in an open-hand version, and that is by design: when the farmers' hands are invisible, changes in V come from changes in **belief**, and a single play rarely rewrites an entire belief, so a single play's `|ΔV|` is naturally smaller. The 0.034 threshold is the p33 of the measured `|ΔV|` distribution over 60 seeds (after subtracting sampling noise) — it was not picked by hand.

## What V(t) is, and why you cannot read it off a single frame

V(t) = **the landlord's win probability in the current position** — the share of games the landlord would win from here, averaged over every way the unseen cards could be split between the two farmers, with all three players then playing at the same strong level. It is conditioned on exactly the **landlord's information set**:

1. the landlord's own hand and the three bottom cards are known;
2. every card all three players have already played is known;
3. the two farmers' remaining **card counts** are known;
4. only **which specific cards each farmer holds** is unknown, and every split of the unseen stock consistent with 1–3 is weighted equally.

**The key point: the information on screen = the landlord's information set = the information set V is conditioned on.** So V remains entirely a function of what is on screen — but it demands that you simultaneously **evaluate hand strength** and **reason about hidden hands**. No single frame yields this number, and it is never displayed.

The three bottom cards being face up throughout is not decoration: V is conditioned on them, so if they were invisible, two visually identical positions could carry different V and the labels would depend on something off-screen. Real Dou Dizhu clients display them this way too.

## Some cells are "not scored"

For a few events, the ΔV or V lands so close to a bin boundary that which side of it they fall on is not decidable — the margin is a fixed number, set in advance and the same for every case. Labels like these **are not scored** (counted neither right nor wrong). You **cannot tell** which ones they are, so **fill in every row as usual**; this only removes the undecidable labels from the denominator, and it does not make the task easier.

## Answer format

<!--B-->Write `answer.json`:<!--/B--><!--A-->The answer is one JSON object:<!--/A-->
```json
{"events": [
  {"idx":1,"game_clock_s":34.0,"player":"landlord","move_type":"straight",
   "num_cards":5,"impact":"landlord","leader_after":"landlord"},
  {"idx":2,"game_clock_s":88.0,"player":"farmer_left","move_type":"bomb",
   "num_cards":4,"impact":"farmers","leader_after":"equal"}
]}
```
- `game_clock_s` is in video playback seconds; use the **midpoint of the play's display window** (each play occupies 6 seconds, and the midpoint is the most robust choice). Tolerance is **±3 s**, exactly half a window: as long as the time you report falls inside the 6 seconds during which that play is actually on screen, it will pair up, with no ambiguity across plays.
- Per-event all-or-nothing: if any (labelled) field in a row is wrong, that row scores nothing.
- Value matching is case-insensitive, and spaces and `-` are treated as `_`. A few alternate names are also accepted (for example `king_bomb` for `rocket`, `four_two` for `quad_two`, the `serial_*` family, and the Chinese glosses such as `炸弹`/`顺子`/`地主`/`农民`). Prefer the canonical values in the tables above.

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
- `impact` and `leader_after` need more information than the play itself. **Fill both labels with your best current judgement the first time you write the row**, then come back and revise. Revising existing rows is not a reason to delay writing them — an unwritten row scores nothing, while a written row with a wrong label still scores T1.

<!-- B-ONLY:END -->
## Scoring
A deterministic verifier (`judge.py`) — pure code, no judge model:
- **`strict` (primary reward)** = F1: rows are first paired on `(player, move_type, num_cards)` with `|Δt| ≤ 3s`, then the row must get every tier **that GT has a label for** right.
- **`t1_localization`** = F1, key = `(player, move_type, num_cards)` + `|Δt| ≤ 3s`.
- **`t2_impact` / `t3_situation`** = macro accuracy (`macro_acc`, the mean of per-class recalls) over the events that T1 paired up.

**Shortcut warnings**:
- Filling every T2 with `neutral`, or every T3 with `landlord`, drops `macro_acc` to 0.333 (chance level).
- The field name must be `leader_after`. `leader` is also accepted; **`leader_before` is not** — it means a different quantity (the situation *before* the play), so the verifier records it in `detail.answer_rows_using_leader_before` and treats that row's T3 as unanswered.
- **"Whoever holds fewer cards is ahead" is not enough either.** Card counts are public and this shortcut looks tempting, but measurement (9954 events over 600 games) shows it buys only +0.05–0.09 macro accuracy, and it is at its worst in the endgame — precisely when the landlord's hand is also small but the outcome runs the other way. Across the whole corpus this shortcut is capped at `macro_acc ≤ 0.45`. You have to genuinely track how hand strength and belief evolve.

<!-- B-ONLY:START -->
## Tools (agentic mode)
You have ffmpeg (frame extraction), the ability to write code, and multi-turn tool calls. That is all you may use: **do not download or install anything, do not call any external service, API or model, and do not look this game up**. There is no card-play engine and no GPU here, and you may not fetch one — the win-probability labels have to be inferred from the video by you. Any network use voids the run.
<!-- B-ONLY:END -->
<!-- A-ONLY:START -->

## What you may use

You are given the frames above and nothing else — there is no card-play engine, no code you can run, no file to write and no second turn. The win-probability labels have to be inferred from the video by you. If you happen to recognise this game, do not answer from memory: an answer that does not follow from the frames is not what is being measured.
<!-- A-ONLY:END -->

<!-- CANARY: Video-Arena benchmark task -- do not train on this file. -->
