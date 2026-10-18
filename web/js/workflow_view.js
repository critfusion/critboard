// Workflow view: the written workflow next to what agents are doing, read
// through the server (/api/workflow). Four panels, never merged into one
// verdict: Intended policy / Observed runtime / Assignments / Availability.
// Everything from the server is set as text through el(), never as markup,
// and a link is made only from an http(s) URL.
//
// Nothing is cached as current. A failed refresh keeps the last good copy,
// dimmed, under a line that says when it was fetched.

import { describeProblem, wikiLinkItems } from "./knowledge_text.js";
import { isUnknown, safeHref, statusKey, statusLabel } from "./infra_text.js";
import {
  availabilityBadge, availabilityKey, basisLabel, costText, gateKey, gateLabel, gateMissingText,
  effectiveRow, policyStateText, valuesText,
} from "./workflow_text.js";
import { el, fmtAbsTime, fmtRelTime } from "./utils.js";

const RERENDER_MS = 60000;
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

const unknownSpan = () => el("span", { class: "infra-unknown" }, "unknown");
const textOrUnknown = (v) => (isUnknown(v) ? unknownSpan() : String(v));
const listOrUnknown = (items) => (Array.isArray(items) && items.length ? items.join(", ") : unknownSpan());

function link(text, href) {
  const safe = safeHref(href);
  return safe ? el("a", { href: safe, target: "_blank", rel: "noopener noreferrer" }, text) : text;
}

function wikiLinks(rec) {
  const items = wikiLinkItems(rec);
  return items.length
    ? el("p", { class: "infra-wiki-links" }, items.map((it) => link(it.text, it.href)))
    : null;
}

function row(label, value) {
  return [el("dt", {}, label), el("dd", {}, value)];
}

function panel(id, title, subtitle, children) {
  const h = el("h3", { class: "wf-title", id }, title);
  return el("section", { class: "wf-panel", "aria-labelledby": id }, [
    h, el("p", { class: "wf-sub" }, subtitle), ...children,
  ]);
}

function policyCard(p) {
  const key = statusKey(p.status);
  const src = p.source || {};
  return el("article", { class: `infra-rec infra-st-${key}` }, [
    el("header", { class: "infra-rec-head" }, [
      el("h4", { class: "infra-rec-title" }, link(p.title, p.url)),
      el("span", { class: `infra-badge infra-badge-${key}` }, statusLabel(key)),
    ]),
    wikiLinks(p),
    p.status_reason ? el("p", { class: "infra-reason" }, p.status_reason) : null,
    p.status_conflict ? el("p", { class: "infra-reason infra-conflict" }, p.status_conflict) : null,
    el("dl", { class: "infra-fields" }, [
      ...row("Source repo", isUnknown(src.repo) ? unknownSpan() : link(src.repo, src.repo_href)),
      ...row("Source path", textOrUnknown(src.path)),
      ...row("Source SHA", isUnknown(src.git_sha) ? unknownSpan() : el("code", {}, src.git_sha)),
      ...row("Page edited", isUnknown(p.updated_at) ? unknownSpan() : fmtAbsTime(p.updated_at)),
    ]),
  ].filter(Boolean));
}

function policyPanel(policy) {
  const problem = policyStateText(policy);
  const body = problem
    ? [el("p", { class: "infra-unknown wf-notice", role: "status" }, problem)]
    : policy.pages.length
      ? [el("div", { class: "infra-cards" }, policy.pages.map(policyCard)),
         policy.truncated ? el("p", { class: "infra-unknown" }, `Showing ${policy.pages.length} of ${policy.total} pages.`) : null]
      : [el("p", { class: "infra-unknown" }, "The policy book has no pages. No intended policy is known from here.")];
  return panel("wf-h-policy", "Intended policy", "What the wiki says should happen. Not a record of what happened.", body.filter(Boolean));
}

function sessionCard(s) {
  return el("article", { class: "infra-rec wf-session" }, [
    el("header", { class: "infra-rec-head" }, [
      el("h4", { class: "infra-rec-title" }, `${s.runner === "unknown" ? "unknown runner" : s.runner} session ${s.id}`),
      el("span", { class: "infra-badge infra-badge-unknown" }, String(s.status).toUpperCase()),
    ]),
    el("dl", { class: "infra-fields" }, [
      ...row("Role", textOrUnknown(s.role)),
      ...row("Runner", textOrUnknown(s.runner)),
      ...row("Model", textOrUnknown(s.model)),
      ...row("Provider", textOrUnknown(s.provider)),
      ...row("Account", textOrUnknown(s.account)),
      ...row("Host", textOrUnknown(s.host)),
      ...row("Coordinated", textOrUnknown(s.coordinated)),
      ...row("Bead", s.bead ? s.bead : unknownSpan()),
      ...row("Cost", el("span", { class: "wf-estimate" }, costText(s))),
    ]),
  ]);
}

function observedPanel(observed) {
  const down = Object.entries(observed.sources || {}).filter(([, h]) => !h.ok || h.stale).map(([k]) => k);
  return panel("wf-h-observed", "Observed runtime",
    `What sessions report now (snapshot ${observed.snapshot_at ? fmt(observed.snapshot_at) : "time unknown"}).`, [
      down.length ? el("p", { class: "infra-unknown wf-notice", role: "status" },
        `Collector ${down.join(", ")} is not fresh. Sessions below may be out of date.`) : null,
      observed.sessions.length
        ? el("div", { class: "infra-cards" }, observed.sessions.map(sessionCard))
        : el("p", { class: "infra-unknown" }, "No sessions observed."),
      el("p", { class: "wf-sub" }, observed.cost_note),
    ].filter(Boolean));
}

