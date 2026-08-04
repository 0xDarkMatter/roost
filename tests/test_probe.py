"""Probe tests — respx mocks /api/oauth/usage; we verify end-to-end behaviour."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from claude_lb.models import Health, Profile
from claude_lb.probe import ANTHROPIC_BETA, ANTHROPIC_VERSION, API_URL, probe_many, probe_profile

USAGE_FIXTURES = Path(__file__).parent / "fixtures" / "oauth-usage"


def _ok_body() -> dict:
    return json.loads((USAGE_FIXTURES / "ok.json").read_text())


def _profile(name: str = "account-a") -> Profile:
    return Profile(
        name=name,
        access_token="sk-ant-oat01-test",
        credentials_path=f"/tmp/{name}/.credentials.json",
        credentials_mtime=1000.0,
    )


@pytest.mark.asyncio
async def test_probe_200_yields_ok_with_usage(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(API_URL).respond(200, json=_ok_body())
    result = await probe_profile(_profile())
    assert result.health is Health.OK
    assert result.error is None
    assert result.usage is not None
    assert result.usage.session_pct == 9
    assert result.usage.weekly_pct == 6


@pytest.mark.asyncio
async def test_probe_200_session_exhausted(respx_mock: respx.MockRouter) -> None:
    body = json.loads((USAGE_FIXTURES / "session-exhausted.json").read_text())
    respx_mock.get(API_URL).respond(200, json=body)
    result = await probe_profile(_profile())
    assert result.health is Health.SESSION_LIMIT
    assert result.usage is not None
    assert result.usage.session_pct == 100


@pytest.mark.asyncio
async def test_probe_200_weekly_exhausted(respx_mock: respx.MockRouter) -> None:
    body = json.loads((USAGE_FIXTURES / "weekly-exhausted.json").read_text())
    respx_mock.get(API_URL).respond(200, json=body)
    result = await probe_profile(_profile())
    assert result.health is Health.WEEKLY_LIMIT
    assert result.usage is not None
    assert result.usage.weekly_pct == 100


@pytest.mark.asyncio
async def test_probe_401_yields_auth_dead(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(API_URL).respond(
        401,
        json={"error": {"type": "authentication_error", "message": "bad token"}},
    )
    result = await probe_profile(_profile())
    assert result.health is Health.AUTH_DEAD
    assert result.error is not None
    assert result.error.type == "authentication_error"


@pytest.mark.asyncio
async def test_probe_403_scope_missing_is_ok(respx_mock: respx.MockRouter) -> None:
    body = json.loads((USAGE_FIXTURES / "403-scope-missing.json").read_text())
    respx_mock.get(API_URL).respond(403, json=body)
    result = await probe_profile(_profile())
    assert result.health is Health.OK
    assert result.usage is None


@pytest.mark.asyncio
async def test_probe_timeout_yields_network_error(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(API_URL).mock(side_effect=httpx.TimeoutException("slow"))
    result = await probe_profile(_profile(), timeout=0.1)
    assert result.health is Health.NETWORK_ERROR
    assert result.error is not None
    assert result.error.type == "timeout"


@pytest.mark.asyncio
async def test_probe_connect_error(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(API_URL).mock(side_effect=httpx.ConnectError("refused"))
    result = await probe_profile(_profile())
    assert result.health is Health.NETWORK_ERROR
    assert result.error is not None
    assert result.error.type == "refused"


@pytest.mark.asyncio
async def test_probe_many_parallel(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(API_URL).respond(200, json=_ok_body())
    profiles = [_profile("a"), _profile("b"), _profile("c")]
    results = await probe_many(profiles)
    assert [r.name for r in results] == ["a", "b", "c"]
    assert all(r.health is Health.OK for r in results)


@pytest.mark.asyncio
async def test_probe_body_not_json_is_unknown(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(API_URL).respond(500, text="html error page")
    result = await probe_profile(_profile())
    assert result.health is Health.UNKNOWN


@pytest.mark.asyncio
async def test_probe_sends_required_headers(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(API_URL).respond(200, json=_ok_body())
    await probe_profile(_profile())
    assert route.called
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer sk-ant-oat01-test"
    assert request.headers["anthropic-version"] == ANTHROPIC_VERSION
    assert request.headers["anthropic-beta"] == ANTHROPIC_BETA


def test_api_url_is_oauth_usage_endpoint() -> None:
    assert API_URL == "https://api.anthropic.com/api/oauth/usage"


# ---------------------------------------------------------------------------
# _classify_exception — every httpx error class maps to the right kind
# ---------------------------------------------------------------------------


def test_classify_exception_timeout() -> None:
    from claude_lb.probe import _classify_exception

    probe = _classify_exception(httpx.ReadTimeout("read timeout after 10s"))
    assert probe.exception_kind == "timeout"
    assert "read timeout" in probe.exception_message.lower()


def test_classify_exception_connect_error() -> None:
    from claude_lb.probe import _classify_exception

    probe = _classify_exception(httpx.ConnectError("refused"))
    assert probe.exception_kind == "refused"
    assert probe.exception_message == "refused"


def test_classify_exception_generic_network_error() -> None:
    from claude_lb.probe import _classify_exception

    # NetworkError parent class — covers DNS, transport, etc.
    probe = _classify_exception(httpx.NetworkError("dns dead"))
    assert probe.exception_kind == "network"


def test_classify_exception_unknown_kind_falls_back_to_other() -> None:
    from claude_lb.probe import _classify_exception

    # An httpx.HTTPError subclass that's none of the recognised kinds.
    probe = _classify_exception(httpx.InvalidURL("bad URL"))
    assert probe.exception_kind == "other"


def test_classify_exception_empty_message_uses_class_name() -> None:
    """An exception with no message should still produce a useful
    exception_message (the class name) instead of the empty string."""
    from claude_lb.probe import _classify_exception

    probe = _classify_exception(httpx.ConnectError(""))
    assert probe.exception_message == "ConnectError"


# ---------------------------------------------------------------------------
# probe_many error paths — request actually times out / connection refused
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_profile_with_timeout_classifies_network_error(
    respx_mock: respx.MockRouter,
) -> None:
    """End-to-end: respx raises a timeout, classifier maps to NETWORK_ERROR."""
    respx_mock.get(API_URL).mock(side_effect=httpx.ReadTimeout("timeout"))
    result = await probe_profile(_profile())
    assert result.health is Health.NETWORK_ERROR


@pytest.mark.asyncio
async def test_probe_profile_with_connect_error_classifies_network_error(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(API_URL).mock(side_effect=httpx.ConnectError("refused"))
    result = await probe_profile(_profile())
    assert result.health is Health.NETWORK_ERROR
    assert result.error is not None


@pytest.mark.asyncio
async def test_probe_many_empty_returns_empty_list() -> None:
    """Edge case: empty input should not even open an httpx client."""
    results = await probe_many([])
    assert results == []


# ---------------------------------------------------------------------------
# probe_raw_many — the diagnostic surface used by `probe --raw`
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_raw_many_returns_tuples_with_status_body_headers(
    respx_mock: respx.MockRouter,
) -> None:
    from claude_lb.probe import probe_raw_many

    body = _ok_body()
    respx_mock.get(API_URL).respond(200, json=body, headers={"x-test": "1"})
    results = await probe_raw_many([_profile("a"), _profile("b")])
    assert len(results) == 2
    name, status, returned_body, headers = results[0]
    assert name == "a"
    assert status == 200
    assert returned_body == body
    assert headers.get("x-test") == "1"


@pytest.mark.asyncio
async def test_probe_raw_many_empty_returns_empty_list() -> None:
    from claude_lb.probe import probe_raw_many

    assert await probe_raw_many([]) == []


def test_probe_raw_many_sync_smoke(respx_mock: respx.MockRouter) -> None:
    """The sync wrapper should round-trip through asyncio.run cleanly."""
    from claude_lb.probe import probe_raw_many_sync

    respx_mock.get(API_URL).respond(200, json=_ok_body())
    results = probe_raw_many_sync([_profile()])
    assert len(results) == 1
    assert results[0][1] == 200  # status code


# ---------------------------------------------------------------------------
# _local_auth_expired — short-circuits before network when token is past expiry
# ---------------------------------------------------------------------------


def _expired_profile(*, refresh_present: bool) -> Profile:
    from datetime import UTC, datetime, timedelta

    return Profile(
        name="expired-acct",
        access_token="oat-stale",
        credentials_path="/tmp/expired/.credentials.json",
        credentials_mtime=1000.0,
        access_token_expires_at=datetime.now(UTC) - timedelta(minutes=10),
        refresh_token_present=refresh_present,
    )


def test_local_auth_expired_with_refresh_token_suggests_refresh() -> None:
    from claude_lb.probe import _local_auth_expired

    result = _local_auth_expired(_expired_profile(refresh_present=True))
    assert result is not None
    assert result.health is Health.AUTH_EXPIRED
    assert result.error is not None
    assert "roost refresh" in result.error.message
    assert "claude login" not in result.error.message
    # No network was hit — latency is 0.
    assert result.probe_latency_ms == 0


def test_local_auth_expired_without_refresh_token_suggests_login() -> None:
    """Profile with no stored refresh token can't be healed by `refresh` —
    the message should point operators at `claude login --profile` instead."""
    from claude_lb.probe import _local_auth_expired

    result = _local_auth_expired(_expired_profile(refresh_present=False))
    assert result is not None
    assert result.health is Health.AUTH_EXPIRED
    assert "claude login --profile" in result.error.message
    assert "no refresh token" in result.error.message.lower()


def test_local_auth_expired_returns_none_when_token_still_valid() -> None:
    """A profile with a future expires_at should fall through to network probe."""
    from datetime import UTC, datetime, timedelta

    from claude_lb.probe import _local_auth_expired

    fresh = Profile(
        name="fresh",
        access_token="oat-fresh",
        credentials_path="/tmp/fresh/.credentials.json",
        credentials_mtime=1000.0,
        access_token_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    assert _local_auth_expired(fresh) is None


def test_local_auth_expired_returns_none_when_no_expires_at() -> None:
    """Profiles whose credentials shape doesn't expose expiresAt fall through."""
    from claude_lb.probe import _local_auth_expired

    no_expires = Profile(
        name="no-exp",
        access_token="oat-stub",
        credentials_path="/tmp/no-exp/.credentials.json",
        credentials_mtime=1000.0,
        access_token_expires_at=None,
    )
    assert _local_auth_expired(no_expires) is None


