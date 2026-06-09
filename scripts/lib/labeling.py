"""Two-stage Haiku theme labeling — shared by per-location and network passes.

Stage 1: read cluster representatives, return up to 3 candidate distinct
practices, each with a verbatim evidence quote.
Stage 2: pick one candidate per cluster that's distinct from themes already
picked in the same pass (sequential awareness), or report no distinct pick.

Caller is responsible for cluster ordering (size-desc within a pass) and for
maintaining the previously-picked list across calls.
"""

from __future__ import annotations

import json
import logging
import time

from .config import LABEL_MODEL

log = logging.getLogger(__name__)

# How many representatives per cluster to send to Stage 1.
REP_PER_CLUSTER = 5


# ---------------------------------------------------------------------------
# System prompts (cached across calls within a stage)
# ---------------------------------------------------------------------------

STAGE1_SYSTEM_PROMPT = """You are a theme analyst for tire-shop customer reviews.

Your job: read 3-5 representative reviews from a cluster and identify the
CONCRETE PRACTICES visible in them. Return UP TO 3 DISTINCT candidate practices.

A "concrete practice" requires all three:
  (1) A named subject — a specific employee, a named role, or a specific service.
  (2) A concrete action — what was done, how it was delivered, the specific behavior.
  (3) A verbatim phrase from the reviews that proves both (1) and (2).

"Distinct" means a DIFFERENT practice, not the same practice phrased differently.

Examples of TWO DISTINCT practices in the same cluster:
  - "Tech walks customer into bay to show wear" AND "Manager pressures
    customer to authorize add-ons" — different actors, different actions.
  - "Appointment ignored on arrival" AND "Wait time exceeds quoted estimate"
    — different breakdowns.
  - "Customer's rims damaged during service" AND "Repair upsold from $30
    patch to $170 tire" — same cluster might contain both.

Examples that are NOT distinct (collapse to ONE candidate, the strongest):
  - "Staff is rude" + "Staff has bad attitude" — same practice.
  - "Manager dismissed me" + "Manager spoke over me" — same practice.

NOT a candidate at all (return nothing for these):
  - "Friendly staff", "Great service", "Fast" — generic sentiment, no concrete behavior.
  - "Honest" without a specific situation.
  - "Highly recommend" — no concrete practice.

THE CRITICAL RULE
Each candidate's evidence_quote is load-bearing. If you cannot pull a verbatim
phrase from the supplied reviews that contains BOTH the named subject AND the
concrete action for a candidate, do NOT include that candidate. Do not
paraphrase. Do not invent. Do not compose across reviews.

OUTPUT SCHEMA (JSON only, no prose around it)
{
  "candidates": [
    { "label": "<5-10 word specific label>",
      "evidence_quote": "<exact verbatim phrase from one review>" },
    ...
  ]
}

Return `{"candidates": []}` if the reviews are generic sentiment with no
concrete practices that meet the bar.
Return 1 candidate if only one distinct practice is supported.
Return 2-3 candidates only if MULTIPLE genuinely-distinct practices are
present, each with its own verbatim quote."""


STAGE2_SYSTEM_PROMPT = """You are selecting one theme per cluster, ensuring
distinctness across all clusters in this pass.

You receive:
  - The candidate themes for the current cluster (up to 3 options, 0-indexed).
  - The themes already selected for prior clusters in this pass.

Your job: pick the candidate that is most DISTINCT from already-selected
themes, OR report that no distinct candidate exists.

"Distinct" means a different practice (different subject, different action,
different situation) — not different phrasing for the same practice. If a
candidate describes the same practice as an already-selected theme, it is
NOT distinct.

When every candidate overlaps with a previously-selected theme, OR when no
candidates were supplied, return picked_index=null. False specificity is
worse than honest overlap.

OUTPUT SCHEMA (JSON only)
When a distinct candidate exists:
  { "picked_index": <int — 0-indexed>, "rationale": "<one sentence>" }
Otherwise:
  { "picked_index": null, "rationale": "<which existing theme overlaps, or 'no candidates'>" }"""


# ---------------------------------------------------------------------------
# Haiku call helpers
# ---------------------------------------------------------------------------

def format_review_for_prompt(review: dict, idx: int) -> str:
    date = (review.get("posted_at") or "----")[:10]
    rating = review.get("rating") or "?"
    source = review.get("source") or "?"
    text = (review.get("text") or "").strip()
    return f"[{idx}] {source}, {rating}★, {date}\n{text}"


