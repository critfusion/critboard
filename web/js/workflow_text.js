// Pure helpers for the Workflow view (no DOM, so they run under plain
// `node`, see nav.test.mjs).
//
// Rule: a missing reviewer, an expired reading and a cost figure are each
// shown in words as what they are. Nothing here turns an absent fact into a
// settled one.

export const GATE_LABELS = {
  missing: "REVIEW GATE MISSING",
  named: "REVIEWER AND APPROVER NAMED",
  no_review_marked: "NO REVIEW MARKED",
};

/** Any gate value the server did not define is shown as missing, never fine. */
export function gateKey(gate) {
  return Object.hasOwn(GATE_LABELS, gate) ? gate : "missing";
}

export const gateLabel = (gate) => GATE_LABELS[gateKey(gate)];

export const GATE_MISSING_WORDS = {
  reviewer: "no reviewer named",
  approver: "no final approver named",
  independent_reviewer: "reviewer is the coder",
  independent_approver: "approver is the coder (self-approval)",
  coder_unknown: "coder unknown",
};

export function gateMissingText(missing) {
  return (Array.isArray(missing) ? missing : []).map((m) => GATE_MISSING_WORDS[m] || String(m)).join("; ");
}

export const BASIS_LABELS = {
  "provider-observed": "provider-observed",
  "owner-reported": "owner-reported",
  unknown: "unknown",
};

export const basisLabel = (b) => BASIS_LABELS[Object.hasOwn(BASIS_LABELS, b) ? b : "unknown"];

/** True when the row is past expires_at at `nowMs`, or says it has expired.
 * An observed row with no readable expires_at cannot be trusted: expired. */
export function isExpired(row, nowMs) {
  if (!row || row.expired || row.state === "stale") return true;
  if (row.state === "unknown" || !row.state) return false;
  const at = Date.parse(row.expires_at);
  return Number.isNaN(at) || nowMs >= at;
}

/** The row as it may be shown now. The server's `expired` is a snapshot from
 * fetch time; this re-checks against the browser clock at render time. A row
 * that has expired since drops its numbers and becomes stale. */
export function effectiveRow(row, nowMs) {
  if (!row || !isExpired(row, nowMs)) return row;
  return { ...row, state: "stale", expired: true, values: null };
}

/** Badge text for an availability row. Expired and unknown never read as available. */
export function availabilityBadge(row) {
  if (!row || row.expired || row.state === "stale") return "STALE";
  if (row.state === "unknown" || !row.state) return "UNKNOWN";
  return row.basis === "owner-reported" ? String(row.state).toUpperCase() : "OBSERVED";
}

export const availabilityKey = (row) => {
  const b = availabilityBadge(row);
  return b === "STALE" ? "stale" : b === "UNKNOWN" ? "unknown" : "fresh";
};

/** "5 of 10 usd (month)", or null. Only called for a fresh provider reading. */
export function valuesText(values) {
  if (!values || typeof values !== "object") return null;
  const num = (v) => (typeof v === "number" ? String(v) : null);
  const rem = num(values.remaining);
  const lim = num(values.limit);
  const unit = values.unit ? ` ${values.unit}` : "";
  const per = values.period ? ` (${values.period})` : "";
  if (rem !== null && lim !== null) return `${rem} of ${lim}${unit} remaining${per}`;
  if (rem !== null) return `${rem}${unit} remaining${per}`;
  const used = num(values.used);
  return used !== null ? `${used}${unit} used${per}` : null;
}

/** "estimate: $1.23 today". The word estimate is always part of the text. */
export function costText(session) {
  const c = session && session.cost_estimate_today_usd;
  return typeof c === "number" ? `estimate: $${c.toFixed(2)} today (not spend)` : "no estimate";
}

export const POLICY_STATE_TEXT = {
  not_configured: "Policy source not configured. Enable and configure the Knowledge module to read the policy book.",
  unreachable: "Policy source unreachable. No policy is shown.",
  auth_failed: "Policy source refused the token. No policy is shown.",
  book_not_found: "Policy book not found. No policy is shown.",
};

export function policyStateText(policy) {
  if (!policy || policy.state === "ok") return null;
  return POLICY_STATE_TEXT[policy.state] || `Policy could not be read (${policy.state}). No policy is shown.`;
}
