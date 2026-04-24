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