def parse_haiku_json(raw: str) -> dict:
    """Extract the first JSON object from Haiku's response.

    Tolerant of leading/trailing whitespace, markdown code fences, leading
    prose before the JSON object, and trailing prose after it. The prompt
    asks for JSON-only, but if Haiku slips in an explanatory paragraph (it
    sometimes does on generic clusters), we parse cleanly instead of dying.

    Raises ValueError or JSONDecodeError on a genuine parse failure.
    """
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        raw = "\n".join(lines).strip()

    start = raw.find("{")
    if start == -1:
        raise ValueError(f"no JSON object found in response: {raw[:200]}")

    obj, _end = json.JSONDecoder().raw_decode(raw[start:])
    if not isinstance(obj, dict):
        raise ValueError(f"expected JSON object, got {type(obj).__name__}")
    return obj


def haiku_json_call(client, system_prompt: str, user_msg: str, max_tokens: int) -> dict | None:
    """One Haiku call with prompt caching, temperature=0, retry once, JSON-parsed.

    Returns the parsed dict on success, or None after one failed retry.
    """
    for attempt in (1, 2):
        try:
            res = client.messages.create(
                model=LABEL_MODEL,
                max_tokens=max_tokens,
                temperature=0,
                system=[
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_msg}],
            )
            return parse_haiku_json(res.content[0].text)
        except Exception as e:
            if attempt == 1:
                log.warning("haiku call failed (attempt 1), retrying: %s", e)
                time.sleep(1)
                continue
            log.error("haiku call failed after retries: %s", e)
            return None


# ---------------------------------------------------------------------------
# Stage 1 + Stage 2 — the actual labeling primitives
# ---------------------------------------------------------------------------

def stage1_candidates(
    client,
    pass_name: str,
    member_count: int,
    avg_rating: float,
    representatives: list[dict],
) -> list[dict] | None:
    """Stage 1: 0-3 candidate themes for a cluster. None on Haiku failure.

    Each candidate is a dict with 'label' and 'evidence_quote'. Both must
    be non-empty strings; malformed entries are filtered out.
    """
    user_msg = (
        f"Cluster context: pass={pass_name}, n={member_count}, "
        f"avg_rating={avg_rating:.2f}.\n\n"
        f"Representative reviews ({len(representatives)} nearest to centroid):\n\n"
        + "\n\n".join(
            format_review_for_prompt(r, i + 1) for i, r in enumerate(representatives)
        )
        + "\n\nReturn your JSON candidate list."
    )
    parsed = haiku_json_call(client, STAGE1_SYSTEM_PROMPT, user_msg, max_tokens=1000)
    if parsed is None:
        return None
    if "candidates" not in parsed:
        log.error("stage1 response missing 'candidates' key: %s", parsed)
        return []
    cleaned: list[dict] = []
    for c in parsed["candidates"]:
        if not isinstance(c, dict):
            continue
        label = c.get("label")
        quote = c.get("evidence_quote")
        if label and quote and isinstance(label, str) and isinstance(quote, str):
            cleaned.append({"label": label.strip(), "evidence_quote": quote.strip()})
    return cleaned


def stage2_select(
    client,
    candidates: list[dict],
    previously_picked: list[str],
) -> dict:
    """Stage 2: pick one candidate or none. Always returns a dict (never None).

    On Haiku failure or empty input, returns picked_index=null with a reason
    rather than raising.
    """
    if not candidates:
        return {"picked_index": None, "rationale": "no candidates from Stage 1"}

    previously_str = (
        "Themes already selected in this pass:\n"
        + "\n".join(f"  - \"{t}\"" for t in previously_picked)
        if previously_picked
        else "Themes already selected in this pass: (none yet)"
    )
    candidates_str = "Candidates for the current cluster:\n" + "\n".join(
        f"  [{i}] {c['label']}\n      quote: \"{c['evidence_quote']}\""
        for i, c in enumerate(candidates)
    )
    user_msg = previously_str + "\n\n" + candidates_str + "\n\nReturn your JSON pick."

    parsed = haiku_json_call(client, STAGE2_SYSTEM_PROMPT, user_msg, max_tokens=300)
    if parsed is None:
        return {"picked_index": None, "rationale": "stage2 haiku call failed"}
    if "picked_index" not in parsed:
        log.error("stage2 response missing 'picked_index' key: %s", parsed)
        return {"picked_index": None, "rationale": "malformed stage2 response"}
    picked = parsed.get("picked_index")
    if picked is not None and not (isinstance(picked, int) and 0 <= picked < len(candidates)):
        log.warning(
            "stage2 returned out-of-range picked_index=%s for %d candidates",
            picked, len(candidates),
        )
        return {"picked_index": None, "rationale": f"invalid picked_index {picked}"}
    return {
        "picked_index": picked,
        "rationale": parsed.get("rationale", ""),
    }
