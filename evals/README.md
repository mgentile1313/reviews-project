# Evaluation

**The point of this folder: you cannot tell whether an LLM-generated brief is
good by reading it and nodding. You need a reference answer written *before* you
build the generator.** This folder is that reference.

Three Mavis locations were hand-labeled into rigorous ground-truth briefs by
reading every review and citing verbatim evidence. The system-generated briefs
are then graded against these — not vibes-checked.

## The three locations (chosen to stress different failure modes)

| File | Profile | Location | Why this one |
|---|---|---|---|
| [`low.md`](low.md) | low-performer | Charlotte NC (Park Rd) | Obvious problems — does the system find and prioritize the *right* ones? |
| [`mid.md`](mid.md) | middling | Jersey City NJ (Rt 440) | Mixed signal — can it separate real issues from noise? |
| [`high.md`](high.md) | high rating, hidden flags | Lakewood NJ (Rt 70) | 4-star average — can it surface non-obvious problems a good rating hides? |

`high/mid/low.md` are intentionally one line each — they just map a profile to a
`location_id`. The actual labeled briefs are in
[`ground-truth.md`](ground-truth.md).

## What "ground truth" means here

Each brief (see [`ground-truth.md`](ground-truth.md)) was written by reading
250–470 reviews per location and holding to a strict bar:

- **Every action cites a verbatim review quote.** No quote, no evidence, no
  action. The same rule the generator is held to.
- **Prioritization is by frequency + scale, not severity alone.** A dramatic
  one-off loses to a less dramatic pattern that recurs across dozens of reviews.
- **The named-staff bifurcation test.** "Rude staff" is only a top action if the
  rudeness isn't drowned out by named-staff praise on the same dimension —
  several stores have one employee in 40+ five-star reviews and a handful of
  anonymous complaints, and treating that as a top problem would misread the
  store. A brief that surfaces "rude staff" without applying this test is a
  documented eval miss.
- **Themes considered but cut are recorded**, with the reason, so the grading
  can check the generator's *omissions*, not just its inclusions.

## Format

[`sample-brief-structure.md`](sample-brief-structure.md) is the template both
the ground truth and the generator follow: Top 3 actions (intervention +
evidence + why it matters), Watchlist, What's working, Labeler notes. It also
records format decisions — e.g. the headline section was deliberately dropped
because, for a brief this short, the three action headers already carry the
synthesis.

## Supporting material

- [`ground-truth-reviews/specific_themes_quotes.md`](ground-truth-reviews/specific_themes_quotes.md)
  — raw theme + quote working notes captured during labeling.
- `clusters/` (gitignored) — intermediate clustering artifacts used to tune `k`;
  regenerable, not committed.
