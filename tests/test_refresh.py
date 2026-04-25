"""Refresh tests — respx mocks /v1/oauth/token; verify in-place credential rewrite."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from claude_lb.models import Profile
from claude_lb.refresh import TOKEN_URL, refresh_many, refresh_profile


def _write_credentials(path: Path, *, access="old-access", refresh="old-refresh", expires_ms: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "claudeAiOauth": {
            "accessToken": access,
            "refreshToken": refresh,
            "expiresAt": expires_ms if expires_ms is not None else 1_700_000_000_000,
            "scopes": ["user:inference", "user:profile"],
            "subscriptionType": "max",
        },
        "someOtherField": "preserved",
    }
    path.write_text(json.dumps(payload))


def _profile(path: Path, *, name="account-a", refresh_present=True) -> Profile:
    return Profile(
        name=name,
        access_token="old-access",
        credentials_path=str(path),
        credentials_mtime=1000.0,
        access_token_expires_at=datetime.now(UTC) - timedelta(minutes=10),
        refresh_token_present=refresh_present,
    )


@pytest.mark.asyncio
async def test_refresh_rewrites_credentials_on_success(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    cred = tmp_path / "account-a" / ".credentials.json"
    _write_credentials(cred, access="old-A", refresh="old-R")
    respx_mock.post(TOKEN_URL).respond(
        200,
        json={
            "access_token": "new-A",
            "refresh_token": "new-R",
            "expires_in": 3600,
        },
    )
    result = await refresh_profile(_profile(cred))
    assert result.refreshed is True
    data = json.loads(cred.read_text())
    assert data["claudeAiOauth"]["accessToken"] == "new-A"
    assert data["claudeAiOauth"]["refreshToken"] == "new-R"
    assert data["someOtherField"] == "preserved"  # untouched
    assert result.new_expires_at is not None


@pytest.mark.asyncio
async def test_refresh_rejected_by_server_does_not_clobber_credentials(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    cred = tmp_path / "account-a" / ".credentials.json"
    _write_credentials(cred, access="old-A", refresh="old-R")
    respx_mock.post(TOKEN_URL).respond(
        401,
        json={"error": {"type": "invalid_grant", "message": "refresh token dead"}},
    )
    result = await refresh_profile(_profile(cred))
    assert result.refreshed is False
    assert result.error_code == "REFRESH_REJECTED"
    data = json.loads(cred.read_text())
    assert data["claudeAiOauth"]["accessToken"] == "old-A"  # unchanged


@pytest.mark.asyncio
async def test_refresh_network_error_classified(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    cred = tmp_path / "account-a" / ".credentials.json"
    _write_credentials(cred)
    respx_mock.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("refused"))
    result = await refresh_profile(_profile(cred))
    assert result.refreshed is False
    assert result.error_code == "NETWORK_ERROR"


@pytest.mark.asyncio
async def test_refresh_missing_refresh_token_fails_fast(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    cred = tmp_path / "account-a" / ".credentials.json"
    cred.parent.mkdir()
    cred.write_text(json.dumps({"claudeAiOauth": {"accessToken": "x"}}))
    result = await refresh_profile(_profile(cred, refresh_present=False))
    assert result.refreshed is False
    assert result.error_code == "NO_REFRESH_TOKEN"
    assert not respx_mock.calls.called  # no network attempted


@pytest.mark.asyncio
async def test_refresh_many_parallel(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    creds = []
    for n in ("a", "b", "c"):
        cred = tmp_path / n / ".credentials.json"
        _write_credentials(cred)
        creds.append(_profile(cred, name=n))
    respx_mock.post(TOKEN_URL).respond(
        200,
        json={"access_token": "new", "refresh_token": "new", "expires_in": 3600},
    )
    results = await refresh_many(creds)
    assert [r.name for r in results] == ["a", "b", "c"]
    assert all(r.refreshed for r in results)


@pytest.mark.asyncio
async def test_refresh_empty_list_returns_empty() -> None:
    results = await refresh_many([])
    assert results == []


@pytest.mark.asyncio
async def test_refresh_unreadable_credentials_file(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    """Credentials file vanished between discovery and refresh."""
    cred = tmp_path / "ghost" / ".credentials.json"
    # File intentionally not created
    result = await refresh_profile(_profile(cred, name="ghost"))
    assert result.refreshed is False
    assert result.error_code == "UNREADABLE"
    assert not respx_mock.calls.called  # fail fast before network


@pytest.mark.asyncio
async def test_refresh_500_is_unexpected_response(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    cred = tmp_path / "account-a" / ".credentials.json"
    _write_credentials(cred)
    respx_mock.post(TOKEN_URL).respond(500, text="<html>500</html>")
    result = await refresh_profile(_profile(cred))
    assert result.refreshed is False
    assert result.error_code == "UNEXPECTED_RESPONSE"
    # Credentials preserved
    data = json.loads(cred.read_text())
    assert data["claudeAiOauth"]["accessToken"] == "old-access"


@pytest.mark.asyncio
async def test_refresh_200_with_missing_access_token_is_not_success(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    """Regression target: server 200 but body omits `access_token`. Current
    code treats this as 'refreshed=True' because each field update is guarded
    individually with truthiness — nothing rotates but we claim success and
    overwrite the credentials file. Caller has no way to know the tokens
    didn't actually change.

    Fixed behaviour: missing access_token -> UNEXPECTED_RESPONSE, no rewrite."""
    cred = tmp_path / "account-a" / ".credentials.json"
    _write_credentials(cred, access="old-A", refresh="old-R")
    respx_mock.post(TOKEN_URL).respond(
        200,
        json={"expires_in": 3600},  # access_token + refresh_token both missing
    )
    result = await refresh_profile(_profile(cred))
    assert result.refreshed is False, (
        "Server 200 without access_token must not be treated as a successful refresh"
    )
    # Credentials must still hold the old tokens
    data = json.loads(cred.read_text())
    assert data["claudeAiOauth"]["accessToken"] == "old-A"
    assert data["claudeAiOauth"]["refreshToken"] == "old-R"


@pytest.mark.asyncio
async def test_refresh_lock_held_returns_lock_held_error(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    """If another process holds the credentials lock, we return LOCK_HELD
    without clobbering the in-flight refresh's tokens. Simulated by pre-
    acquiring the lock from this test process."""
    from filelock import FileLock

    cred = tmp_path / "account-a" / ".credentials.json"
    _write_credentials(cred)
    lock_path = str(cred) + ".lock"

    # Acquire the lock so refresh_profile can't.
    external_lock = FileLock(lock_path)
    external_lock.acquire(timeout=1)
    try:
        result = await refresh_profile(
            _profile(cred),
            lock_timeout=0.2,  # short so the test isn't slow
        )
    finally:
        external_lock.release()

    assert result.refreshed is False
    assert result.error_code == "LOCK_HELD"
    assert not respx_mock.calls.called  # never made the HTTP POST
    # Credentials unchanged
    data = json.loads(cred.read_text())
    assert data["claudeAiOauth"]["accessToken"] == "old-access"


@pytest.mark.asyncio
async def test_refresh_releases_lock_on_success(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    """Lock must be released after a successful refresh so subsequent
    refreshes of the same profile don't deadlock."""
    from filelock import FileLock

    cred = tmp_path / "account-a" / ".credentials.json"
    _write_credentials(cred)
    respx_mock.post(TOKEN_URL).respond(
        200,
        json={"access_token": "new-A", "refresh_token": "new-R", "expires_in": 3600},
    )
    first = await refresh_profile(_profile(cred))
    assert first.refreshed is True

    # Should be acquirable by an external waiter immediately.
    lock = FileLock(str(cred) + ".lock")
    lock.acquire(timeout=0.5)
    lock.release()


