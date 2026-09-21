import { el, fmtRelTime } from "../utils.js";

// Click behaviour: clicking a pill opens a small popover with the full
// detail + remedy text (not truncated into a tooltip) -- the "click"
// counterpart to the title attribute's hover tooltip below. It is appended
// to document.body and positioned with fixed coordinates, deliberately NOT
// inline inside the panel: this panel's height is a fixed layout value
// (config/layout.json h:1, ~48px of body), so any inline expansion would
// just be clipped/require scrolling inside a tiny box -- effectively
// invisible. One popover instance is shared across all pills in this panel.
let activePopover = null;
let activePillKey = null;

function detailBody(key, s) {
  const rows = [el("div", { class: "source-health-detail-key mono" }, key)];
  if (s.ok === false) {
    rows.push(
      el("div", { class: "source-health-detail-line" }, s.detail || s.error || "No further detail is available for this failure.")
    );
    if (s.remedy) rows.push(el("div", { class: "source-health-detail-remedy" }, s.remedy));
  } else {
    rows.push(
      el(
        "div",
        { class: "source-health-detail-line faint mono" },
        `ok · last run ${fmtRelTime(s.last_run)} · ${s.duration_ms ?? "?"}ms${s.stale ? " · stale" : ""}`
      )
    );
  }
  return rows;
}

function closePopover() {
  if (activePopover) activePopover.remove();
  activePopover = null;
  activePillKey = null;
  document.removeEventListener("click", onDocClick, true);
  document.removeEventListener("keydown", onDocKey, true);
  window.removeEventListener("resize", closePopover);
}

function onDocClick(e) {
  if (activePopover && !activePopover.contains(e.target) && !e.target.closest(".source-health-pill")) {
    closePopover();
  }
}

function onDocKey(e) {
  if (e.key === "Escape") closePopover();
}

function openPopover(pillEl, key, s) {
  closePopover();
  const pop = el("div", { class: "source-health-popover", role: "dialog", "aria-label": `${key} source health detail` }, detailBody(key, s));
  document.body.appendChild(pop);

  const rect = pillEl.getBoundingClientRect();
  const popRect = pop.getBoundingClientRect();
  let left = rect.left;
  if (left + popRect.width > window.innerWidth - 8) left = window.innerWidth - popRect.width - 8;
  left = Math.max(8, left);
  let top = rect.bottom + 6;
  if (top + popRect.height > window.innerHeight - 8) top = rect.top - popRect.height - 6;
  top = Math.max(8, top);
  pop.style.left = `${left}px`;
  pop.style.top = `${top}px`;

  activePopover = pop;
  activePillKey = key;
  // Deferred so the click that opened this popover doesn't immediately
  // trigger onDocClick and close it again.
  setTimeout(() => {
    document.addEventListener("click", onDocClick, true);
    document.addEventListener("keydown", onDocKey, true);
    window.addEventListener("resize", closePopover);
  }, 0);
}

export default {
  title: "Source health",
  minW: 3,
  minH: 1,
  render(container, { data }) {
    closePopover();
    const sources = data?.sources || {};
    const keys = Object.keys(sources);

    if (keys.length === 0) {
      container.appendChild(el("div", { class: "empty-state" }, "No collectors reporting."));
      return;
    }

    const wrap = el("div", { style: "display:flex; flex-wrap:wrap; gap:6px; align-items:flex-start;" });

    for (const key of keys) {
      const s = sources[key] || {};
      // Tone: pill-crit only for a GENUINE failure (something configured
      // and broken); an unconfigured optional dependency is pill-info, the
      // same calm color used elsewhere for "not a problem, just inactive".
      let cls = "pill-ok";
      if (s.ok === false) cls = s.optional ? "pill-info" : "pill-crit";
      else if (s.stale) cls = "pill-warn";

      const title = s.ok === false
        ? `${key}: ${s.detail || s.error || "unknown error"}${s.remedy ? ` — ${s.remedy}` : ""}`
        : `${key}: ok, last run ${fmtRelTime(s.last_run)}, ${s.duration_ms ?? "?"}ms`;

      const pill = el(
        "span",
        {
          class: `pill ${cls} source-health-pill`,
          title,
          role: "button",
          tabindex: "0",
          "aria-expanded": "false",
          "aria-label": `${key} source health, click for detail`,
        },
        [el("span", { class: "pill-dot" }), el("span", {}, key)]
      );
      const toggle = () => {
        if (activePillKey === key) {
          closePopover();
        } else {
          openPopover(pill, key, s);
          pill.setAttribute("aria-expanded", "true");
        }
      };
      pill.addEventListener("click", (e) => {
        e.stopPropagation();
        toggle();
      });
      pill.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          toggle();
        }
      });
      wrap.appendChild(pill);
    }
    container.appendChild(wrap);
  },
};