@pytest.mark.asyncio
async def test_probe_many_skips_network_for_already_expired_profiles(
    respx_mock: respx.MockRouter,
) -> None:
    """An already-expired profile should be classified locally without
    consuming a respx call. Mix it with a healthy profile and verify only
    the healthy one hits the mock."""
    route = respx_mock.get(API_URL).respond(200, json=_ok_body())
    expired = _expired_profile(refresh_present=True)
    fresh = _profile("fresh")
    results = await probe_many([expired, fresh])
    assert results[0].health is Health.AUTH_EXPIRED
    assert results[1].health is Health.OK
    assert route.call_count == 1  # only `fresh` hit the network


def test_probe_many_sync_wrapper(respx_mock: respx.MockRouter) -> None:
    """The sync wrapper should round-trip through asyncio.run cleanly."""
    from claude_lb.probe import probe_many_sync

    respx_mock.get(API_URL).respond(200, json=_ok_body())
    results = probe_many_sync([_profile("a"), _profile("b")])
    assert len(results) == 2
    assert all(r.health is Health.OK for r in results)


@pytest.mark.asyncio
async def test_probe_profile_uses_supplied_client(
    respx_mock: respx.MockRouter,
) -> None:
    """When a client is passed in, probe_profile should reuse it instead of
    opening a new one. The respx mock is global, so we can't directly observe
    the client identity — but we can confirm the path runs without error."""
    from claude_lb.probe import probe_profile

    respx_mock.get(API_URL).respond(200, json=_ok_body())
    async with httpx.AsyncClient() as client:
        result = await probe_profile(_profile(), client=client)
    assert result.health is Health.OK


