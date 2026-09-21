// Manually-triggered AI-provider quota check. Hard rule (see AGENTS.md
// briefing): NEVER polled, never on page load, never on a timer -- the only
// thing that makes a live provider call is a click on the Refresh button
// (POST /api/quota/refresh). Mounting/re-rendering this widget only ever
// GETs the last cached result, same as usage_history.js's own-fetch pattern.
import { el, fmtRelTime, fmtCost, fmtTokens, fmtInt, clamp } from "../utils.js";

const PROVIDER_LABELS = {
  openrouter: "OpenRouter",
  opencode_zen: "OpenCode Zen",
  google: "Google / Gemini",
  claude: "Claude (5h block)",
  kimi: "Kimi Code",
  xai: "xAI / Grok",
  openai: "OpenAI / Codex",
};

function fmtByUnit(value, unit) {
  if (value === null || value === undefined || Number.isNaN(value)) return "--";
  switch (unit) {
    case "usd":
      return fmtCost(value);
    case "tokens":
      return fmtTokens(value);
    case "requests":
      return fmtInt(value) + " req";
    case "percent":
      return value.toFixed(1) + "%";
    case "minutes": {
      const h = Math.floor(value / 60);
      const m = Math.round(value % 60);
      return h > 0 ? `${h}h ${m}m` : `${m}m`;
    }
    default:
      return String(value);
  }
}

function statusPill(entry) {
  if (entry.ok === null || entry.ok === undefined) {
    return el("span", { class: "pill pill-idle" }, [el("span", { class: "pill-dot" }), "never checked"]);
  }
  if (entry.ok === true) {
    return el("span", { class: "pill pill-ok" }, [el("span", { class: "pill-dot" }), "ok"]);
  }
  return el("span", { class: "pill pill-crit" }, [el("span", { class: "pill-dot" }), "unavailable"]);
}

function progressColor(pct) {
  if (pct >= 0.9) return "var(--color-status-crit)";
  if (pct >= 0.7) return "var(--color-status-warn)";
  return "var(--color-status-ok)";
}

function renderRow(entry) {
  const provider = entry.provider || "?";
  const label = PROVIDER_LABELS[provider] || provider;
  const isLocal = entry.source === "local_derived";

  const row = el("div", { class: "quota-row" + (isLocal ? " quota-row-local" : "") });

  const headChildren = [el("span", { class: "quota-row-label mono" }, label)];
  if (isLocal) {
    headChildren.push(
      el(
        "span",
        {
          class: "pill quota-local-pill",
          title: "derived locally from usage history -- not reported by the provider",
        },
        "LOCAL ESTIMATE"
      )
    );
  }
  headChildren.push(statusPill(entry));
  row.appendChild(el("div", { class: "quota-row-head" }, headChildren));

  if (entry.ok === false) {
    row.appendChild(
      el("div", { class: "faint", style: "font-size:10px; margin-top:3px; line-height:1.4;" }, entry.error || "unavailable")
    );
  } else if (entry.ok === true) {
    const hasLimit = typeof entry.limit === "number" && entry.limit > 0;
    const info = el("div", { class: "mono tabular-nums quota-row-info" });
    if (hasLimit) {
      info.appendChild(el("span", {}, `${fmtByUnit(entry.remaining, entry.unit)} remaining of ${fmtByUnit(entry.limit, entry.unit)}`));
    } else if (entry.used !== null && entry.used !== undefined) {
      info.appendChild(el("span", {}, `${fmtByUnit(entry.used, entry.unit)} used (no cap set)`));
    } else {
      info.appendChild(el("span", { class: "faint" }, "no usage figures reported"));
    }
    if (entry.period) info.appendChild(el("span", { class: "faint" }, String(entry.period)));
    row.appendChild(info);

    if (hasLimit) {
      const used = typeof entry.used === "number" ? entry.used : entry.limit - (entry.remaining || 0);
      const pct = clamp(used / entry.limit, 0, 1);
      const track = el("div", { class: "quota-progress-track" });
      track.appendChild(
        el("div", {
          class: "quota-progress-fill",
          style: `width:${(pct * 100).toFixed(1)}%; background:${progressColor(pct)};`,
        })
      );
      row.appendChild(track);
    }
  }

  row.appendChild(
    el(
      "div",
      { class: "faint mono", style: "font-size:9px; margin-top:3px;" },
      entry.checked_at ? `checked ${fmtRelTime(entry.checked_at)}` : "never checked"
    )
  );

  return row;
}

function renderRows(host, providers) {
  host.innerHTML = "";
  if (!providers || providers.length === 0) {
    host.appendChild(el("div", { class: "empty-state" }, "No provider data."));
    return;
  }
  for (const entry of providers) host.appendChild(renderRow(entry));
}

function mount(container) {
  container.innerHTML = "";

  const status = el("span", { class: "faint mono", style: "font-size:9px;" }, "");
  const btn = el("button", { type: "button", class: "show-all-btn quota-refresh-btn" }, "Refresh");
  const toolbar = el("div", { class: "quota-toolbar" }, [
    el("span", { class: "faint mono", style: "font-size:9px;" }, "manual check only -- never polled"),
    el("div", { style: "display:flex; align-items:center; gap:8px;" }, [status, btn]),
  ]);

  const rowsHost = el("div", { class: "quota-rows" });
  const wrap = el("div", { class: "quota-widget" }, [toolbar, rowsHost]);
  container.appendChild(wrap);

  rowsHost.appendChild(el("div", { class: "empty-state" }, "Loading…"));

  let cooldownTimer = null;

  function clearCooldown() {
    if (cooldownTimer) {
      clearInterval(cooldownTimer);
      cooldownTimer = null;
    }
  }

  function setBtn(disabled, label) {
    btn.disabled = disabled;
    btn.textContent = label;
  }

  async function load() {
    try {
      const res = await fetch("/api/quota", { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      if (!rowsHost.isConnected) return;
      renderRows(rowsHost, json.providers);
    } catch (err) {
      if (!rowsHost.isConnected) return;
      rowsHost.innerHTML = "";
      rowsHost.appendChild(
        el("div", { class: "empty-state", style: "color:var(--color-status-crit);" }, [
          el("div", {}, "Could not load quota data."),
          el("div", { class: "faint", style: "margin-top:4px; font-size:10px;" }, String((err && err.message) || err)),
        ])
      );
    }
  }

  function startCooldown(seconds) {
    clearCooldown();
    let remaining = Math.max(1, Math.round(seconds));
    setBtn(true, "Refresh");
    status.textContent = `rate-limited, retry in ${remaining}s`;
    cooldownTimer = setInterval(() => {
      remaining -= 1;
      if (remaining <= 0) {
        clearCooldown();
        status.textContent = "";
        setBtn(false, "Refresh");
      } else {
        status.textContent = `rate-limited, retry in ${remaining}s`;
      }
    }, 1000);
  }

  btn.addEventListener("click", async () => {
    if (btn.disabled) return;
    clearCooldown();
    setBtn(true, "Refreshing…");
    status.textContent = "";
    try {
      const res = await fetch("/api/quota/refresh", { method: "POST" });
      if (res.status === 429) {
        const retryAfter = Number(res.headers.get("Retry-After") || "30");
        startCooldown(retryAfter);
        return;
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      if (rowsHost.isConnected) renderRows(rowsHost, json.providers);
      setBtn(false, "Refresh");
    } catch (err) {
      status.textContent = "refresh failed";
      setBtn(false, "Refresh");
    }
  });

  load();
}

export default {
  title: "Provider quota",
  minW: 4,
  minH: 3,
  render(container) {
    mount(container);
  },
};
