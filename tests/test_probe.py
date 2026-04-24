"""Probe tests — respx mocks the Anthropic API; we verify classification end-to-end."""

from __future__ import annotations

import httpx
import pytest
import respx

from claude_lb.models import Health, Profile
from claude_lb.probe import API_URL, probe_many, probe_profile


def _profile(name: str = "account-a") -> Profile:
    return Profile(
        name=name,
        access_token="sk-ant-oat01-test",
        credentials_path=f"/tmp/{name}/.credentials.json",
        credentials_mtime=1000.0,
    )


@pytest.mark.asyncio
async def test_probe_200_yields_ok(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(API_URL).respond(200, json={"data": [], "has_more": False})
    result = await probe_profile(_profile())
    assert result.health is Health.OK
    assert result.error is None


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
async def test_probe_429_session(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(API_URL).respond(
        429,
        json={"error": {"type": "rate_limit_error", "message": "Session limit reached for current 5-hour window."}},
    )
    result = await probe_profile(_profile())
    assert result.health is Health.SESSION_LIMIT


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
    respx_mock.get(API_URL).respond(200, json={"data": []})
    profiles = [_profile("a"), _profile("b"), _profile("c")]
    results = await probe_many(profiles)
    assert [r.name for r in results] == ["a", "b", "c"]
    assert all(r.health is Health.OK for r in results)


@pytest.mark.asyncio
async def test_probe_body_not_json_is_unknown_like(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(API_URL).respond(500, text="html error page")
    result = await probe_profile(_profile())
    assert result.health is Health.UNKNOWN


@pytest.mark.asyncio
async def test_probe_sends_bearer_token(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(API_URL).respond(200, json={"data": []})
    await probe_profile(_profile())
    assert route.called
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer sk-ant-oat01-test"
    assert request.headers["anthropic-version"] == "2023-06-01"


@pytest.mark.asyncio
async def test_probe_many_with_empty_list_returns_empty() -> None:
    results = await probe_many([])
    assert results == []


def test_probe_many_sync_empty() -> None:
    """Synchronous wrapper must not crash on empty input."""
    from claude_lb.probe import probe_many_sync

    assert probe_many_sync([]) == []


@pytest.mark.asyncio
async def test_probe_records_credentials_mtime(respx_mock: respx.MockRouter) -> None:
    """Regression: probe results must carry the profile's mtime forward so
    cache.is_entry_fresh can detect credentials re-write."""
    respx_mock.get(API_URL).respond(200, json={"data": []})
    profile = _profile()
    result = await probe_profile(profile)
    assert result.credentials_mtime == profile.credentials_mtime


@pytest.mark.asyncio
async def test_probe_uses_supplied_client_without_opening_new_one(
    respx_mock: respx.MockRouter,
) -> None:
    """When an httpx.AsyncClient is supplied, probe_profile should use it
    instead of constructing its own."""
    respx_mock.get(API_URL).respond(200, json={"data": []})
    profile = _profile()
    async with httpx.AsyncClient() as client:
        result = await probe_profile(profile, client=client)
    assert result.health is Health.OK


@pytest.mark.asyncio
async def test_probe_http_not_json_is_unknown(respx_mock: respx.MockRouter) -> None:
    """5xx with HTML-ish body: status 500 → unknown, no crash on body parse."""
    respx_mock.get(API_URL).respond(500, text="<html>whoops</html>")
    result = await probe_profile(_profile())
    assert result.health is Health.UNKNOWN


@pytest.mark.asyncio
async def test_probe_429_with_body_as_list_falls_back_gracefully(
    respx_mock: respx.MockRouter,
) -> None:
    """Anthropic error bodies are usually objects, but if the server ever
    returns a JSON array the classifier should NOT crash — it should yield
    RATE_LIMITED (the type-extraction yields 'unknown', no keyword match)."""
    respx_mock.get(API_URL).respond(429, json=[])
    result = await probe_profile(_profile())
    assert result.health is Health.RATE_LIMITED
