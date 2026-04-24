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
