/**
 * UI label helpers. The database uses 'actions' / 'working' as canonical
 * pass identifiers; the UI surface uses friendlier language.
 */

export type Pass = "actions" | "working";

export const passLabel = {
  singular: (p: Pass) => (p === "actions" ? "Action item" : "What's working"),
  plural: (p: Pass) => (p === "actions" ? "Action items" : "What's working"),
};

export function passNoun(p: Pass, polarity: "negative" | "positive" = "negative") {
  // For prevalence sentences: "X% of negative reviews" / "X% of positive reviews"
  return polarity === "negative"
    ? "negative reviews"
    : "positive reviews";
}
