import { safeHref } from "./infra_text.js";

// Wording for the Knowledge view's problem line. Pure (no DOM) so it runs
// under plain `node` (see nav.test.mjs). `fmt` formats an ISO time.
//
// Rule: an outage is stated plainly with both times. Nothing here ever says
// the data is current when the last request failed.

export function describeProblem(error, status, fmt, host = "", label = "Knowledge") {
  if (!error) return null;
  const since = status && status.down_since ? fmt(status.down_since) : null;
  const last = status && status.last_success ? fmt(status.last_success) : "never";
  const tail = since ? ` since ${since}; last success ${last}` : `; last success ${last}`;
  switch (error.code) {
    case "unreachable":
      return `BookStack unreachable${tail}`;
    case "auth_failed":
      return `BookStack rejected the configured token (HTTP ${error.http_status})${tail}`;
    case "upstream_error":
      return `BookStack returned an error${error.http_status ? ` (HTTP ${error.http_status})` : ""}${tail}`;
    case "upstream_too_large":
      return `BookStack page is larger than CritBoard's limit${tail}`;
    case "host_not_allowed":
      return `CritBoard refused this address \u2014 add '${host}' to modules.knowledge.allowed_hosts in config/sources.json`;
    case "origin_not_allowed":
      return "CritBoard refused this address: cross-origin request refused";
    case "not_found":
      return null;
    default:
      // not_configured / token_file_*: the operator's setup, not an outage.
      return `${label} module setup problem: ${error.message}`;
  }
}

// The served-revision line for one page. `served` is the server's marker
// ({state, served_at}); `fmtRel` formats an ISO time as "5m ago". The suffix
// is always there: the marker lives in memory and starts empty at restart.
// "Served" means the revision was put in a /api/context answer, nothing more.
export function servedLine(served, fmtRel) {
  const tail = " (since CritBoard restart)";
  if (!served) return null;
  if (served.state === "served" && served.served_at) {
    return `Served to a session start ${fmtRel(served.served_at)}${tail}`;
  }
  return `Pending: not yet served to a session start${tail}`;
}

// Edit and History links of a record, in display order. A missing or
// non-http(s) URL gives no link.
// `name` is the page title. The accessible label starts with the visible
// text (WCAG 2.5.3) and adds ": <name>" only when there is a name.
// `texts` sets the visible words.
export function wikiLinkItems(rec, name = "", texts = {}) {
  const out = [];
  const add = (url, text) => {
    const title = typeof name === "string" ? name.trim() : "";
    if (rec && safeHref(url)) out.push({ text, href: url, label: title ? `${text}: ${title}` : text });
  };
  add(rec && rec.edit_url, texts.edit || "Edit");
  add(rec && rec.history_url, texts.history || "History");
  return out;
}
