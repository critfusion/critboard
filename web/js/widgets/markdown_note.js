import { el, escapeHtml } from "../utils.js";

// Minimal markdown -> HTML. Handles headers, bold/italic, inline code,
// links, and unordered lists -- enough for a pinned note. No external
// markdown library is loaded (not on the allowlist).
function renderMarkdown(src) {
  const lines = escapeHtml(src || "").split("\n");
  const out = [];
  let inList = false;

  const inline = (s) =>
    s
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*]+)\*/g, "<em>$1</em>")
      .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');

  for (const raw of lines) {
    const line = raw.trimEnd();
    const h = /^(#{1,3})\s+(.*)$/.exec(line);
    const li = /^[-*]\s+(.*)$/.exec(line);

    if (li) {
      if (!inList) {
        out.push("<ul>");
        inList = true;
      }
      out.push(`<li>${inline(li[1])}</li>`);
      continue;
    }
    if (inList) {
      out.push("</ul>");
      inList = false;
    }

    if (h) {
      const level = h[1].length + 2; // h3..h5, keep notes visually small
      out.push(`<h${level}>${inline(h[2])}</h${level}>`);
    } else if (line === "") {
      out.push("");
    } else {
      out.push(`<p>${inline(line)}</p>`);
    }
  }
  if (inList) out.push("</ul>");
  return out.join("\n");
}

export default {
  title: "Note",
  minW: 2,
  minH: 1,
  render(container, { options }) {
    const text = options?.text || "";
    if (!text.trim()) {
      container.appendChild(el("div", { class: "empty-state" }, "Empty note. Set options.text in layout.json."));
      return;
    }
    const wrap = el("div", {
      class: "markdown-note",
      html: renderMarkdown(text),
      style: [
        "font-size:11.5px; line-height:1.5;",
      ].join(""),
    });
    container.appendChild(wrap);

    // Scoped, lightweight typography for the rendered markdown -- injected
    // once per render since this widget has no shared stylesheet hook.
    wrap.querySelectorAll("h3,h4,h5").forEach((h) => {
      h.style.margin = "0 0 4px";
      h.style.color = "var(--color-accent-cyan)";
      h.style.fontFamily = "var(--font-mono)";
    });
    wrap.querySelectorAll("p").forEach((p) => (p.style.margin = "0 0 6px"));
    wrap.querySelectorAll("ul").forEach((ul) => {
      ul.style.margin = "0 0 6px";
      ul.style.paddingLeft = "16px";
    });
    wrap.querySelectorAll("code").forEach((c) => {
      c.style.fontFamily = "var(--font-mono)";
      c.style.background = "var(--color-bg-elevated)";
      c.style.padding = "1px 4px";
      c.style.borderRadius = "3px";
      c.style.fontSize = "10.5px";
    });
    wrap.querySelectorAll("a").forEach((a) => (a.style.color = "var(--color-accent-cyan)"));
  },
};
