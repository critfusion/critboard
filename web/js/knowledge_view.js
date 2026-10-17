// Knowledge view: read-only search and read of BookStack pages through the
// server-side adapter (/api/knowledge/*). The browser never sees the API
// token. Everything from the server is set with textContent, never innerHTML.
//
// The view's DOM is built once and kept while you switch views, so a search
// you typed is still there when you come back.

import { describeProblem } from "./knowledge_text.js";
import { el, fmtAbsTime, fmtRelTime } from "./utils.js";

const fmt = (iso) => `${fmtAbsTime(iso)} (${fmtRelTime(iso)})`;

async function api(path) {
  try {
    const resp = await fetch(path, { cache: "no-store" });
    const body = await resp.json().catch(() => null);
    if (body && typeof body === "object" && "ok" in body) return body;
    return { ok: false, error: { code: "upstream_error", message: "unexpected response", http_status: resp.status }, status: null };
  } catch (e) {
    // The CritBoard server itself did not answer.
    return { ok: false, error: { code: "local", message: "CritBoard server not reachable" }, status: null };
  }
}

function problemText(body) {
  if (body.error && body.error.code === "local") return body.error.message;
  return describeProblem(body.error, body.status, fmt, location.hostname);
}

export function mountKnowledge(root) {
  const input = el("input", {
    type: "search", id: "kb-q", name: "q", maxlength: "200", autocomplete: "off",
    placeholder: "Search pages", "aria-label": "Search knowledge pages",
  });
  const form = el("form", { class: "kb-search", role: "search" }, [
    input, el("button", { type: "submit" }, "Search"),
  ]);
  const status = el("div", { class: "kb-status", role: "status" });
  const results = el("ul", { class: "kb-results", "aria-label": "Search results" });
  const detail = el("article", { class: "kb-detail", "aria-live": "polite" });
  const layout = el("div", { class: "kb-layout" }, [
    el("div", { class: "kb-left" }, [results]), detail,
  ]);
  root.append(form, status, layout);

  let fetchedNote = null; // text of the "fetched at" line when a page is shown

  function setStatus(body) {
    status.className = "kb-status";
    const problem = problemText(body);
    if (problem) {
      status.classList.add("kb-down");
      status.textContent = problem;
      results.classList.add("kb-stale");
      detail.classList.add("kb-stale");
      if (fetchedNote && !fetchedNote.dataset.stale) {
        fetchedNote.dataset.stale = "1";
        fetchedNote.textContent += " BookStack is not answering now; this copy may be out of date.";
      }
    } else if (body.ok) {
      results.classList.remove("kb-stale");
      detail.classList.remove("kb-stale");
      status.textContent = `BookStack reachable. Checked ${fmtAbsTime(body.fetched_at)}.`;
    }
  }

  async function openPage(id) {
    detail.replaceChildren(el("p", { class: "faint" }, "Loading..."));
    fetchedNote = null;
    const body = await api(`/api/knowledge/page/${encodeURIComponent(id)}`);
    if (!body.ok) {
      const msg = body.error && body.error.code === "not_found"
        ? "This page no longer exists in BookStack."
        : problemText(body);
      detail.replaceChildren(el("p", { class: "kb-down" }, msg));
      if (body.error && body.error.code !== "not_found") setStatus(body);
      return;
    }
    const prov = body.provenance || {};
    const tags = (prov.tags || []).length
      ? el("dl", { class: "kb-tags" }, prov.tags.flatMap((t) => [el("dt", {}, t.name), el("dd", {}, t.value || "--")]))
      : el("p", { class: "kb-unknown" }, "No source or provenance tags on this page. Origin unknown.");
    const links = [];
    if (body.edit_url) {
      links.push(el("a", { href: body.edit_url, target: "_blank", rel: "noopener noreferrer", class: "kb-edit" }, "Edit in BookStack"));
    }
    if (body.open_url) {
      links.push(el("a", { href: body.open_url, target: "_blank", rel: "noopener noreferrer" }, "Open in BookStack"));
    }
    fetchedNote = el("p", { class: "kb-fetched faint" }, `Fetched ${fmtAbsTime(body.fetched_at)}.`);
    detail.replaceChildren(...[
      el("h2", { class: "kb-title" }, body.name),
      el("p", { class: "kb-meta" }, `Book: ${(body.book && body.book.name) || "--"} | Last updated: ${fmtAbsTime(body.updated_at)}`),
      el("div", { class: "kb-provenance" }, [el("h3", {}, "Source / provenance"), tags,
        el("p", { class: "kb-meta" }, `Last edited by: ${prov.updated_by || "--"} | Created by: ${prov.created_by || "--"} | Revisions: ${prov.revision_count ?? "--"}`)]),
      el("p", { class: "kb-links" }, links),
      el("div", { class: "kb-text" }, body.text),
      body.truncated ? el("p", { class: "kb-unknown" }, "Page text was cut at the display limit. Open it in BookStack for the rest.") : null,
      fetchedNote,
    ].filter(Boolean));
    setStatus(body);
  }

  async function search(q) {
    results.replaceChildren(el("li", { class: "faint" }, "Searching..."));
    const body = await api(`/api/knowledge/search?q=${encodeURIComponent(q)}`);
    if (!body.ok) {
      results.replaceChildren();
      setStatus(body);
      return;
    }
    setStatus(body);
    if (!body.results.length) {
      results.replaceChildren(el("li", { class: "faint" }, q.trim() ? "No pages found." : "Type a search term."));
      return;
    }
    results.replaceChildren(...body.results.map((r) => {
      const btn = el("button", { type: "button", class: "kb-result" }, [
        el("span", { class: "kb-result-name" }, r.name),
        el("span", { class: "kb-result-book" }, r.book),
        el("span", { class: "kb-result-preview" }, r.preview),
      ]);
      btn.addEventListener("click", () => openPage(r.id));
      return el("li", {}, [btn]);
    }));
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    search(input.value);
  });

  async function refreshStatus() {
    setStatus(await api("/api/knowledge/status"));
  }

  return { onShow: refreshStatus };
}
