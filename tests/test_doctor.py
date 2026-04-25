"""Doctor tests — each check in isolation + end-to-end run."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_lb import doctor as doctor_mod
from claude_lb import paths as paths_mod


@pytest.fixture(autouse=True)
def _isolate(
    tmp_path: Path,
    credentials_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("CLAUDE_LB_PROFILES_DIR", str(credentials_dir))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(paths_mod, "config_dir", lambda: config)
    monkeypatch.setattr(paths_mod, "cache_path", lambda: config / "health.json")
    monkeypatch.setattr(paths_mod, "ensure_config_dir", lambda: config)

    from claude_lb import cache as cache_mod

    monkeypatch.setattr(cache_mod, "cache_path", lambda: config / "health.json")
    monkeypatch.setattr(cache_mod, "ensure_config_dir", lambda: config)
    monkeypatch.setattr(doctor_mod, "config_dir", lambda: config)
    monkeypatch.setattr(doctor_mod, "cache_path", lambda: config / "health.json")
    return config


def test_empty_report_flags_profiles_missing() -> None:
    report = doctor_mod.run_doctor(skip_network=True)
    assert not report.all_passed
    profiles_check = next(c for c in report.checks if c.name == "profiles_discoverable")
    assert profiles_check.passed is False


def test_all_checks_pass_with_healthy_state(profile_factory) -> None:
    profile_factory("account-a")
    profile_factory("account-b")
    report = doctor_mod.run_doctor(skip_network=True)
    assert report.all_passed
    names = {c.name for c in report.checks}
    assert {"config_dir_writable", "profiles_discoverable", "credentials_parseable", "cache_readable"} <= names


def test_report_to_dict_has_meta_counts(profile_factory) -> None:
    profile_factory("account-a")
    report = doctor_mod.run_doctor(skip_network=True)
    payload = doctor_mod.report_to_dict(report)
    assert payload["meta"]["total"] == len(report.checks)
    assert payload["meta"]["passed"] + payload["meta"]["failed"] == payload["meta"]["total"]
    assert payload["data"]["version"]
    assert isinstance(payload["data"]["checks"], list)


def test_skip_network_omits_reachability_check(profile_factory) -> None:
    profile_factory("account-a")
    report = doctor_mod.run_doctor(skip_network=True)
    names = [c.name for c in report.checks]
    assert "anthropic_reachable" not in names


def test_subcommand_imports_check_passes_on_healthy_install(profile_factory) -> None:
    profile_factory("account-a")
    report = doctor_mod.run_doctor(skip_network=True)
    check = next(c for c in report.checks if c.name == "subcommand_imports")
    assert check.passed is True
    # At least the critical modules are verified
    assert any(m in check.detail for m in ("refresh", "modules load"))


def test_subcommand_imports_check_surfaces_failures(
    profile_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate a stale editable install by making one module's import fail."""
    import builtins

    profile_factory("account-a")
    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "claude_lb.refresh":
            raise ImportError("simulated stale install")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    report = doctor_mod.run_doctor(skip_network=True)
    check = next(c for c in report.checks if c.name == "subcommand_imports")
    assert check.passed is False
    assert "refresh" in check.detail
    assert "reinstall" in check.detail.lower()
    assert report.all_passed is False


def test_credentials_parseable_reports_token_source_distribution(profile_factory) -> None:
    profile_factory("account-a", shape="modern")
    profile_factory("legacy1", shape="legacy_oauth")
    profile_factory("plain1", shape="plain")
    report = doctor_mod.run_doctor(skip_network=True)
    check = next(c for c in report.checks if c.name == "credentials_parseable")
    assert check.passed
    # Exact detail includes token-source distribution
    assert "modern shape: 1" in check.detail
    assert "legacy shape: 2" in check.detail


# ---------------------------------------------------------------------------
# Refresh-token check (warning, not failure)
# ---------------------------------------------------------------------------


