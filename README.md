# CritBoard -- frontend

Mission-control UI for a fleet of coding agents. Plain ES modules, plain CSS,
no build step, no bundler. Open a file, edit it, hit refresh.

Scope of this directory: `web/`, `config/layout.json`, `config/theme.json`.
The backend (`server/`, `config/sources.json`, `config/pricing.json`,
`deploy/`) is owned separately -- see `SPEC.md` for the full contract and the
`/api/snapshot` schema this UI renders.

## Run it

**Against the live backend** (FastAPI serving `/api/snapshot`, `/api/stream`,
`/api/config/*` on the same origin as the static files):

Open `http://<host>:9999/index.html`. That's it -- `index.html` always talks
to the real API.

**Against the fixture, with no backend at all** (this is how the UI was built
and verified):

```sh
python3 -m http.server 8777 --directory /path/to/dashboard/web
```

Then open one of:

- `http://localhost:8777/dev.html` -- the dev harness. Loads
  `web/fixtures/snapshot.json` once at boot and does not open an SSE
  connection (there's no backend to stream from). Config is read through
  `web/config` -> a symlink to `../config`, so editing `config/layout.json`
  or `config/theme.json` and refreshing shows the change, exactly like
  production.
- `http://localhost:8777/dev.html?fixture=degraded` -- same app, loaded
  against `web/fixtures/snapshot-degraded.json`: every collector reports
  `ok:false`, every list is empty. Confirms the whole grid still renders
  (no blank page, no thrown errors) when the backend is unhealthy.
- `http://localhost:8777/index.html?fixture=1` -- equivalent to `dev.html`,
  for testing the production shell against the fixture without a second
  HTML file. `?fixture=degraded` works here too.

Fixture mode is on when `window.__DASHBOARD_FIXTURE__ === true` (set by
`dev.html`) or the URL has a `?fixture` param.

## How to change the layout

Layout is data, not code. Edit `config/layout.json`, save, refresh the page.
No JS edit is ever required to add, move, resize, or remove a panel.

Each entry in `panels[]`:

```jsonc
{
  "id": "fleet",              // unique, used as the DOM anchor + patch target
  "type": "agent_grid",       // must be a key in web/js/registry.js
  "title": "FLEET",           // panel header text
  "x": 0, "y": 0, "w": 8, "h": 5,   // grid position/size, in columns/rows
  "options": { "show_idle": true, "sort": "status" }  // passed to the widget as-is
}
```

`grid.columns` (default 12), `grid.row_height` (px), and `grid.gap` (px)
control the overall grid; panels position themselves with `x`/`y`/`w`/`h` in
that coordinate system (0-indexed, `w`/`h` in column/row units).

Example -- move the burn gauge to the top-left and widen it: change its
`"x": 8, "w": 4` to `"x": 0, "w": 6` (and shuffle whatever was there),
save, refresh. Nothing else changes.

If `type` doesn't match a known widget, the panel renders a visible
placeholder card naming the missing type instead of a blank space or a
crash -- try setting a panel's `type` to `"nonsense"` and refresh to see it.

## How to add a widget

1. Create `web/js/widgets/<type>.js` exporting the widget contract:

   ```js
   export default {
     title: "My Widget",       // fallback label
     minW: 3, minH: 2,         // documented minimum size, not enforced
     render(el, { data, options, panel, bus, window }) {
       // data   = the full Snapshot (pick what you need)
       // options = this panel's `options` object from layout.json
       // panel  = the full panel config ({id, type, title, x, y, w, h, ...})
       // bus    = {on, off, emit} pub/sub; app.js emits "window:change"
       //          when the header's today/7d/30d selector changes
       // window = the currently selected window ("today" | "7d" | "30d")
       el.textContent = "hello";
     },
     // optional: cheaper re-render for SSE patches. Falls back to render()
     // if omitted.
     update(el, ctx) { this.render(el, ctx); },
   };
   ```

2. Register it in `web/js/registry.js`:

   ```js
   import my_widget from "./widgets/my_widget.js";
   // ...
   const registry = { /* ...existing entries..., */ my_widget };
   ```

   Optionally add a `dependencies.my_widget = ["usage"]` entry there too, so
   SSE patches only re-render this widget when a relevant top-level snapshot
   key changes. Omitting it just means the widget re-renders on every patch
   -- harmless, just not as cheap.

3. Add a panel with `"type": "my_widget"` to `config/layout.json`.

That's the entire surface. Widgets are self-contained: don't import one
widget from another. `web/js/utils.js` (formatting helpers: `fmtTokens`,
`fmtCost`, `fmtRelTime`, `escapeHtml`, `get`, `setPath`, ...) is shared and
fine to import from any widget.

`app.js` wraps every `render()`/`update()` call in try/catch -- a widget
that throws shows an error card with the message; the rest of the grid keeps
working.

## Theming

`config/theme.json` colors/fonts/radius/density/motion values are applied as
CSS custom properties on `:root` at boot (`--color-*`, `--font-*`,
`--radius-*`, `--density-*`, `--motion-*`). `web/css/style.css` has matching
defaults so there's no flash of unstyled content; editing `theme.json` and
refreshing changes the entire look with zero JS/CSS edits.

`theme.json._presets.light-ops` is a worked example of a second theme --
copy its `colors` block up over the top-level `colors` block to switch to a
light theme.

`theme.json.reload_banner` controls the "new version available" banner
(`enabled`, and `auto_reload_after_s` -- set a number to reload the page
automatically that many seconds after a new build is detected instead of
waiting for a click; `null` means manual only).

## Live updates

`app.js` boots by fetching `/api/snapshot`, then opens `GET /api/stream`
(SSE). `patch` events (`{"paths": {"usage.burn": {...}, ...}}`) are merged
into the in-memory snapshot at the given dotted path (replacing that path's
value, not deep-merging) and only the widgets that declared a dependency on
the touched top-level key(s) in `registry.js` re-render. `snapshot` events
(sent on connect and every 60s) trigger a full re-render. The connection
indicator in the header shows live/reconnecting/offline with the age of the
last good snapshot, and reconnects with exponential backoff (1s doubling to
30s) on stream errors.

When the backend's `version` field (`{"build", "started_at"}`) changes from
what was first seen at page load, a banner offers a reload. It never fires
on first load (that value becomes the baseline) and never fires if the
field is absent.

## Keyboard shortcuts

`?` help overlay, `r` force a resnapshot, `f` toggle fullscreen, `1`-`9`
focus that panel (by grid order), `Esc` closes the help overlay or dismisses
the reload banner.