@pytest.mark.asyncio
async def test_refresh_releases_lock_on_rejection(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    """Lock must be released even when the server rejects the refresh token."""
    from filelock import FileLock

    cred = tmp_path / "account-a" / ".credentials.json"
    _write_credentials(cred)
    respx_mock.post(TOKEN_URL).respond(
        401, json={"error": {"type": "invalid_grant", "message": "dead"}}
    )
    result = await refresh_profile(_profile(cred))
    assert result.refreshed is False
    assert result.error_code == "REFRESH_REJECTED"
    # Lock must be acquirable immediately
    lock = FileLock(str(cred) + ".lock")
    lock.acquire(timeout=0.5)
    lock.release()


@pytest.mark.asyncio
async def test_refresh_missing_filelock_dependency(
    tmp_path: Path,
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale editable install: pyproject declares filelock but the tool venv
    lacks it. refresh_profile must return MISSING_DEPENDENCY cleanly instead
    of bubbling a ModuleNotFoundError traceback.

    Simulates the failure by hiding `filelock` from the import machinery.
    """
    import builtins

    cred = tmp_path / "account-a" / ".credentials.json"
    _write_credentials(cred)

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "filelock":
            raise ImportError("No module named 'filelock'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    result = await refresh_profile(_profile(cred))
    assert result.refreshed is False
    assert result.error_code == "MISSING_DEPENDENCY"
    assert "filelock" in (result.error_message or "")
    assert "reinstall" in (result.error_message or "").lower()
    # Credentials must be left untouched (we never got to the write step).
    data = json.loads(cred.read_text())
    assert data["claudeAiOauth"]["accessToken"] == "old-access"
    # No network attempted.
    assert not respx_mock.calls.called


@pytest.mark.asyncio
async def test_refresh_sends_client_id_and_beta_header(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    cred = tmp_path / "account-a" / ".credentials.json"
    _write_credentials(cred, refresh="the-rt")
    route = respx_mock.post(TOKEN_URL).respond(
        200,
        json={"access_token": "A", "refresh_token": "R", "expires_in": 3600},
    )
    await refresh_profile(_profile(cred))
    assert route.called
    req = route.calls.last.request
    body = json.loads(req.content.decode())
    assert body["grant_type"] == "refresh_token"
    assert body["refresh_token"] == "the-rt"
    assert body["client_id"] == "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
    assert req.headers.get("anthropic-beta") == "oauth-2025-04-20"


# ---------------------------------------------------------------------------
# Pure helpers — _extract_refresh_token + _apply_token_response edge cases
# ---------------------------------------------------------------------------


def test_extract_refresh_token_returns_none_when_oauth_block_missing() -> None:
    from claude_lb.refresh import _extract_refresh_token

    assert _extract_refresh_token({}) is None
    assert _extract_refresh_token({"otherField": 1}) is None


def test_extract_refresh_token_returns_none_when_oauth_is_not_dict() -> None:
    from claude_lb.refresh import _extract_refresh_token

    assert _extract_refresh_token({"claudeAiOauth": "not-a-dict"}) is None
    assert _extract_refresh_token({"claudeAiOauth": [1, 2, 3]}) is None


def test_extract_refresh_token_strips_whitespace_and_rejects_empty() -> None:
    from claude_lb.refresh import _extract_refresh_token

    assert _extract_refresh_token({"claudeAiOauth": {"refreshToken": "   "}}) is None
    assert _extract_refresh_token({"claudeAiOauth": {"refreshToken": "  the-rt  "}}) == "the-rt"


def test_extract_refresh_token_returns_none_when_value_is_not_string() -> None:
    from claude_lb.refresh import _extract_refresh_token

    assert _extract_refresh_token({"claudeAiOauth": {"refreshToken": 12345}}) is None


def test_apply_token_response_creates_oauth_block_when_missing() -> None:
    """If the credentials shape lacks claudeAiOauth, _apply_token_response
    should create it rather than crash — degenerate but not impossible state."""
    from claude_lb.refresh import _apply_token_response

    payload = {"otherField": "preserved"}
    new_payload, new_expires = _apply_token_response(
        payload,
        {"access_token": "new-A", "refresh_token": "new-R", "expires_in": 3600},
    )
    assert new_payload["otherField"] == "preserved"
    assert new_payload["claudeAiOauth"]["accessToken"] == "new-A"
    assert new_payload["claudeAiOauth"]["refreshToken"] == "new-R"
    assert new_expires is not None


def test_apply_token_response_skips_fields_that_are_wrong_type() -> None:
    """A token response with non-string access_token / refresh_token must not
    poison the credentials — leave the existing values in place."""
    from claude_lb.refresh import _apply_token_response

    payload = {"claudeAiOauth": {"accessToken": "old-A", "refreshToken": "old-R"}}
    new_payload, _ = _apply_token_response(
        payload,
        {"access_token": None, "refresh_token": 0, "expires_in": "not-a-number"},
    )
    assert new_payload["claudeAiOauth"]["accessToken"] == "old-A"
    assert new_payload["claudeAiOauth"]["refreshToken"] == "old-R"


def test_apply_token_response_handles_omitted_refresh_token() -> None:
    """A token response with access_token but no refresh_token should rotate
    just the access token — Anthropic doesn't always return a new refresh."""
    from claude_lb.refresh import _apply_token_response

    payload = {"claudeAiOauth": {"accessToken": "old-A", "refreshToken": "keep-R"}}
    new_payload, _ = _apply_token_response(
        payload,
        {"access_token": "new-A", "expires_in": 3600},
    )
    assert new_payload["claudeAiOauth"]["accessToken"] == "new-A"
    assert new_payload["claudeAiOauth"]["refreshToken"] == "keep-R"


# ---------------------------------------------------------------------------
# _atomic_write_credentials — failure cleans up tempfile
# ---------------------------------------------------------------------------


def test_atomic_write_credentials_unlinks_tempfile_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the inner replace() fails (or json.dump raises), the tempfile must
    be cleaned up — otherwise stale .credentials-*.json.tmp files accumulate."""
    import os as _os

    from claude_lb.refresh import _atomic_write_credentials

    target = tmp_path / "x" / ".credentials.json"

    real_replace = _os.replace

    def boom_replace(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(_os, "replace", boom_replace)

    with pytest.raises(OSError):
        _atomic_write_credentials(target, {"k": "v"})

    monkeypatch.setattr(_os, "replace", real_replace)
    leftover = list((target.parent).glob(".credentials-*.json.tmp"))
    assert leftover == []  # tempfile was cleaned up


# ---------------------------------------------------------------------------
# refresh_many_sync — sync wrapper smoke
# ---------------------------------------------------------------------------


def test_refresh_many_sync_with_empty_input_returns_empty(
    respx_mock: respx.MockRouter,
) -> None:
    from claude_lb.refresh import refresh_many_sync

    assert refresh_many_sync([]) == []


# ---------------------------------------------------------------------------
# refresh_profile — error.message as string vs dict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_profile_handles_error_as_plain_string(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    """Some Anthropic 401 bodies have `error: "auth failed"` rather than
    `error: {message: ...}`. Both shapes must yield REFRESH_REJECTED with
    a useful message — not crash on .get()."""
    from claude_lb.refresh import refresh_profile

    cred = tmp_path / "p" / ".credentials.json"
    _write_credentials(cred, refresh="the-rt")
    respx_mock.post(TOKEN_URL).respond(401, json={"error": "auth failed"})
    result = await refresh_profile(_profile(cred), timeout=1.0)
    assert result.refreshed is False
    assert result.error_code == "REFRESH_REJECTED"
    assert "auth failed" in (result.error_message or "")


@pytest.mark.asyncio
async def test_refresh_profile_network_timeout_returns_network_error(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    from claude_lb.refresh import refresh_profile

    cred = tmp_path / "p" / ".credentials.json"
    _write_credentials(cred, refresh="the-rt")
    respx_mock.post(TOKEN_URL).mock(side_effect=httpx.ReadTimeout("timed out"))
    result = await refresh_profile(_profile(cred), timeout=1.0)
    assert result.refreshed is False
    assert result.error_code == "NETWORK_ERROR"


@pytest.mark.asyncio
async def test_refresh_profile_500_returns_unexpected_response(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    from claude_lb.refresh import refresh_profile

    cred = tmp_path / "p" / ".credentials.json"
    _write_credentials(cred, refresh="the-rt")
    respx_mock.post(TOKEN_URL).respond(500, text="upstream dead")
    result = await refresh_profile(_profile(cred), timeout=1.0)
    assert result.refreshed is False
    assert result.error_code == "UNEXPECTED_RESPONSE"
    assert "500" in (result.error_message or "")


@pytest.mark.asyncio
async def test_refresh_profile_write_failed_when_atomic_write_raises(
    tmp_path: Path, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If Anthropic returns a valid 200 but the local atomic-write fails
    (e.g. disk full), refresh should report WRITE_FAILED rather than
    pretending the refresh succeeded — otherwise the next call would think
    the new token is on disk when it isn't."""
    from claude_lb import refresh as refresh_mod
    from claude_lb.refresh import refresh_profile

    cred = tmp_path / "p" / ".credentials.json"
    _write_credentials(cred, refresh="the-rt")
    respx_mock.post(TOKEN_URL).respond(
        200,
        json={"access_token": "new-A", "refresh_token": "new-R", "expires_in": 3600},
    )

    def _boom_write(path, payload):
        raise OSError("simulated disk full")

    monkeypatch.setattr(refresh_mod, "_atomic_write_credentials", _boom_write)
    result = await refresh_profile(_profile(cred), timeout=1.0)
    assert result.refreshed is False
    assert result.error_code == "WRITE_FAILED"
    assert "disk full" in (result.error_message or "")