def test_refresh_tokens_check_passes_when_all_have_tokens(profile_factory) -> None:
    profile_factory("account-a")  # default modern shape includes refreshToken
    profile_factory("account-b")
    report = doctor_mod.run_doctor(skip_network=True)
    check = next(c for c in report.checks if c.name == "refresh_tokens_present")
    assert check.passed is True
    assert check.extra.get("warning") is None  # no warn marker
    assert "all 2" in check.detail.lower() or "2 profile" in check.detail.lower()


def test_refresh_tokens_check_warns_on_missing(
    profile_factory, credentials_dir: Path
) -> None:
    """Profiles without refreshToken should produce a WARN (passed=True + warning flag).
    Doesn't fail doctor — legacy/API-key profiles are valid setups."""
    import json as _json

    profile_factory("with_refresh")  # has refreshToken (modern shape)
    # Build a profile manually without refreshToken
    no_rt_dir = credentials_dir / "no_refresh"
    no_rt_dir.mkdir(parents=True, exist_ok=True)
    (no_rt_dir / ".credentials.json").write_text(_json.dumps({
        "claudeAiOauth": {
            "accessToken": "sk-ant-oat01-no-rt",
            "expiresAt": 99999999999999,
        }
    }))

    report = doctor_mod.run_doctor(skip_network=True)
    check = next(c for c in report.checks if c.name == "refresh_tokens_present")
    assert check.passed is True  # WARN level — doesn't fail the doctor run
    assert check.extra.get("warning") is True
    assert "no_refresh" in check.detail
    assert "claude login" in check.detail
    assert check.extra["with_token"] == ["with_refresh"]
    assert check.extra["without_token"] == ["no_refresh"]


def test_refresh_tokens_check_skipped_with_no_profiles() -> None:
    report = doctor_mod.run_doctor(skip_network=True)
    check = next(c for c in report.checks if c.name == "refresh_tokens_present")
    assert check.passed is True
    assert "skipped" in check.detail.lower()


# ---------------------------------------------------------------------------
# Cache check — must detect corruption (load_cache silently swallows it)
# ---------------------------------------------------------------------------


def test_cache_readable_check_detects_malformed_json(
    profile_factory, _isolate: Path
) -> None:
    """REGRESSION: load_cache returns an empty cache on bad JSON without
    raising — to keep the rest of the tool working through cache corruption.
    But that meant the doctor's cache check could never report corruption,
    silently passing while load_cache logged a warning. Doctor should now
    surface the corruption directly."""
    profile_factory("account-a")
    # Corrupt the cache file
    cache_file = _isolate / "health.json"
    cache_file.write_text("{this is not valid json", encoding="utf-8")

    report = doctor_mod.run_doctor(skip_network=True)
    check = next(c for c in report.checks if c.name == "cache_readable")
    assert check.passed is False
    assert "malformed" in check.detail.lower()
    assert "delete" in check.detail.lower() or "invalidate" in check.detail.lower()


def test_cache_readable_check_passes_with_valid_cache(
    profile_factory, _isolate: Path
) -> None:
    profile_factory("account-a")
    cache_file = _isolate / "health.json"
    cache_file.write_text(
        '{"schema_version": 1, "updated_at": "2026-04-25T00:00:00Z", "profiles": {}}',
        encoding="utf-8",
    )
    report = doctor_mod.run_doctor(skip_network=True)
    check = next(c for c in report.checks if c.name == "cache_readable")
    assert check.passed is True


def test_cache_readable_check_passes_when_cache_absent(
    profile_factory,
) -> None:
    """Missing cache is fine — first probe will create it."""
    profile_factory("account-a")
    report = doctor_mod.run_doctor(skip_network=True)
    check = next(c for c in report.checks if c.name == "cache_readable")
    assert check.passed is True
    assert "no cache yet" in check.detail.lower()


# ---------------------------------------------------------------------------
# Reachability check — iterate all address-family candidates
# ---------------------------------------------------------------------------


