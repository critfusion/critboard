import { el, get, fmtInt, fmtCost, fmtTokens } from "../utils.js";

function autoFormat(path, value) {
  if (typeof value !== "number") return String(value ?? "--");
  if (path.includes("cost_usd")) return fmtCost(value);
  if (path.includes("token") || path.includes("total")) return fmtTokens(value);
  return fmtInt(value);
}

export default {
  title: "Stats",
  minW: 3,
  minH: 1,
  render(container, { data, options }) {
    const stats = Array.isArray(options?.stats) ? options.stats : [];
    if (stats.length === 0) {
      container.appendChild(el("div", { class: "empty-state" }, "No stats configured."));
      return;
    }

    const wrap = el("div", { class: "tile-row", style: "height:100%;" });
    for (const s of stats) {
      const val = get(data, s.path, null);
      const tile = el("div", { style: "display:flex; flex-direction:column; justify-content:center; align-items:center; gap:2px;" });
      tile.appendChild(el("div", { class: "mono tabular-nums", style: "font-size:19px; font-weight:700;" }, val === null ? "--" : autoFormat(s.path, val)));
      tile.appendChild(el("div", { class: "faint mono", style: "font-size:9px; letter-spacing:.05em;" }, s.label || s.path));
      wrap.appendChild(tile);
    }
    container.appendChild(wrap);
  },
};
