"""critdash/quota.py: manually-triggered AI-provider quota checks.

Every test here uses httpx.MockTransport (built into httpx -- no extra
dependency) to fake provider responses. No test in this file makes a real
network call. Coverage: success, timeout, auth failure, malformed response,
the refresh rate-limit, credential-loading edge cases, and -- for every
scenario that touches a fake credential -- a positive assertion that the
credential never appears anywhere in the serialized result.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from critdash import quota

FAKE_OPENROUTER_KEY = "sk-or-v1-FAKE0123456789abcdef0123456789abcdef"
FAKE_ZEN_KEY = "sk-zenFAKE0123456789abcdef0123456789"
FAKE_GOOGLE_KEY = "AQ.FAKE0123456789abcdef0123456789abcdef"
FAKE_KIMI_TOKEN = "eyJFAKEKIMITOKEN0123456789abcdef0123456789abcdef"
FAKE_GROK_KEY = "eyJFAKEGROKTOKEN0123456789abcdef0123456789abcdef"
FAKE_OPENAI_KEY = "sk-proj-FAKE0123456789abcdef0123456789abcdef"

ALL_FAKE_SECRETS = [
    FAKE_OPENROUTER_KEY, FAKE_ZEN_KEY, FAKE_GOOGLE_KEY,
    FAKE_KIMI_TOKEN, FAKE_GROK_KEY, FAKE_OPENAI_KEY,
]


def assert_no_secrets(obj) -> None:
    blob = json.dumps(obj)
    for secret in ALL_FAKE_SECRETS:
        assert secret not in blob, f"credential leaked into serialized output: {secret[:8]}..."


def mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# -- credential loaders ---------------------------------------------------


def test_load_opencode_keys_reads_all_three(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({
        "opencode": {"type": "api", "key": FAKE_ZEN_KEY},
        "google": {"type": "api", "key": FAKE_GOOGLE_KEY},
        "openrouter": {"type": "api", "key": FAKE_OPENROUTER_KEY},
    }))
    keys = quota.load_opencode_keys(str(path))
    assert keys == {"opencode": FAKE_ZEN_KEY, "google": FAKE_GOOGLE_KEY, "openrouter": FAKE_OPENROUTER_KEY}


def test_load_opencode_keys_missing_file_returns_empty(tmp_path):
    assert quota.load_opencode_keys(str(tmp_path / "nope.json")) == {}


def test_load_opencode_keys_malformed_json_returns_empty(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text("{not json")
    assert quota.load_opencode_keys(str(path)) == {}


def test_load_kimi_access_token_reads_newest_file(tmp_path):
    cred_dir = tmp_path / "credentials"
    cred_dir.mkdir()
    old = cred_dir / "old.json"
    old.write_text(json.dumps({"access_token": "old-token"}))
    new = cred_dir / "new.json"
    new.write_text(json.dumps({"access_token": FAKE_KIMI_TOKEN}))
    import os
    now = time.time()
    os.utime(old, (now - 100, now - 100))
    os.utime(new, (now, now))
    token, err = quota.load_kimi_access_token(str(tmp_path))
    assert token == FAKE_KIMI_TOKEN
    assert err is None


def test_load_kimi_access_token_no_credentials_dir(tmp_path):
    token, err = quota.load_kimi_access_token(str(tmp_path / "does-not-exist"))
    assert token is None
    assert "no Kimi credentials directory" in err


def test_load_kimi_access_token_empty_dir(tmp_path):
    (tmp_path / "credentials").mkdir()
    token, err = quota.load_kimi_access_token(str(tmp_path))
    assert token is None
    assert "no Kimi credential files" in err


def test_load_kimi_access_token_missing_field(tmp_path):
    cred_dir = tmp_path / "credentials"
    cred_dir.mkdir()
    (cred_dir / "a.json").write_text(json.dumps({"refresh_token": "x"}))
    token, err = quota.load_kimi_access_token(str(tmp_path))
    assert token is None
    assert "no access_token field" in err


def test_load_grok_key_finds_oidc_record(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({
        "https://auth.x.ai::some-agent-id": {"key": FAKE_GROK_KEY, "auth_mode": "oidc"},
    }))
    assert quota.load_grok_key(str(path)) == FAKE_GROK_KEY


def test_load_grok_key_missing_file(tmp_path):
    assert quota.load_grok_key(str(tmp_path / "nope.json")) is None


def test_load_openai_key(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": FAKE_OPENAI_KEY}))
    assert quota.load_openai_key(str(path)) == FAKE_OPENAI_KEY


def test_load_openai_key_missing_field(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"auth_mode": "apikey"}))
    assert quota.load_openai_key(str(path)) is None


# -- check_openrouter -------------------------------------------------------


async def test_openrouter_no_key_is_unavailable():
    result = await quota.check_openrouter(mock_client(lambda r: httpx.Response(200)), None)
    assert result["ok"] is False
    assert result["source"] is None
    assert "no API key" in result["error"]


async def test_openrouter_success_no_cap():
    def handler(request):
        assert request.headers["authorization"] == f"Bearer {FAKE_OPENROUTER_KEY}"
        return httpx.Response(200, json={"data": {
            "limit": 0, "limit_remaining": 0, "limit_reset": "daily",
            "usage": 12.5, "usage_daily": 1.0, "usage_weekly": 5.0, "usage_monthly": 12.5,
            "is_free_tier": False,
            "free_model_daily_requests": {"used": 3, "limit": 1000, "remaining": 997},
        }})
    async with mock_client(handler) as client:
        result = await quota.check_openrouter(client, FAKE_OPENROUTER_KEY)
    assert result["ok"] is True
    assert result["source"] == "provider_api"
    assert result["unit"] == "usd"
    assert result["limit"] is None  # no cap configured -> never fabricate a limit
    assert result["remaining"] is None
    assert result["used"] == 12.5
    assert result["extra"]["free_model_daily_requests"]["remaining"] == 997
    assert_no_secrets(result)


async def test_openrouter_success_with_cap():
    def handler(request):
        return httpx.Response(200, json={"data": {
            "limit": 100.0, "limit_remaining": 42.5, "limit_reset": "monthly",
            "usage_monthly": 57.5, "free_model_daily_requests": {"used": 0, "limit": 1000, "remaining": 1000},
        }})
    async with mock_client(handler) as client:
        result = await quota.check_openrouter(client, FAKE_OPENROUTER_KEY)
    assert result["ok"] is True
    assert result["limit"] == 100.0
    assert result["remaining"] == 42.5
    assert result["used"] == 57.5
    assert result["period"] == "monthly"


async def test_openrouter_timeout():
    def handler(request):
        raise httpx.TimeoutException("boom", request=request)
    async with mock_client(handler) as client:
        result = await quota.check_openrouter(client, FAKE_OPENROUTER_KEY)
    assert result["ok"] is False
    assert result["source"] == "provider_api"
    assert "timed out" in result["error"]
    assert_no_secrets(result)


async def test_openrouter_auth_failure():
    def handler(request):
        return httpx.Response(401, json={"error": "invalid key"})
    async with mock_client(handler) as client:
        result = await quota.check_openrouter(client, FAKE_OPENROUTER_KEY)
    assert result["ok"] is False
    assert "HTTP 401" in result["error"]
    assert_no_secrets(result)


async def test_openrouter_malformed_response():
    def handler(request):
        return httpx.Response(200, json={"unexpected": "shape"})
    async with mock_client(handler) as client:
        result = await quota.check_openrouter(client, FAKE_OPENROUTER_KEY)
    assert result["ok"] is False
    assert "unrecognized response shape" in result["error"]


async def test_openrouter_non_json_response():
    def handler(request):
        return httpx.Response(200, text="not json at all")
    async with mock_client(handler) as client:
        result = await quota.check_openrouter(client, FAKE_OPENROUTER_KEY)
    assert result["ok"] is False


# -- check_opencode_zen / check_google (no endpoint exists) -----------------


async def test_opencode_zen_always_unavailable_no_network():
    result = await quota.check_opencode_zen(FAKE_ZEN_KEY)
    assert result["ok"] is False
    assert result["source"] is None
    assert "no documented balance/usage endpoint" in result["error"]
    assert_no_secrets(result)


async def test_opencode_zen_notes_missing_key():
    result = await quota.check_opencode_zen(None)
    assert "no key found" in result["error"]


async def test_google_always_unavailable_no_network():
    result = await quota.check_google(FAKE_GOOGLE_KEY)
    assert result["ok"] is False
    assert result["source"] is None
    assert "Cloud Console" in result["error"] or "Cloud Monitoring" in result["error"]
    assert_no_secrets(result)


# -- check_claude_block (local_derived) --------------------------------------


async def test_claude_block_inactive():
    result = await quota.check_claude_block({"active": False})
    assert result["ok"] is True
    assert result["source"] == "local_derived"
    assert result["limit"] is None
    assert result["extra"]["active"] is False


async def test_claude_block_none_treated_as_inactive():
    result = await quota.check_claude_block(None)
    assert result["ok"] is True
    assert result["extra"]["active"] is False


async def test_claude_block_active_maps_pct_elapsed_to_minutes():
    block = {
        "active": True, "pct_elapsed": 0.5, "started_at": "2026-09-21T10:00:00Z",
        "ends_at": "2026-09-21T15:00:00Z", "tokens": 12345, "cost_usd": 3.21,
    }
    result = await quota.check_claude_block(block)
    assert result["ok"] is True
    assert result["source"] == "local_derived"
    assert result["limit"] == 300.0
    assert result["used"] == 150.0
    assert result["remaining"] == 150.0
    assert result["unit"] == "minutes"
    assert result["resets_at"] == "2026-09-21T15:00:00Z"
    assert result["extra"]["tokens"] == 12345


# -- check_kimi ---------------------------------------------------------


async def test_kimi_no_credential(tmp_path):
    result = await quota.check_kimi(
        mock_client(lambda r: httpx.Response(200)), str(tmp_path), quota.DEFAULT_KIMI_USAGE_URL
    )
    assert result["ok"] is False
    assert result["source"] is None


async def test_kimi_success_shape_a_ratio(tmp_path):
    cred_dir = tmp_path / "credentials"
    cred_dir.mkdir()
    (cred_dir / "a.json").write_text(json.dumps({"access_token": FAKE_KIMI_TOKEN}))

    def handler(request):
        assert request.headers["authorization"] == f"Bearer {FAKE_KIMI_TOKEN}"
        return httpx.Response(200, json={
            "code": 0, "msg": "success",
            "data": {"quota": {
                "usages": [{"usedRatio": 0.42, "resetAt": "2026-09-22T00:00:00Z"}],
                "extraUsage": {"balanceCents": 1000, "totalCents": 5000, "currency": "USD"},
            }},
        })
    async with mock_client(handler) as client:
        result = await quota.check_kimi(client, str(tmp_path), "https://api.kimi.ai/coding/v1/usages")
    assert result["ok"] is True
    assert result["source"] == "provider_api"
    assert result["unit"] == "percent"
    assert result["used"] == 42.0
    assert result["remaining"] == 58.0
    assert result["resets_at"] == "2026-09-22T00:00:00Z"
    assert_no_secrets(result)


async def test_kimi_success_shape_b_counts(tmp_path):
    cred_dir = tmp_path / "credentials"
    cred_dir.mkdir()
    (cred_dir / "a.json").write_text(json.dumps({"access_token": FAKE_KIMI_TOKEN}))

    def handler(request):
        return httpx.Response(200, json={
            "usage": {
                "limit": "2048", "used": "214", "remaining": "1834",
                "resetTime": "2026-09-22T00:00:00Z",
            },
            "limits": [{
                "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                "detail": {
                    "limit": "200", "used": "139", "remaining": "61",
                    "resetTime": "2026-09-21T13:00:00Z",
                },
            }],
        })
    async with mock_client(handler) as client:
        result = await quota.check_kimi(client, str(tmp_path), "https://api.kimi.ai/coding/v1/usages")
    assert result["ok"] is True
    assert result["unit"] == "requests"
    assert result["limit"] == 200.0
    assert result["used"] == 139.0
    assert result["remaining"] == 61.0


async def test_kimi_expired_token_401(tmp_path):
    cred_dir = tmp_path / "credentials"
    cred_dir.mkdir()
    (cred_dir / "a.json").write_text(json.dumps({"access_token": FAKE_KIMI_TOKEN}))

    def handler(request):
        return httpx.Response(
            401, json={"error": {"message": "The API Key appears to be invalid or may have expired."}}
        )
    async with mock_client(handler) as client:
        result = await quota.check_kimi(client, str(tmp_path), "https://api.kimi.ai/coding/v1/usages")
    assert result["ok"] is False
    assert result["source"] == "provider_api"
    assert "expired" in result["error"] or "rejected" in result["error"]
    assert_no_secrets(result)


async def test_kimi_timeout(tmp_path):
    cred_dir = tmp_path / "credentials"
    cred_dir.mkdir()
    (cred_dir / "a.json").write_text(json.dumps({"access_token": FAKE_KIMI_TOKEN}))

    def handler(request):
        raise httpx.TimeoutException("boom", request=request)
    async with mock_client(handler) as client:
        result = await quota.check_kimi(client, str(tmp_path), "https://api.kimi.ai/coding/v1/usages")
    assert result["ok"] is False
    assert "timed out" in result["error"]


async def test_kimi_unrecognized_shape_never_fabricates(tmp_path):
    cred_dir = tmp_path / "credentials"
    cred_dir.mkdir()
    (cred_dir / "a.json").write_text(json.dumps({"access_token": FAKE_KIMI_TOKEN}))

    def handler(request):
        return httpx.Response(200, json={"totally": "different"})
    async with mock_client(handler) as client:
        result = await quota.check_kimi(client, str(tmp_path), "https://api.kimi.ai/coding/v1/usages")
    assert result["ok"] is False
    assert result["remaining"] is None
    assert result["used"] is None
    assert "not recognized" in result["error"]


async def test_kimi_non_json_response(tmp_path):
    cred_dir = tmp_path / "credentials"
    cred_dir.mkdir()
    (cred_dir / "a.json").write_text(json.dumps({"access_token": FAKE_KIMI_TOKEN}))

    def handler(request):
        return httpx.Response(200, text="<html>not json</html>")
    async with mock_client(handler) as client:
        result = await quota.check_kimi(client, str(tmp_path), "https://api.kimi.ai/coding/v1/usages")
    assert result["ok"] is False
    assert "not valid JSON" in result["error"]


# -- check_xai ---------------------------------------------------------


async def test_xai_no_credential():
    result = await quota.check_xai(mock_client(lambda r: httpx.Response(200)), None)
    assert result["ok"] is False
    assert result["source"] is None


async def test_xai_success():
    def handler(request):
        assert request.headers["authorization"] == f"Bearer {FAKE_GROK_KEY}"
        return httpx.Response(200, json={
            "name": "my-key", "team_id": "t1",
            "remaining_balance": 42.5, "spent_balance": 7.5, "total_granted": 50.0,
        })
    async with mock_client(handler) as client:
        result = await quota.check_xai(client, FAKE_GROK_KEY)
    assert result["ok"] is True
    assert result["source"] == "provider_api"
    assert result["remaining"] == 42.5
    assert result["limit"] == 50.0
    assert result["unit"] == "usd"
    assert_no_secrets(result)


async def test_xai_oauth_token_rejected_as_unavailable():
    def handler(request):
        return httpx.Response(401, json={"code": "unauthenticated", "error": "API key is missing."})
    async with mock_client(handler) as client:
        result = await quota.check_xai(client, FAKE_GROK_KEY)
    assert result["ok"] is False
    assert result["source"] == "provider_api"
    assert "not a console-issued xAI API key" in result["error"]
    assert_no_secrets(result)


async def test_xai_timeout():
    def handler(request):
        raise httpx.TimeoutException("boom", request=request)
    async with mock_client(handler) as client:
        result = await quota.check_xai(client, FAKE_GROK_KEY)
    assert result["ok"] is False
    assert "timed out" in result["error"]


# -- check_openai ---------------------------------------------------------


async def test_openai_no_credential():
    result = await quota.check_openai(mock_client(lambda r: httpx.Response(200)), None)
    assert result["ok"] is False
    assert result["source"] is None


async def test_openai_project_key_rejected_needs_admin_key():
    def handler(request):
        return httpx.Response(403, json={
            "error": "You have insufficient permissions for this operation. Missing scopes: api.usage.read."
        })
    async with mock_client(handler) as client:
        result = await quota.check_openai(client, FAKE_OPENAI_KEY)
    assert result["ok"] is False
    assert result["source"] == "provider_api"
    assert "Admin API key" in result["error"]
    assert_no_secrets(result)


async def test_openai_timeout():
    def handler(request):
        raise httpx.TimeoutException("boom", request=request)
    async with mock_client(handler) as client:
        result = await quota.check_openai(client, FAKE_OPENAI_KEY)
    assert result["ok"] is False
    assert "timed out" in result["error"]


async def test_openai_success_shape():
    def handler(request):
        return httpx.Response(200, json={"data": [
            {"results": [
                {"input_tokens": 100, "output_tokens": 50},
                {"input_tokens": 10, "output_tokens": 5},
            ]},
        ]})
    async with mock_client(handler) as client:
        result = await quota.check_openai(client, FAKE_OPENAI_KEY)
    assert result["ok"] is True
    assert result["used"] == 165.0
    assert result["unit"] == "tokens"
    assert result["limit"] is None


# -- refresh_all orchestration ------------------------------------------


class FakeConfig:
    def __init__(self, sources):
        self.sources = sources

    def expand(self, key):
        import os
        return os.path.expanduser(str(self.sources.get(key, "")))


def make_config(tmp_path, **overrides):
    opencode_path = tmp_path / "opencode-auth.json"
    opencode_path.write_text(json.dumps({
        "openrouter": {"key": FAKE_OPENROUTER_KEY},
        "opencode": {"key": FAKE_ZEN_KEY},
        "google": {"key": FAKE_GOOGLE_KEY},
    }))
    grok_path = tmp_path / "grok-auth.json"
    grok_path.write_text(json.dumps({"https://auth.x.ai::agent": {"key": FAKE_GROK_KEY}}))
    codex_path = tmp_path / "codex-auth.json"
    codex_path.write_text(json.dumps({"OPENAI_API_KEY": FAKE_OPENAI_KEY}))
    kimi_dir = tmp_path / "kimi-code"
    (kimi_dir / "credentials").mkdir(parents=True)
    (kimi_dir / "credentials" / "a.json").write_text(json.dumps({"access_token": FAKE_KIMI_TOKEN}))

    sources = {
        "opencode_auth_path": str(opencode_path),
        "grok_auth_path": str(grok_path),
        "codex_auth_path": str(codex_path),
        "kimi_dir": str(kimi_dir),
        "kimi_usage_url": "https://api.kimi.ai/coding/v1/usages",
        "quota_timeout_s": 1,
        "quota_refresh_min_interval_s": 30,
    }
    sources.update(overrides)
    return FakeConfig(sources)


async def test_refresh_all_returns_every_provider(tmp_path):
    config = make_config(tmp_path)

    def handler(request):
        url = str(request.url)
        if "openrouter.ai" in url:
            return httpx.Response(
                200, json={"data": {"limit": 0, "limit_remaining": 0, "usage_monthly": 1.0}}
            )
        if "kimi.ai" in url:
            return httpx.Response(401, json={"error": {"message": "expired"}})
        if "x.ai" in url:
            return httpx.Response(401, json={"error": "API key is missing."})
        if "openai.com" in url:
            return httpx.Response(403, json={"error": "Missing scopes: api.usage.read."})
        return httpx.Response(500)

    async with mock_client(handler) as client:
        results = await quota.refresh_all(config, {"active": False}, client=client)

    assert set(results.keys()) == set(quota.PROVIDER_NAMES)
    assert results["openrouter"]["ok"] is True
    assert results["opencode_zen"]["ok"] is False
    assert results["opencode_zen"]["source"] is None
    assert results["google"]["ok"] is False
    assert results["claude"]["ok"] is True
    assert results["claude"]["source"] == "local_derived"
    assert results["kimi"]["ok"] is False
    assert results["xai"]["ok"] is False
    assert results["openai"]["ok"] is False
    assert_no_secrets(results)


async def test_refresh_all_one_provider_hanging_past_timeout_does_not_block_others(tmp_path):
    config = make_config(tmp_path, quota_timeout_s=0.2)

    async def slow_handler(request):
        if "openrouter.ai" in str(request.url):
            import anyio
            await anyio.sleep(5)
        return httpx.Response(200, json={"data": {"limit": 0, "limit_remaining": 0, "usage_monthly": 1.0}})

    transport = httpx.MockTransport(slow_handler)
    async with httpx.AsyncClient(transport=transport, timeout=0.2) as client:
        results = await quota.refresh_all(config, {"active": False}, client=client)

    assert set(results.keys()) == set(quota.PROVIDER_NAMES)
    assert results["openrouter"]["ok"] is False
    assert "timed out" in results["openrouter"]["error"]
    # every other provider still completed despite openrouter hanging
    assert results["claude"]["ok"] is True


async def test_refresh_all_provider_exception_does_not_crash_the_batch(tmp_path, monkeypatch):
    config = make_config(tmp_path)

    async def boom(*a, **kw):
        raise RuntimeError("simulated bug in a provider check")

    monkeypatch.setattr(quota, "check_openrouter", boom)

    def handler(request):
        return httpx.Response(401, json={"error": "x"})

    async with mock_client(handler) as client:
        results = await quota.refresh_all(config, {"active": False}, client=client)

    assert results["openrouter"]["ok"] is False
    assert "unexpected error" in results["openrouter"]["error"]
    assert results["claude"]["ok"] is True  # unaffected by openrouter's crash


# -- build_quota_response / do_refresh / rate limiting -----------------------


def test_build_quota_response_never_checked_state():
    resp = quota.build_quota_response({}, stale_after_s=3600)
    assert resp["stale"] is True
    assert len(resp["providers"]) == len(quota.PROVIDER_NAMES)
    for p in resp["providers"]:
        assert p["ok"] is None
        assert p["checked_at"] is None
        assert p["error"] is None


def test_build_quota_response_fresh_entry_not_stale():
    fresh = quota.never_checked_entry("openrouter")
    fresh["ok"] = True
    fresh["checked_at"] = quota._now_iso()
    resp = quota.build_quota_response({"openrouter": fresh}, stale_after_s=3600)
    openrouter_entry = next(p for p in resp["providers"] if p["provider"] == "openrouter")
    assert openrouter_entry["ok"] is True
    others_never_checked = [p for p in resp["providers"] if p["provider"] != "openrouter"]
    assert all(p["ok"] is None for p in others_never_checked)
    assert resp["stale"] is True  # other 6 providers are still never-checked


def test_build_quota_response_old_entry_is_stale():
    entry = quota.never_checked_entry("openrouter")
    entry["ok"] = True
    entry["checked_at"] = "2020-01-01T00:00:00Z"
    resp = quota.build_quota_response({p: entry for p in quota.PROVIDER_NAMES}, stale_after_s=60)
    assert resp["stale"] is True


def test_check_refresh_rate_limit_allows_first_call():
    state = quota.RefreshState()
    quota.check_refresh_rate_limit(state, time.monotonic(), min_interval_s=30)  # no raise


def test_check_refresh_rate_limit_rejects_immediate_repeat():
    state = quota.RefreshState()
    state.last_refresh_monotonic = time.monotonic()
    with pytest.raises(quota.RateLimited) as excinfo:
        quota.check_refresh_rate_limit(state, time.monotonic(), min_interval_s=30)
    assert excinfo.value.retry_after_s > 0


def test_check_refresh_rate_limit_allows_after_interval_elapses():
    state = quota.RefreshState()
    state.last_refresh_monotonic = time.monotonic() - 31
    quota.check_refresh_rate_limit(state, time.monotonic(), min_interval_s=30)  # no raise


async def test_do_refresh_persists_to_store_and_returns_result(tmp_path):
    from critdash.store import Store

    store = Store(tmp_path / "test.db")
    config = make_config(tmp_path)
    state = quota.RefreshState()

    def handler(request):
        return httpx.Response(401, json={"error": "x"})

    async with mock_client(handler) as client:
        result = await quota.do_refresh(config, store, {"active": False}, state, client=client)

    assert len(result["providers"]) == len(quota.PROVIDER_NAMES)
    cached = store.get_quota_cache()
    assert set(cached.keys()) == set(quota.PROVIDER_NAMES)
    assert_no_secrets(result)
    assert_no_secrets(cached)
    store.close()


async def test_do_refresh_raises_rate_limited_on_immediate_repeat(tmp_path):
    from critdash.store import Store

    store = Store(tmp_path / "test.db")
    config = make_config(tmp_path)
    state = quota.RefreshState()

    def handler(request):
        return httpx.Response(401, json={"error": "x"})

    async with mock_client(handler) as client:
        await quota.do_refresh(config, store, {"active": False}, state, client=client)
        with pytest.raises(quota.RateLimited):
            await quota.do_refresh(config, store, {"active": False}, state, client=client)
    store.close()