@pytest.mark.asyncio
async def test_probe_profile_short_circuits_on_locally_expired(
    respx_mock: respx.MockRouter,
) -> None:
    """probe_profile should return the AUTH_EXPIRED record without hitting
    the network when the local expiresAt is past. Hits the early-return at
    line 164."""
    from claude_lb.probe import probe_profile

    route = respx_mock.get(API_URL).respond(200, json=_ok_body())
    expired = _expired_profile(refresh_present=True)
    result = await probe_profile(expired)
    assert result.health is Health.AUTH_EXPIRED
    assert route.call_count == 0  # network not touched


# ---------------------------------------------------------------------------
# Phase D — consecutive_failures lifecycle in probe_many
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_many_increments_failures_on_network_error(
    respx_mock: respx.MockRouter,
) -> None:
    """A NETWORK_ERROR outcome should bump prev_failures by 1."""
    profile = _profile("a")
    respx_mock.get(API_URL).mock(side_effect=httpx.ConnectError("boom"))

    results = await probe_many([profile], prev_failures={"a": 2})
    assert results[0].health is Health.NETWORK_ERROR
    assert results[0].consecutive_failures == 3


@pytest.mark.asyncio
async def test_probe_many_resets_failures_on_non_network_outcome(
    respx_mock: respx.MockRouter,
) -> None:
    """Any non-NETWORK_ERROR outcome should reset the counter to 0."""
    profile = _profile("a")
    respx_mock.get(API_URL).respond(200, json=_ok_body())

    results = await probe_many([profile], prev_failures={"a": 5})
    assert results[0].health is Health.OK
    assert results[0].consecutive_failures == 0


@pytest.mark.asyncio
async def test_probe_many_starts_at_one_when_no_prior_failures(
    respx_mock: respx.MockRouter,
) -> None:
    """A first-time NETWORK_ERROR with no prev_failures entry starts at 1."""
    profile = _profile("a")
    respx_mock.get(API_URL).mock(side_effect=httpx.ConnectError("boom"))

    results = await probe_many([profile])
    assert results[0].consecutive_failures == 1


@pytest.mark.asyncio
async def test_probe_many_failures_independent_per_profile(
    respx_mock: respx.MockRouter,
) -> None:
    """Each profile's counter advances independently of its peers'."""
    p1 = _profile("a")
    p2 = _profile("b")
    respx_mock.get(API_URL).mock(side_effect=httpx.ConnectError("boom"))

    results = await probe_many(
        [p1, p2],
        prev_failures={"a": 0, "b": 4},
    )
    by_name = {r.name: r for r in results}
    assert by_name["a"].consecutive_failures == 1
    assert by_name["b"].consecutive_failures == 5
