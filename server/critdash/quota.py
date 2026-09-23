"""Manually-triggered "remaining quota" check for each AI provider.

Hard rules (see AGENTS.md briefing for the full spec):
  - NEVER polled, never on page load, never on a timer. The only thing that
    triggers a live provider call is POST /api/quota/refresh, which a human
    clicks. GET /api/quota only ever returns the last cached result.
  - NEVER log, echo, or persist a credential. Every check_* function below
    takes an already-loaded token/key as a plain argument and returns a
    result dict built exclusively from named, hand-picked fields -- never a
    blanket "here's the raw provider response" dump that could carry a
    provider-echoed key fragment. Error strings built from a provider's raw
    HTTP body are passed through _redact() as a defense-in-depth backstop.
  - NEVER fabricate a quota. A provider with no usable endpoint (OpenCode
    Zen, Google/Gemini) returns ok=False with a plain-language reason and
    source=None -- no live call is even attempted, since none is possible.
    A provider whose endpoint IS real but whose available credential is the
    wrong type (xAI, OpenAI) or has expired (Kimi's short-lived OAuth token)
    gets ok=False, source="provider_api" (a live call WAS made) and the
    real error/status from that call -- never a guessed number.

Per-provider findings (verified empirically 2026-09-21, see the delivery
report for the full evidence trail):
  - openrouter: GET https://openrouter.ai/api/v1/auth/key, Bearer <key from
    ~/.local/share/opencode/auth.json:openrouter.key>. Live, 200, real data.
  - opencode_zen (key in the same auth.json under "opencode"): no balance/
    usage endpoint exists for API-key callers -- confirmed against
    https://opencode.ai/docs/zen/ and the open feature request
    (github.com/anomalyco/opencode issue #44189, "Zen API: expose credit
    balance"). Console-only today.
  - google (key in the same auth.json under "google", an OAuth access
    token for the Cloud Code CLI, not a generative-language API key):
    Gemini/Cloud quota lives in Cloud Console / Cloud Monitoring, not a
    per-key balance endpoint -- no live call attempted.
  - claude: no metered API billing (Claude Code is a subscription). The
    dashboard already derives a real 5h rate-limit block from local usage
    events (store.current_usage_block_start) -- surfaced here as
    source="local_derived", never source="provider_api".
  - kimi: GET https://api.kimi.ai/coding/v1/usages (base_url straight from
    ~/.kimi-code/config.toml's own provider config), Bearer <access_token
    from the newest file under ~/.kimi-code/credentials/*.json>. Endpoint is
    real (confirmed against api.kimi.ai directly: a 401 "invalid or expired"
    JSON error came back, not a 404/connection failure). Kimi's CLI issues
    15-minute access tokens refreshed transparently by the CLI itself while
    it's running; the token captured on disk between CLI sessions is
    routinely stale, which is exactly the failure this endpoint will report
    honestly rather than mask.
  - xai: GET https://api.x.ai/v1/api-key, Bearer <key> (xAI's documented
    management-API endpoint for a key's remaining_balance/spent_balance/
    total_granted, per docs.x.ai). The only credential on disk
    (~/.grok/auth.json) is a SpaceXAI/Grok-Build-CLI OAuth session token
    (issued by auth.x.ai for the coding-agent product), not a console-issued
    xAI API key -- tried live, rejected with 401 "API key is missing".
  - openai: OpenAI's usage/cost endpoints (api.openai.com/v1/organization/
    usage/*) require an organization Admin API key (scope api.usage.read),
    per OpenAI's own docs. The only credential on disk (~/.codex/auth.json)
    is a project key -- tried live, rejected with 403 "insufficient
    permissions... Missing scopes: api.usage.read".
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import UTC, datetime

import httpx

from .state import now_iso as _now_iso
from .store import BLOCK_DURATION

PROVIDER_NAMES: tuple[str, ...] = (
    "openrouter", "opencode_zen", "google", "claude", "kimi", "xai", "openai",
)

QUOTA_TIMEOUT_S = 10.0
REFRESH_MIN_INTERVAL_S = 30.0
DEFAULT_KIMI_USAGE_URL = "https://api.kimi.ai/coding/v1/usages"
BLOCK_DURATION_MIN = 300.0  # 5h, minutes -- matches store.BLOCK_DURATION

# Defense-in-depth only (see module docstring): redacts anything long and
# token-shaped out of a raw provider error body before it can reach a log
# line or an API response. The real guarantee is structural -- every result
# dict below is built field-by-field from named values, never a raw dump.
# Deliberately excludes '.' from the character class: real secrets (API
# keys, JWT segments) are long unbroken runs of base64url/hex; legitimate
# prose like an OAuth scope name ("monitoring.timeSeries.list") is short
# dot-separated words that would otherwise false-positive here.
_TOKENLIKE_RE = re.compile(r"[A-Za-z0-9_-]{32,}")


def _redact(text: str | None) -> str | None:
    if text is None:
        return None
    return _TOKENLIKE_RE.sub("[REDACTED]", text)


_UNSET = object()


def _result(
    provider: str, *, ok: bool | None, source: str | None = None,
    limit: float | None = None, remaining: float | None = None, used: float | None = None,
    unit: str | None = None, period: str | None = None, resets_at: str | None = None,
    extra: dict | None = None, error: str | None = None, checked_at=_UNSET,
) -> dict:
    """The one place the response shape is assembled -- every check_*
    function returns exactly this key set, so a caller (or a test) never has
    to guess which fields a given provider bothered to fill in. `checked_at`
    defaults to "now" (a check just ran); never_checked_entry() below is the
    one caller that passes checked_at=None explicitly, since for that entry
    no check has ever run."""
    return {
        "provider": provider, "ok": ok, "source": source,
        "limit": limit, "remaining": remaining, "used": used, "unit": unit,
        "period": period, "resets_at": resets_at, "extra": extra,
        "checked_at": _now_iso() if checked_at is _UNSET else checked_at,
        "error": _redact(error),
    }


def never_checked_entry(provider: str) -> dict:
    return _result(provider, ok=None, checked_at=None)


# -- credential loading -------------------------------------------------
# Every loader here reads a file already on disk and returns plain string
# key(s)/None -- never partial/masked values, never printed or logged.


def _read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def load_opencode_keys(path: str) -> dict[str, str]:
    """~/.local/share/opencode/auth.json holds three providers' keys under
    top-level {"openrouter": {"key": ...}, "opencode": {"key": ...},
    "google": {"key": ...}} -- see AGENTS.md briefing, confirmed live
    2026-09-21."""
    doc = _read_json(path)
    if not isinstance(doc, dict):
        return {}
    out: dict[str, str] = {}
    for name in ("openrouter", "opencode", "google"):
        entry = doc.get(name)
        if isinstance(entry, dict) and isinstance(entry.get("key"), str) and entry["key"]:
            out[name] = entry["key"]
    return out


def load_kimi_access_token(kimi_dir: str) -> tuple[str | None, str | None]:
    """Kimi Code CLI's OAuth access token, from the newest file under
    <kimi_dir>/credentials/*.json (mtime-sorted -- there is normally exactly
    one, but this is robust to a re-login leaving an older file behind).
    Returns (token, None) or (None, human-reason)."""
    cred_dir = os.path.join(os.path.expanduser(kimi_dir), "credentials")
    try:
        names = [n for n in os.listdir(cred_dir) if n.endswith(".json")]
    except OSError:
        return None, f"no Kimi credentials directory at {cred_dir}"
    if not names:
        return None, f"no Kimi credential files found in {cred_dir}"
    names.sort(key=lambda n: os.path.getmtime(os.path.join(cred_dir, n)), reverse=True)
    doc = _read_json(os.path.join(cred_dir, names[0]))
    if not isinstance(doc, dict) or not isinstance(doc.get("access_token"), str) or not doc["access_token"]:
        return None, f"Kimi credential file {names[0]} has no access_token field"
    return doc["access_token"], None


def load_grok_key(path: str) -> str | None:
    """~/.grok/auth.json keys OAuth session records by
    "https://auth.x.ai::<agent-id>" -- the key we want is that record's
    "key" field (a JWT-shaped bearer token, not a console xAI API key)."""
    doc = _read_json(path)
    if not isinstance(doc, dict):
        return None
    for k, v in doc.items():
        if k.startswith("https://auth.x.ai") and isinstance(v, dict):
            key = v.get("key")
            if isinstance(key, str) and key:
                return key
    return None


def load_openai_key(path: str) -> str | None:
    """~/.codex/auth.json is {"auth_mode": "apikey", "OPENAI_API_KEY": "..."}."""
    doc = _read_json(path)
    if not isinstance(doc, dict):
        return None
    val = doc.get("OPENAI_API_KEY")
    return val if isinstance(val, str) and val else None


def _safe_body(r: httpx.Response, limit: int = 200) -> str:
    try:
        text = r.text
    except Exception:  # noqa: BLE001 - a broken response body must never crash the check
        return ""
    return (text or "")[:limit]


# -- per-provider checks -------------------------------------------------
# Every check_* is `async def` (even the ones that never await anything) so
# refresh_all can schedule all seven uniformly with asyncio.gather.


async def check_openrouter(client: httpx.AsyncClient, key: str | None) -> dict:
    if not key:
        return _result(
            "openrouter", ok=False,
            error="no API key found in ~/.local/share/opencode/auth.json (openrouter.key)",
        )
    try:
        r = await client.get(
            "https://openrouter.ai/api/v1/auth/key", headers={"Authorization": f"Bearer {key}"}
        )
    except httpx.TimeoutException:
        return _result("openrouter", ok=False, source="provider_api", error="request timed out")
    except httpx.HTTPError as exc:
        return _result(
            "openrouter", ok=False, source="provider_api",
            error=f"request failed: {type(exc).__name__}",
        )

    if r.status_code != 200:
        return _result(
            "openrouter", ok=False, source="provider_api",
            error=f"HTTP {r.status_code} from OpenRouter: {_safe_body(r)}",
        )
    try:
        data = r.json()["data"]
        if not isinstance(data, dict):
            raise TypeError
    except (KeyError, TypeError, ValueError):
        return _result("openrouter", ok=False, source="provider_api", error="unrecognized response shape")

    # limit/limit_remaining are 0 (not null) when the key has no configured
    # dollar spend cap -- a literal 0 there would misread as "no quota left",
    # so a cap is only reported when `limit` is actually truthy. The genuine
    # dollar figures (usage_monthly etc.) and the real, always-bounded
    # free_model_daily_requests window still get surfaced either way.
    limit = data.get("limit")
    has_cap = bool(limit)
    free_daily = data.get("free_model_daily_requests") or {}
    return _result(
        "openrouter", ok=True, source="provider_api",
        limit=limit if has_cap else None,
        remaining=data.get("limit_remaining") if has_cap else None,
        used=(limit - data.get("limit_remaining", 0)) if has_cap else data.get("usage_monthly"),
        unit="usd",
        period=(data.get("limit_reset") or "monthly") if has_cap else "monthly",
        resets_at=None,
        extra={
            "cap_configured": has_cap,
            "usage": data.get("usage"), "usage_daily": data.get("usage_daily"),
            "usage_weekly": data.get("usage_weekly"), "usage_monthly": data.get("usage_monthly"),
            "is_free_tier": data.get("is_free_tier"),
            "free_model_daily_requests": free_daily,
        },
    )


async def check_opencode_zen(key: str | None) -> dict:
    # No live call: no balance/usage endpoint exists for OpenCode Zen API
    # keys today (see module docstring). Reporting this unconditionally,
    # whether or not a key is present, since the answer is the same either
    # way -- there is nothing to query.
    reason = (
        "OpenCode Zen has no documented balance/usage endpoint for API keys "
        "(confirmed against opencode.ai/docs/zen/; a balance endpoint is a "
        "requested but unshipped feature -- github.com/anomalyco/opencode "
        "issue #44189). Balance is only visible in the web console."
    )
    if not key:
        reason += " (no key found in ~/.local/share/opencode/auth.json either.)"
    return _result("opencode_zen", ok=False, error=reason)


async def check_google(key: str | None) -> dict:
    # No live call: see module docstring -- Gemini/Cloud quota has no simple
    # per-key balance endpoint, and the stored credential is an OAuth access
    # token for the Cloud Code CLI, not a service-account credential with
    # Cloud Monitoring scope.
    reason = (
        "Gemini/Google quota is managed in Google Cloud Console (Quotas & "
        "System Limits) or the Cloud Monitoring API, not a per-key balance "
        "endpoint. The stored credential is an OAuth access token for the "
        "coding-assistant CLI, not a service-account credential with "
        "monitoring.timeSeries.list scope, so no live check is possible "
        "with what's on disk."
    )
    if not key:
        reason += " (no key found in ~/.local/share/opencode/auth.json either.)"
    return _result("google", ok=False, error=reason)


def current_local_block(store, now: datetime | None = None) -> dict:
    """Construct the currently-active 5h rate-limit block dict (started_at/
    ends_at/tokens/cost_usd/pct_elapsed/active) from
    store.current_usage_block_start(), for check_claude_block below. This is
    the ccusage-style local reconstruction -- a block starts at the first
    message after the previous one ended -- not a real value read from any
    provider. It used to also feed the burn_gauge widget via /api/snapshot,
    but that surfaced it as if it were the account's real rate-limit window,
    which it cannot be (usage_events mixes every provider/host together);
    the burn_gauge feature was removed, and check_claude_block is now this
    function's only caller. `now` is overridable for tests; defaults to the
    real current time."""
    now = now or datetime.now(UTC)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    block_start = store.current_usage_block_start(now_iso)
    if not block_start:
        return {
            "started_at": None, "ends_at": None, "tokens": 0, "cost_usd": 0.0,
            "pct_elapsed": 0.0, "active": False,
        }
    start_dt = datetime.fromisoformat(block_start.replace("Z", "+00:00"))
    end_dt = start_dt + BLOCK_DURATION
    block_totals = store.usage_totals(block_start)
    elapsed = (now - start_dt).total_seconds()
    pct = max(0.0, min(1.0, elapsed / BLOCK_DURATION.total_seconds()))
    return {
        "started_at": block_start,
        "ends_at": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tokens": block_totals["total"],
        "cost_usd": block_totals["cost_usd"],
        "pct_elapsed": round(pct, 4),
        "active": True,
    }


async def check_claude_block(block: dict | None) -> dict:
    """Purely local: derived from store.current_usage_block_start() via
    current_local_block() above. Never a network call, so this can never
    fail -- ok is always True."""
    block = block or {}
    if not block.get("active"):
        return _result(
            "claude", ok=True, source="local_derived", unit="minutes", period="5h_block",
            extra={"active": False, "note": "no active 5h rate-limit block right now"},
        )
    pct = block.get("pct_elapsed") or 0.0
    used_min = round(pct * BLOCK_DURATION_MIN, 1)
    remaining_min = round(BLOCK_DURATION_MIN - used_min, 1)
    return _result(
        "claude", ok=True, source="local_derived",
        limit=BLOCK_DURATION_MIN, remaining=remaining_min, used=used_min,
        unit="minutes", period="5h_block", resets_at=block.get("ends_at"),
        extra={
            "active": True, "started_at": block.get("started_at"),
            "tokens": block.get("tokens"), "cost_usd": block.get("cost_usd"),
            "pct_elapsed": pct,
        },
    )


def _parse_kimi_usage(payload) -> dict | None:
    """Defensive parse: two response shapes were found in independent
    third-party documentation (no live 200 was reachable to confirm one --
    the on-disk token was already expired at build time, see the delivery
    report), so this recognizes either and returns None -- never a guess --
    for anything else, which check_kimi turns into an honest ok=False rather
    than a fabricated number.

    Shape A: {"data": {"quota": {"usages": [{"usedRatio", "resetAt", ...}],
              "extraUsage": {...}}}}  (ratio-based, 0..1)
    Shape B: {"usage": {"limit","used","remaining","resetTime"},
              "limits": [{"window": {...}, "detail": {"limit","used",
              "remaining","resetTime"}}]}  (count-based)
    """
    if not isinstance(payload, dict):
        return None

    windows = None
    extra_usage = None
    data = payload.get("data")
    quota = data.get("quota") if isinstance(data, dict) else None
    if isinstance(quota, dict) and isinstance(quota.get("usages"), list) and quota["usages"]:
        windows = quota["usages"]
        extra_usage = quota.get("extraUsage")
    elif isinstance(payload.get("limits"), list) and payload["limits"]:
        windows = payload["limits"]

    if not windows:
        return None
    primary = windows[0]
    if not isinstance(primary, dict):
        return None

    detail = primary.get("detail") if isinstance(primary.get("detail"), dict) else primary
    ratio = primary.get("usedRatio")

    if isinstance(ratio, int | float):
        limit_f, used_f = 100.0, round(float(ratio) * 100, 1)
        remaining_f = round(limit_f - used_f, 1)
        unit = "percent"
        reset = primary.get("resetAt")
    else:
        used, limit_v, remaining = detail.get("used"), detail.get("limit"), detail.get("remaining")
        if used is None or limit_v is None:
            return None
        try:
            used_f, limit_f = float(used), float(limit_v)
        except (TypeError, ValueError):
            return None
        remaining_f = float(remaining) if isinstance(remaining, int | float) else max(0.0, limit_f - used_f)
        unit = "requests"
        reset = detail.get("resetTime")

    window_meta = primary.get("window") if isinstance(primary.get("window"), dict) else {}
    period = window_meta.get("timeUnit") or ("5h" if len(windows) == 1 else "window")

    return {
        "limit": limit_f, "remaining": remaining_f, "used": used_f, "unit": unit,
        "period": period, "resets_at": reset if isinstance(reset, str) else None,
        "extra": {"windows": windows, "extra_usage": extra_usage},
    }


async def check_kimi(client: httpx.AsyncClient, kimi_dir: str, usage_url: str) -> dict:
    token, cred_err = load_kimi_access_token(kimi_dir)
    if token is None:
        return _result("kimi", ok=False, error=cred_err)

    try:
        r = await client.get(usage_url, headers={"Authorization": f"Bearer {token}"})
    except httpx.TimeoutException:
        return _result("kimi", ok=False, source="provider_api", error="request timed out")
    except httpx.HTTPError as exc:
        return _result("kimi", ok=False, source="provider_api", error=f"request failed: {type(exc).__name__}")

    if r.status_code == 401:
        return _result(
            "kimi", ok=False, source="provider_api",
            error=(
                "access token rejected (Kimi CLI issues short-lived ~15-minute "
                "OAuth session tokens; the one on disk had expired -- run the "
                "kimi CLI once to refresh it, then retry)"
            ),
        )
    if r.status_code != 200:
        return _result(
            "kimi", ok=False, source="provider_api",
            error=f"HTTP {r.status_code} from Kimi: {_safe_body(r)}",
        )
    try:
        payload = r.json()
    except ValueError:
        return _result("kimi", ok=False, source="provider_api", error="response was not valid JSON")

    parsed = _parse_kimi_usage(payload)
    if parsed is None:
        return _result("kimi", ok=False, source="provider_api", error="response shape not recognized")
    return _result("kimi", ok=True, source="provider_api", **parsed)


async def check_xai(client: httpx.AsyncClient, key: str | None) -> dict:
    if not key:
        return _result("xai", ok=False, error="no credential found in ~/.grok/auth.json")

    try:
        r = await client.get("https://api.x.ai/v1/api-key", headers={"Authorization": f"Bearer {key}"})
    except httpx.TimeoutException:
        return _result("xai", ok=False, source="provider_api", error="request timed out")
    except httpx.HTTPError as exc:
        return _result("xai", ok=False, source="provider_api", error=f"request failed: {type(exc).__name__}")

    if r.status_code != 200:
        return _result(
            "xai", ok=False, source="provider_api",
            error=(
                "the stored credential is a SpaceXAI/Grok-Build-CLI OAuth session "
                "token, not a console-issued xAI API key; xAI's documented balance "
                f"endpoint (GET https://api.x.ai/v1/api-key) rejected it "
                f"(HTTP {r.status_code}: {_safe_body(r)}). A real xAI API key is "
                "needed for live quota."
            ),
        )
    try:
        data = r.json()
        if not isinstance(data, dict):
            raise TypeError
    except (TypeError, ValueError):
        return _result("xai", ok=False, source="provider_api", error="response was not valid JSON")

    remaining = data.get("remaining_balance")
    if remaining is None:
        return _result("xai", ok=False, source="provider_api", error="unrecognized response shape")
    return _result(
        "xai", ok=True, source="provider_api",
        limit=data.get("total_granted"), remaining=remaining, used=data.get("spent_balance"),
        unit="usd", period="granted", resets_at=None,
        extra={"team_id": data.get("team_id"), "name": data.get("name")},
    )


async def check_openai(client: httpx.AsyncClient, key: str | None) -> dict:
    if not key:
        return _result("openai", ok=False, error="no credential found in ~/.codex/auth.json")

    start_time = int(time.time()) - 3600
    url = f"https://api.openai.com/v1/organization/usage/completions?start_time={start_time}"
    try:
        r = await client.get(url, headers={"Authorization": f"Bearer {key}"})
    except httpx.TimeoutException:
        return _result("openai", ok=False, source="provider_api", error="request timed out")
    except httpx.HTTPError as exc:
        return _result(
            "openai", ok=False, source="provider_api", error=f"request failed: {type(exc).__name__}",
        )

    if r.status_code in (401, 403):
        return _result(
            "openai", ok=False, source="provider_api",
            error=(
                "the stored credential (~/.codex/auth.json OPENAI_API_KEY) is a "
                "project API key; OpenAI's usage/cost endpoints require an "
                "organization Admin API key (scope api.usage.read), which is not "
                f"present (HTTP {r.status_code}: {_safe_body(r)}). Needs an Admin "
                "key from platform.openai.com to go live."
            ),
        )
    if r.status_code != 200:
        return _result(
            "openai", ok=False, source="provider_api",
            error=f"HTTP {r.status_code} from OpenAI: {_safe_body(r)}",
        )
    try:
        payload = r.json()
        buckets = payload.get("data") if isinstance(payload, dict) else None
    except ValueError:
        return _result("openai", ok=False, source="provider_api", error="response was not valid JSON")
    if not isinstance(buckets, list):
        return _result("openai", ok=False, source="provider_api", error="unrecognized response shape")

    total_tokens = 0
    for bucket in buckets:
        for row in (bucket.get("results") or []) if isinstance(bucket, dict) else []:
            total_tokens += (row.get("input_tokens") or 0) + (row.get("output_tokens") or 0)
    return _result(
        "openai", ok=True, source="provider_api",
        used=float(total_tokens), unit="tokens", period="trailing_1h",
        extra={"note": "usage only -- OpenAI's usage endpoint has no remaining/limit concept"},
    )


# -- orchestration ---------------------------------------------------------


async def _guard(provider: str, coro, timeout_s: float) -> dict:
    """Every provider check gets its own timeout AND a catch-all -- a
    provider check must never raise into refresh_all and take the other six
    down with it."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout_s)
    except TimeoutError:
        return _result(
            provider, ok=False, source="provider_api", error=f"timed out after {timeout_s:.0f}s",
        )
    except Exception as exc:  # noqa: BLE001 - a check must never crash the refresh endpoint
        return _result(
            provider, ok=False, source="provider_api",
            error=f"unexpected error: {type(exc).__name__}",
        )


async def refresh_all(config, block: dict, *, client: httpx.AsyncClient | None = None) -> dict[str, dict]:
    """Runs every provider's live check concurrently. Never called on a
    timer or on page load -- see module docstring -- only from
    POST /api/quota/refresh."""
    opencode_keys = load_opencode_keys(config.expand("opencode_auth_path"))
    grok_key = load_grok_key(config.expand("grok_auth_path"))
    openai_key = load_openai_key(config.expand("codex_auth_path"))
    kimi_dir = config.expand("kimi_dir")
    kimi_usage_url = config.sources.get("kimi_usage_url", DEFAULT_KIMI_USAGE_URL)
    timeout_s = float(config.sources.get("quota_timeout_s", QUOTA_TIMEOUT_S))

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=timeout_s)
    try:
        guard_timeout = timeout_s + 2.0
        results = await asyncio.gather(
            _guard("openrouter", check_openrouter(client, opencode_keys.get("openrouter")), guard_timeout),
            _guard("opencode_zen", check_opencode_zen(opencode_keys.get("opencode")), guard_timeout),
            _guard("google", check_google(opencode_keys.get("google")), guard_timeout),
            _guard("claude", check_claude_block(block), guard_timeout),
            _guard("kimi", check_kimi(client, kimi_dir, kimi_usage_url), guard_timeout),
            _guard("xai", check_xai(client, grok_key), guard_timeout),
            _guard("openai", check_openai(client, openai_key), guard_timeout),
        )
    finally:
        if owns_client:
            await client.aclose()
    return {r["provider"]: r for r in results}


# -- cache-facing response builders (used directly by main.py's endpoints) --


def build_quota_response(cache: dict[str, dict], stale_after_s: float) -> dict:
    """GET /api/quota's body: the last cached result per provider, never a
    network call. A provider missing from `cache` (never refreshed since the
    DB was created) gets a distinct ok=None "never checked" entry rather
    than looking like a failed check."""
    now = datetime.now(UTC)
    providers = []
    any_stale = False
    for name in PROVIDER_NAMES:
        entry = cache.get(name)
        if entry is None:
            providers.append(never_checked_entry(name))
            any_stale = True
            continue
        providers.append(entry)
        stale = True
        checked_at = entry.get("checked_at")
        if checked_at:
            try:
                dt = datetime.fromisoformat(str(checked_at).replace("Z", "+00:00"))
                stale = (now - dt).total_seconds() > stale_after_s
            except ValueError:
                stale = True
        if stale:
            any_stale = True
    return {"providers": providers, "stale": any_stale, "generated_at": _now_iso()}


# -- static provider-availability classification (GET /api/settings/suggest) -
# Derived from this module's own verified per-provider findings above (never
# a live call -- see the hard rules in the module docstring; this endpoint is
# a GET that must never make one). never_available providers have NO live
# endpoint at all (check_opencode_zen/check_google never issue a network
# request -- source is always None). needs_credential providers DO have a
# real, documented endpoint, but the only credential found on disk on the
# hosts this was verified against is the wrong type or expired. working
# providers succeed with what's on disk today.
NEVER_AVAILABLE_PROVIDERS: tuple[str, ...] = ("opencode_zen", "google")
NEEDS_CREDENTIAL_PROVIDERS: tuple[str, ...] = ("kimi", "xai", "openai")
WORKING_PROVIDERS: tuple[str, ...] = ("openrouter", "claude")


def provider_availability_buckets() -> dict[str, list[str]]:
    return {
        "never_available": list(NEVER_AVAILABLE_PROVIDERS),
        "needs_credential": list(NEEDS_CREDENTIAL_PROVIDERS),
        "working": list(WORKING_PROVIDERS),
    }


class RateLimited(Exception):
    def __init__(self, retry_after_s: float):
        super().__init__(f"refresh rate-limited; retry in {retry_after_s:.0f}s")
        self.retry_after_s = retry_after_s


class RefreshState:
    """Holds the monotonic timestamp of the last refresh. In-memory only by
    design -- resets on a dashboard restart, which is fine: the rate limit
    exists to stop a user hammering the button in one running session, not
    to survive a restart."""

    def __init__(self):
        self.last_refresh_monotonic: float | None = None


def check_refresh_rate_limit(state: RefreshState, now_monotonic: float, min_interval_s: float) -> None:
    if state.last_refresh_monotonic is not None:
        elapsed = now_monotonic - state.last_refresh_monotonic
        if elapsed < min_interval_s:
            raise RateLimited(min_interval_s - elapsed)


async def do_refresh(
    config, store, block: dict, state: RefreshState, *, client: httpx.AsyncClient | None = None,
) -> dict:
    """POST /api/quota/refresh's full body: rate-limit gate, live checks,
    persist to the store, return the fresh result. Raises RateLimited (the
    caller maps that to HTTP 429) rather than silently serving a stale
    cache, so a hammering client gets an honest refusal, not a quiet no-op."""
    min_interval_s = float(config.sources.get("quota_refresh_min_interval_s", REFRESH_MIN_INTERVAL_S))
    check_refresh_rate_limit(state, time.monotonic(), min_interval_s)
    state.last_refresh_monotonic = time.monotonic()

    results = await refresh_all(config, block, client=client)
    for name, entry in results.items():
        store.set_quota_cache(name, entry)
    return {"providers": [results[name] for name in PROVIDER_NAMES], "generated_at": _now_iso()}


__all__ = [
    "NEEDS_CREDENTIAL_PROVIDERS",
    "NEVER_AVAILABLE_PROVIDERS",
    "PROVIDER_NAMES",
    "WORKING_PROVIDERS",
    "RateLimited",
    "RefreshState",
    "build_quota_response",
    "provider_availability_buckets",
    "check_claude_block",
    "current_local_block",
    "check_google",
    "check_kimi",
    "check_opencode_zen",
    "check_openai",
    "check_openrouter",
    "check_xai",
    "check_refresh_rate_limit",
    "do_refresh",
    "load_grok_key",
    "load_kimi_access_token",
    "load_opencode_keys",
    "load_openai_key",
    "never_checked_entry",
    "refresh_all",
]