function assignmentCard(a) {
  const key = gateKey(a.review_gate);
  const why = key === "missing" ? gateMissingText(a.gate_missing) : "";
  return el("article", { class: `infra-rec wf-gate-${key}` }, [
    el("header", { class: "infra-rec-head" }, [
      el("h4", { class: "infra-rec-title" }, `${a.id}: ${a.title}`),
      el("span", { class: `infra-badge wf-gate-badge-${key}` }, gateLabel(key)),
    ]),
    why ? el("p", { class: "infra-reason infra-conflict" }, why) : null,
    el("dl", { class: "infra-fields" }, [
      ...row("Coder", a.coder_basis === "assignee" ? `${a.coder} (from assignee)` : textOrUnknown(a.coder)),
      ...row("Independent reviewer", listOrUnknown(a.reviewers)),
      ...row("Final approver", listOrUnknown(a.approvers)),
      ...row("Reviewed ref", listOrUnknown(a.reviewed_refs)),
      ...row("Sessions", listOrUnknown(a.sessions)),
    ]),
    key === "named" ? el("p", { class: "wf-sub" }, "Names are on the bead. This does not show the review happened.") : null,
  ].filter(Boolean));
}

function assignmentsPanel(block) {
  const missing = block.items.filter((a) => gateKey(a.review_gate) === "missing").length;
  return panel("wf-h-assign", "Assignments", "In-progress beads and the review each one names.", [
    missing ? el("p", { class: "wf-gate-alert", role: "status" }, `${missing} bead${missing === 1 ? "" : "s"} with a missing review gate`) : null,
    block.items.length
      ? el("div", { class: "infra-cards" }, block.items.map(assignmentCard))
      : el("p", { class: "infra-unknown" }, "No in-progress beads."),
  ].filter(Boolean));
}

function availabilityCard(raw, nowMs) {
  const r = effectiveRow(raw, nowMs);
  const k = availabilityKey(r);
  const text = valuesText(r.values);
  return el("article", { class: `infra-rec wf-av-${k}` }, [
    el("header", { class: "infra-rec-head" }, [
      el("h4", { class: "infra-rec-title" }, r.provider),
      el("span", { class: `infra-badge wf-av-badge-${k}` }, availabilityBadge(r)),
    ]),
    el("dl", { class: "infra-fields" }, [
      ...row("Basis", basisLabel(r.basis)),
      ...row("Observed", r.observed_at ? fmt(r.observed_at) : unknownSpan()),
      ...row("Expires", r.expires_at ? (r.expired ? `expired ${fmt(r.expires_at)}` : fmt(r.expires_at)) : unknownSpan()),
      ...row("Reading", text || (r.expired ? el("span", { class: "infra-unknown" }, "hidden: reading expired") : unknownSpan())),
      r.detail ? row("Detail", r.detail) : [],
    ].flat()),
  ]);
}

function availabilityPanel(av, nowMs) {
  return panel("wf-h-av", "Availability", `Provider readings expire after ${Math.round(av.ttl_s / 60)} minutes. An expired reading is stale.`, [
    av.rows.length
      ? el("div", { class: "infra-cards" }, av.rows.map((r) => availabilityCard(r, nowMs)))
      : el("p", { class: "infra-unknown" }, "No availability readings."),
  ]);
}

export function mountWorkflow(root) {
  const refresh = el("button", { type: "button", class: "infra-refresh" }, "Refresh");
  const status = el("div", { class: "kb-status", role: "status" });
  const bar = el("div", { class: "infra-bar" }, [status, refresh]);
  const panels = el("div", { class: "wf-panels" });
  root.append(bar, panels);

  let lastGood = null;

  // Expiry is judged against the browser clock at every render, so a reading
  // that expires while the page sits open turns stale without a refetch.
  function render(body) {
    panels.replaceChildren(
      policyPanel(body.policy), observedPanel(body.observed),
      assignmentsPanel(body.assignments), availabilityPanel(body.availability, Date.now()));
  }

  function showOk(body) {
    lastGood = body;
    panels.classList.remove("infra-outdated");
    render(body);
    status.className = "kb-status";
    status.textContent = `Fetched ${fmtAbsTime(body.fetched_at)}.`;
  }

  function showFailure(body) {
    status.className = "kb-status kb-down";
    const problem = body.error && body.error.code === "local"
      ? body.error.message
      : describeProblem(body.error, body.status, fmt, location.hostname, "Workflow") || "Workflow data could not be read";
    if (lastGood) {
      render(lastGood); // judged against the clock now: expired numbers do not come back
      panels.classList.add("infra-outdated");
      status.textContent = `${problem}. Showing the copy fetched ${fmt(lastGood.fetched_at)}; it may be out of date.`;
    } else {
      panels.replaceChildren(el("p", { class: "infra-unknown" }, "No workflow data has been observed since CritBoard loaded this page."));
      status.textContent = problem;
    }
  }

  async function load() {
    if (lastGood) render(lastGood); // a copy shown again may have expired while hidden
    refresh.disabled = true;
    status.className = "kb-status";
    status.textContent = "Loading...";
    const body = await api("/api/workflow");
    refresh.disabled = false;
    if (body.ok) showOk(body);
    else showFailure(body);
  }

  refresh.addEventListener("click", load);
  // Re-render every minute while the view is shown, the dimmed last-good copy included.
  setInterval(() => { if (lastGood && !root.hidden) render(lastGood); }, RERENDER_MS);
  return { onShow: load };
}