def test_reachability_iterates_addrinfo_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: was only trying infos[0]; IPv6-only boxes with v4 in [0]
    would fail spuriously. Verify we fall through to a later candidate."""
    import socket

    call_count = {"n": 0}

    fake_infos = [
        (socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("::1", 443, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 443)),
    ]

    def fake_getaddrinfo(*args: object, **kwargs: object) -> list:
        return fake_infos

    class FakeSocket:
        def __init__(self, family: int, socktype: int, proto: int) -> None:
            self.family = family

        def settimeout(self, s: float) -> None:
            pass

        def connect(self, addr: object) -> None:
            call_count["n"] += 1
            # First candidate fails, second succeeds
            if self.family == socket.AF_INET6:
                raise OSError("v6 not routable")
            return None

        def close(self) -> None:
            pass

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(socket, "socket", FakeSocket)

    result = doctor_mod._check_anthropic_reachable(timeout_s=1.0)
    assert result.passed is True
    assert call_count["n"] == 2  # Tried v6, then v4


def test_reachability_fails_when_all_candidates_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    fake_infos = [
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 443)),
    ]

    def fake_getaddrinfo(*args: object, **kwargs: object) -> list:
        return fake_infos

    class FakeSocket:
        def __init__(self, family: int, socktype: int, proto: int) -> None:
            pass

        def settimeout(self, s: float) -> None:
            pass

        def connect(self, addr: object) -> None:
            raise OSError("refused")

        def close(self) -> None:
            pass

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(socket, "socket", FakeSocket)

    result = doctor_mod._check_anthropic_reachable(timeout_s=1.0)
    assert result.passed is False
    assert "TCP connect" in result.detail


def test_reachability_dns_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def fake_getaddrinfo(*args: object, **kwargs: object) -> list:
        raise OSError("dns dead")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    result = doctor_mod._check_anthropic_reachable(timeout_s=1.0)
    assert result.passed is False
    assert "DNS" in result.detail


def test_reachability_empty_addrinfo(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [])

    result = doctor_mod._check_anthropic_reachable(timeout_s=1.0)
    assert result.passed is False
    assert "No A/AAAA" in result.detail


# ---------------------------------------------------------------------------
# status.claude.com check — informational, never fails the doctor run
# ---------------------------------------------------------------------------


import httpx
import respx


def test_status_page_clean_passes_without_warning(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(doctor_mod.STATUS_PAGE_URL).respond(
        200,
        json={
            "page": {"name": "Claude"},
            "status": {"indicator": "none", "description": "All Systems Operational"},
            "incidents": [],
            "components": [
                {"name": "Claude API", "status": "operational"},
            ],
        },
    )
    result = doctor_mod._check_claude_status_page(timeout_s=1.0)
    assert result.passed is True
    assert result.extra.get("warning") is None  # clean — no warn marker
    assert "All Systems Operational" in result.detail
    assert "no active incidents" in result.detail


def test_status_page_active_incident_warns_but_passes(respx_mock: respx.MockRouter) -> None:
    """Indicator can be 'none' while there's an active monitoring-state
    incident — surface the incident without failing doctor."""
    respx_mock.get(doctor_mod.STATUS_PAGE_URL).respond(
        200,
        json={
            "status": {"indicator": "none", "description": "All Systems Operational"},
            "incidents": [
                {
                    "name": "Elevated errors on Claude Opus 4.7",
                    "status": "monitoring",
                    "impact": "minor",
                    "shortlink": "https://stspg.io/abc",
                },
            ],
            "components": [],
        },
    )
    result = doctor_mod._check_claude_status_page(timeout_s=1.0)
    assert result.passed is True
    assert result.extra["warning"] is True
    assert "Elevated errors on Claude Opus 4.7" in result.detail
    assert "monitoring" in result.detail
    assert "minor" in result.detail
    assert result.extra["active_incidents"][0]["name"] == "Elevated errors on Claude Opus 4.7"


def test_status_page_skips_resolved_incidents(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(doctor_mod.STATUS_PAGE_URL).respond(
        200,
        json={
            "status": {"indicator": "none", "description": "All Systems Operational"},
            "incidents": [
                {"name": "Old issue", "status": "resolved", "impact": "minor"},
            ],
            "components": [],
        },
    )
    result = doctor_mod._check_claude_status_page(timeout_s=1.0)
    assert result.passed is True
    assert result.extra.get("warning") is None
    assert "no active incidents" in result.detail


def test_status_page_indicator_major_warns(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(doctor_mod.STATUS_PAGE_URL).respond(
        200,
        json={
            "status": {"indicator": "major", "description": "Major Outage"},
            "incidents": [
                {"name": "API down", "status": "investigating", "impact": "major"},
            ],
            "components": [
                {"name": "Claude API", "status": "major_outage"},
            ],
        },
    )
    result = doctor_mod._check_claude_status_page(timeout_s=1.0)
    assert result.passed is True  # informational, never fails
    assert result.extra["warning"] is True
    assert result.extra["indicator"] == "major"
    assert "Major Outage" in result.detail
    assert "API down" in result.detail
    assert "Claude API" in result.detail


def test_status_page_multiple_incidents_summarises(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(doctor_mod.STATUS_PAGE_URL).respond(
        200,
        json={
            "status": {"indicator": "minor", "description": "Partial Outage"},
            "incidents": [
                {"name": "First", "status": "investigating", "impact": "minor"},
                {"name": "Second", "status": "monitoring", "impact": "minor"},
                {"name": "Third", "status": "identified", "impact": "minor"},
            ],
            "components": [],
        },
    )
    result = doctor_mod._check_claude_status_page(timeout_s=1.0)
    assert "First" in result.detail  # leading incident shown by name
    assert "+2 more" in result.detail
    assert len(result.extra["active_incidents"]) == 3


def test_status_page_unreachable_warns_does_not_fail(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(doctor_mod.STATUS_PAGE_URL).mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    result = doctor_mod._check_claude_status_page(timeout_s=1.0)
    assert result.passed is True  # don't fail doctor on Statuspage flakiness
    assert result.extra["warning"] is True
    assert "Couldn't reach" in result.detail
    assert "connection refused" in result.detail


def test_status_page_malformed_json_warns(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(doctor_mod.STATUS_PAGE_URL).respond(
        200, content=b"<html>maintenance</html>"
    )
    result = doctor_mod._check_claude_status_page(timeout_s=1.0)
    assert result.passed is True
    assert result.extra["warning"] is True
    assert "Couldn't reach" in result.detail


def test_status_page_http_error_warns(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(doctor_mod.STATUS_PAGE_URL).respond(503, text="upstream dead")
    result = doctor_mod._check_claude_status_page(timeout_s=1.0)
    assert result.passed is True
    assert result.extra["warning"] is True


def test_status_page_included_in_run_doctor_when_network_enabled(
    profile_factory, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wire-up test: status check runs alongside reachability, only with network."""
    profile_factory("account-a")
    # Stub the TCP reachability check so the test doesn't hit real DNS.
    monkeypatch.setattr(
        doctor_mod, "_check_anthropic_reachable",
        lambda: doctor_mod.CheckResult(name="anthropic_reachable", passed=True, detail="stub"),
    )
    respx_mock.get(doctor_mod.STATUS_PAGE_URL).respond(
        200,
        json={
            "status": {"indicator": "none", "description": "All Systems Operational"},
            "incidents": [],
            "components": [],
        },
    )
    report = doctor_mod.run_doctor(skip_network=False)
    names = [c.name for c in report.checks]
    assert "claude_status_page" in names
    assert "anthropic_reachable" in names

    # Skip-network omits both
    report_offline = doctor_mod.run_doctor(skip_network=True)
    offline_names = [c.name for c in report_offline.checks]
    assert "claude_status_page" not in offline_names
    assert "anthropic_reachable" not in offline_names
