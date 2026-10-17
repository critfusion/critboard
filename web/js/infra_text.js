// Pure helpers for the Infrastructure view (no DOM, so they run under plain
// `node`, see nav.test.mjs).
//
// Rule: a fact the wiki does not back is shown as unknown, stale or
// needs-verification, in words, never by colour alone. Nothing here turns a
// missing date into a current one.

export const STATUS_LABELS = {
  current: "CURRENT",
  "needs-verification": "NEEDS VERIFICATION",
  contradicted: "CONTRADICTED",
  stale: "STALE",
  unknown: "UNKNOWN",
};

/** Any value the server did not vouch for is UNKNOWN. */
export function statusKey(status) {
  return Object.hasOwn(STATUS_LABELS, status) ? status : "unknown";
}

export function statusLabel(status) {
  return STATUS_LABELS[statusKey(status)];
}

/** Only http(s) URLs become links. Everything else is plain text. */
export function safeHref(href) {
  return typeof href === "string" && /^https?:\/\/[^\s]+$/i.test(href) ? href : null;
}

export const isUnknown = (v) => v === undefined || v === null || v === "" || v === "unknown";

/** Records grouped by kind, groups sorted by name with "unknown" last. */
export function groupByKind(records) {
  const groups = new Map();
  for (const r of Array.isArray(records) ? records : []) {
    const kind = isUnknown(r && r.kind) ? "unknown" : String(r.kind);
    if (!groups.has(kind)) groups.set(kind, []);
    groups.get(kind).push(r);
  }
  return [...groups.entries()]
    .sort(([a], [b]) => (a === "unknown") - (b === "unknown") || a.localeCompare(b))
    .map(([kind, items]) => ({ kind, records: items }));
}

/** Counts per status key, every key present. */
export function statusCounts(records) {
  const counts = Object.fromEntries(Object.keys(STATUS_LABELS).map((k) => [k, 0]));
  for (const r of Array.isArray(records) ? records : []) counts[statusKey(r && r.status)] += 1;
  return counts;
}

/** The "last verified" line. An edit date is never worded as a verification.
 * `fmt` formats an ISO time. */
export function verifiedText(record, fmt) {
  if (record.verified_basis === "live_check" && !isUnknown(record.last_verified)) {
    return `Live check ${fmt(record.last_verified)}`;
  }
  if (record.verified_basis === "page_updated" && !isUnknown(record.age_from)) {
    return `Never live-checked. Page last edited ${fmt(record.age_from)}`;
  }
  return "Never live-checked. No edit date either";
}
