// Wording for the Knowledge view's problem line. Pure (no DOM) so it runs
// under plain `node` (see nav.test.mjs). `fmt` formats an ISO time.
//
// Rule: an outage is stated plainly with both times. Nothing here ever says
// the data is current when the last request failed.

export function describeProblem(error, status, fmt) {
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
    case "not_found":
      return null;
    default:
      // not_configured / token_file_*: the operator's setup, not an outage.
      return `Knowledge module setup problem: ${error.message}`;
  }
}
