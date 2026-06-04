# Sample brief structure

Reference template for hand-labeled ground-truth briefs. One brief per location.
The system-generated briefs will be evaluated against briefs written in this format.

**Headline finding section is deliberately omitted.** Decided 2026-05-28 during the
first brief: for a brief this succinct, the three action headers already carry the
synthesis. A weak headline becomes editorial fluff. Skipped on this brief and all
future ones.

---

# Ground Truth Brief — [Location Name, City State]

**Location ID:** [uuid]
**Profile:** [low-performer / middling / deceptive]
**Reviews read:** [N] reviews, date range [oldest] to [newest]
**Labeler:** Matt (with Claude as structural scribe)
**Date labeled:** 2026-XX-XX

---

## Top 3 actions

Order reflects priority. Priority is set by frequency + content scale in the reviews,
not by severity alone. Each action must be implementable by the store manager after
reading — not a reworded theme.

### 1. [Imperative verb] [specific intervention]

**Intervention:** [1–3 sentences describing the concrete change. Who does what, when,
and how it's verified. If multiple sub-actions, label them (a) and (b). If an
alternative action was considered and rejected, name it here in one line.]

**Evidence:** "[verbatim quote from a specific review]" — [google/yelp], [YYYY-MM-DD], [N]★ (~[N] other reviews echo this pattern[, optionally: brief specifier])

**Why it matters:** [1–2 sentences. The business consequence — revenue, retention,
liability, brand, safety. Not a restatement of the complaint. Must explain why fixing
this matters more than fixing the cut themes.]

### 2. [Imperative verb] [specific intervention]

**Intervention:** [...]

**Evidence:** "[verbatim quote]" — [source], [date], [N]★ (~[N] other reviews echo this pattern)

**Why it matters:** [...]

### 3. [Imperative verb] [specific intervention]

**Intervention:** [...]

**Evidence:** "[verbatim quote]" — [source], [date], [N]★ (~[N] other reviews echo this pattern)

**Why it matters:** [...]

---

## Watchlist

Patterns that are real but did not make the top 3 — either too low frequency,
ambiguous signal, or worth tracking for change over time. Each entry needs at least
one verbatim quote with attribution; use two quotes when the pattern is bifurcated
or composite.

- **[Pattern name]:** [1–2 sentences describing the pattern and why it's on watchlist rather than top 3.]
  "[verbatim quote]" — [source], [date], [N]★
  (Optional second quote if needed to show the pattern's shape.)

- **[Pattern name]:** [...]
  "[verbatim quote]" — [source], [date], [N]★

- **[Pattern name]:** [...]
  "[verbatim quote]" — [source], [date], [N]★

---

## What's working

Specific, pattern-supported positives only. "Friendly staff" / "good prices" /
"fast service" without a specific practice or person attached do NOT belong here.
Same evidence bar as the actions: verbatim quote, attribution, multi-review pattern.

If nothing meets the bar, this section says so explicitly:
*"No specific, pattern-supported positives identified above the generic-vibes threshold."*

- **[Specific practice / pattern]:** [1 sentence describing the practice and its strength.]
  "[verbatim quote]" — [source], [date], [N]★
  (Optional second quote when one positive needs multiple voices to land.)

- **[Specific practice / pattern]:** [...]
  "[verbatim quote]" — [source], [date], [N]★

---

## Labeler notes

Observations that didn't fit the structure. Hypotheses about what's actually driving
the patterns, signals that conflict with each other, surprises, and any context
worth carrying into the eval comparison later. This is the section where the
labeler's judgment shows through — not the actions, which are structured.

- [Observation, hypothesis, or open question.]
- [...]
- [...]

---

## Themes considered but cut from top 3

Auditability. Every theme surfaced in Stage 1 that didn't make the top 3 gets one
line here with the reason for the cut. Reasons: too rare, lower frequency than
chosen actions, overlaps with a chosen action, signal too ambiguous, or moved to
watchlist (name watchlist destination if so).

- **[Theme name]:** [Reason for cut.]
- **[Theme name]:** [Reason.]
- **[Theme name]:** [Reason.]
