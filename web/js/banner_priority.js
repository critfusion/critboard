// Pure decision logic for the reload-banner vs update-banner mutual
// exclusion (see app.js's "reload banner" / "update banner" sections). No
// DOM access at all -- kept separate and dependency-free on purpose so it
// can be tested directly under plain `node`, since this repo has no
// frontend test harness (see server/tests/test_banner_priority.py, which
// shells out to `node` to run web/js/banner_priority.test.mjs against this
// file).
//
// The rule (macOS install report, defect 3): #reload-banner and
// #update-banner must never both show at once. The update banner is the
// actionable one (it has a "New version" story too, but it's specifically
// "a newer commit exists upstream, click to pull it") and takes priority
// over the reload banner (just "the server restarted, reload the page").

/** Whether the update banner should currently be considered active (visible,
 * or about to become visible), given the `update` object from
 * /api/snapshot's "update" key and the key of whichever update the viewer
 * last dismissed. Mirrors renderUpdateBanner()'s own visibility decision in
 * app.js -- keep the two in sync if either changes. */
export function isUpdateBannerActive(update, dismissedUpdateKey) {
  if (!update || !update.update_available) return false;
  const key = update.latest || null;
  if (key !== null && key === dismissedUpdateKey) return false;
  return true;
}
