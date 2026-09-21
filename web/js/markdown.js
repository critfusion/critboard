// Minimal markdown-to-HTML for bead descriptions/notes. Handles headings,
// ordered/unordered lists, fenced code blocks, inline code, bold/italic, and
// preserves blank lines as paragraph breaks. Not a full CommonMark parser --
// just enough for the free-text fields beads carry. Input is always
// HTML-escaped before any tag is introduced, so this is safe against
// untrusted content.

import { escapeHtml } from "./utils.js";

function inline(s) {
  let out = escapeHtml(s);
  out = out.replace(/`([^`]+)`/g, '<code class="md-code">$1</code>');
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
  return out;
}

export function renderMarkdownLite(text) {
  if (!text) return "";
  const lines = String(text).replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let list = null; // { type: 'ul'|'ol', items: [] }
  let i = 0;

  function flushList() {
    if (!list) return;
    const tag = list.type;
    out.push(`<${tag} class="md-list">` + list.items.map((it) => `<li>${inline(it)}</li>`).join("") + `</${tag}>`);
    list = null;
  }

  while (i < lines.length) {
    const line = lines[i];

    const fence = line.match(/^```(\w*)\s*$/);
    if (fence) {
      flushList();
      const codeLines = [];
      i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) {
        codeLines.push(lines[i]);
        i++;
      }
      i++; // skip closing fence, if any
      out.push(`<pre class="md-pre"><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      flushList();
      const level = heading[1].length;
      out.push(`<h${level} class="md-h">${inline(heading[2])}</h${level}>`);
      i++;
      continue;
    }

    const ul = line.match(/^\s*[-*]\s+(.*)$/);
    const ol = line.match(/^\s*\d+\.\s+(.*)$/);
    if (ul) {
      if (!list || list.type !== "ul") {
        flushList();
        list = { type: "ul", items: [] };
      }
      list.items.push(ul[1]);
      i++;
      continue;
    }
    if (ol) {
      if (!list || list.type !== "ol") {
        flushList();
        list = { type: "ol", items: [] };
      }
      list.items.push(ol[1]);
      i++;
      continue;
    }

    flushList();

    if (line.trim() === "") {
      i++;
      continue;
    }

    const paraLines = [line];
    i++;
    while (
      i < lines.length &&
      lines[i].trim() !== "" &&
      !/^```/.test(lines[i]) &&
      !/^#{1,6}\s+/.test(lines[i]) &&
      !/^\s*[-*]\s+/.test(lines[i]) &&
      !/^\s*\d+\.\s+/.test(lines[i])
    ) {
      paraLines.push(lines[i]);
      i++;
    }
    out.push(`<p class="md-p">${paraLines.map(inline).join("<br>")}</p>`);
  }
  flushList();
  return out.join("\n");
}
