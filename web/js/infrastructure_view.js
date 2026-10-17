// Infrastructure view: read-only records curated in a BookStack book, read
// through the server (/api/infrastructure). The browser never sees the API
// token. Everything from the server is set with textContent, never innerHTML,
// and a link is made only from an http(s) URL.
//
// Nothing is cached here as current. If a refresh fails, the records from the
// last good fetch stay on screen, dimmed, under a line that says when they
// were fetched and since when BookStack has been failing.

import { describeProblem } from "./knowledge_text.js";
import {
  groupByKind, isUnknown, safeHref, statusCounts, statusKey, statusLabel, STATUS_LABELS, verifiedText,
} from "./infra_text.js";
import { el, fmtAbsTime, fmtRelTime } from "./utils.js";

const fmt = (iso) => `${fmtAbsTime(iso)} (${fmtRelTime(iso)})`;

async function api(path) {
  try {
    const resp = await fetch(path, { cache: "no-store" });
    const body = await resp.json().catch(() => null);
    if (body && typeof body === "object" && "ok" in body) return body;
    return { ok: false, error: { code: "upstream_error", message: "unexpected response", http_status: resp.status }, status: null };
  } catch (e) {
    return { ok: false, error: { code: "local", message: "CritBoard server not reachable" }, status: null };
  }
}

function problemText(body) {
  if (body.error && body.error.code === "local") return body.error.message;
  return describeProblem(body.error, body.status, fmt, location.hostname, "Infrastructure");
}

const unknownSpan = () => el("span", { class: "infra-unknown" }, "unknown");

function textOrUnknown(value) {
  return isUnknown(value) ? unknownSpan() : String(value);
}

function link(text, href) {
  const safe = safeHref(href);
  return safe
    ? el("a", { href: safe, target: "_blank", rel: "noopener noreferrer" }, text)
    : text;
}

function linkList(items) {
  if (!items || !items.length) return unknownSpan();
  return el("ul", { class: "infra-list" }, items.map((it) => el("li", {}, link(it.text, it.href))));
}

function textList(items) {
  if (!items || !items.length) return unknownSpan();
  return el("ul", { class: "infra-list" }, items.map((t) => el("li", {}, t)));
}

function sourceCell(src) {
  if (!src || [src.repo, src.path, src.git_sha].every(isUnknown)) {
    return el("span", { class: "infra-unknown" }, "unknown (no Provenance table)");
  }
  return el("span", {}, [
    isUnknown(src.repo) ? unknownSpan() : link(src.repo, src.repo_href),
    " : ", textOrUnknown(src.path),
    " @ ", isUnknown(src.git_sha) ? unknownSpan() : el("code", {}, src.git_sha),
    isUnknown(src.class) ? null : ` (${src.class})`,
  ]);
}

function row(label, value) {
  return [el("dt", {}, label), el("dd", {}, value)];
}

function card(r) {
  const key = statusKey(r.status);
  const title = link(r.title, r.url);
  const badge = el("span", { class: `infra-badge infra-badge-${key}` }, statusLabel(key));
  const live = r.verified_basis === "live_check";
  return el("article", { class: `infra-rec infra-st-${key}` }, [
    el("header", { class: "infra-rec-head" }, [el("h4", { class: "infra-rec-title" }, title), badge]),
    r.status_reason ? el("p", { class: "infra-reason" }, r.status_reason) : null,
    r.status_conflict ? el("p", { class: "infra-reason infra-conflict" }, r.status_conflict) : null,
    el("dl", { class: "infra-fields" }, [
      ...row("Purpose", textOrUnknown(r.purpose)),
      ...row("Owner", textOrUnknown(r.owner)),
      ...row("Environment", textOrUnknown(r.environment)),
      ...row("Verified", el("span", { class: live ? "" : "infra-unknown" }, verifiedText(r, fmt))),
      ...row("Source", sourceCell(r.source)),
      ...row("Runbooks", linkList(r.runbooks)),
      ...row("Backup / recovery", linkList(r.backups)),
      ...row("Related", textList(r.related)),
      ...row("Page edited", isUnknown(r.updated_at) ? unknownSpan() : `${fmtAbsTime(r.updated_at)} by ${isUnknown(r.updated_by) ? "unknown" : r.updated_by}`),
    ]),
  ].filter(Boolean));
}

function summary(records, truncated, total) {
  const counts = statusCounts(records);
  const chips = Object.keys(STATUS_LABELS).filter((k) => counts[k]).map((k) =>
    el("span", { class: `infra-badge infra-badge-${k}` }, `${counts[k]} ${STATUS_LABELS[k]}`));
  return el("div", { class: "infra-summary" }, [
    ...chips,
    truncated ? el("span", { class: "infra-unknown" }, `Showing ${records.length} of ${total} pages. Raise the page limit in the wiki book or split it.`) : null,
  ].filter(Boolean));
}

export function mountInfrastructure(root) {
  const refresh = el("button", { type: "button", class: "infra-refresh" }, "Refresh");
  const status = el("div", { class: "kb-status", role: "status" });
  const bar = el("div", { class: "infra-bar" }, [status, refresh]);
  const sum = el("div", {});
  const groups = el("div", { class: "infra-groups" });
  root.append(bar, sum, groups);

  let lastGood = null; // body of the last successful fetch

  function render(body) {
    groups.replaceChildren(...groupByKind(body.records).map(({ kind, records }, i) => {
      const id = `infra-kind-${i}`;
      const h = el("h3", { class: "infra-kind", id }, `${kind} (${records.length})`);
      const sec = el("section", { class: "infra-group", "aria-labelledby": id }, [h, el("div", { class: "infra-cards" }, records.map(card))]);
      return sec;
    }));
    if (!body.records.length) {
      groups.replaceChildren(el("p", { class: "infra-unknown" },
        `Book "${(body.book && body.book.name) || ""}" has no pages. Nothing is known about the infrastructure from here.`));
    }
    sum.replaceChildren(summary(body.records, body.truncated, body.total));
  }

  function showOk(body) {
    lastGood = body;
    groups.classList.remove("infra-outdated");
    render(body);
    status.className = "kb-status";
    status.textContent = `BookStack reachable. Fetched ${fmtAbsTime(body.fetched_at)}. Book: ${(body.book && body.book.name) || "--"}. Stale after ${body.stale_after_days} days.`;
  }

  function showFailure(body) {
    status.className = "kb-status kb-down";
    const problem = problemText(body) || "Infrastructure data could not be read";
    if (lastGood) {
      groups.classList.add("infra-outdated");
      status.textContent = `${problem}. Showing the copy fetched ${fmt(lastGood.fetched_at)}; it may be out of date.`;
    } else {
      groups.classList.remove("infra-outdated");
      groups.replaceChildren(el("p", { class: "infra-unknown" }, "No infrastructure data has been observed since CritBoard loaded this page."));
      sum.replaceChildren();
      status.textContent = problem;
    }
  }

  async function load() {
    refresh.disabled = true;
    status.className = "kb-status";
    status.textContent = "Loading...";
    const body = await api("/api/infrastructure");
    refresh.disabled = false;
    if (body.ok) showOk(body);
    else showFailure(body);
  }

  refresh.addEventListener("click", load);
  return { onShow: load };
}
