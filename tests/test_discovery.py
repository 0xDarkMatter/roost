"""Discovery tests — walk synthetic credentials dirs, try all token shapes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_lb import discovery


@pytest.fixture(autouse=True)
def _redirect_profiles_dir(monkeypatch: pytest.MonkeyPatch, credentials_dir: Path) -> None:
    monkeypatch.setenv("CLAUDE_LB_PROFILES_DIR", str(credentials_dir))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


def test_empty_dir_yields_no_profiles(credentials_dir: Path) -> None:
    assert discovery.discover_profiles() == []


def test_modern_shape_is_discovered(profile_factory) -> None:
    profile_factory("account-a", shape="modern", access_token="sk-ant-oat01-modern")
    profiles = discovery.discover_profiles()
    assert [p.name for p in profiles] == ["account-a"]
    assert profiles[0].access_token == "sk-ant-oat01-modern"
    assert profiles[0].token_source == "claudeAiOauth.accessToken"


def test_legacy_oauth_shape_is_discovered(profile_factory) -> None:
    profile_factory("account-b", shape="legacy_oauth", access_token="legacy-t")
    profiles = discovery.discover_profiles()
    assert profiles[0].access_token == "legacy-t"
    assert profiles[0].token_source == "oauthAccessToken"


def test_plain_shape_is_discovered(profile_factory) -> None:
    profile_factory("account-c", shape="plain", access_token="plain-t")
    profiles = discovery.discover_profiles()
    assert profiles[0].token_source == "accessToken"


def test_broken_shape_is_skipped(profile_factory) -> None:
    profile_factory("broken", shape="broken")
    assert discovery.discover_profiles() == []


def test_invalid_json_is_skipped(profile_factory) -> None:
    profile_factory("bad", shape="invalid_json")
    assert discovery.discover_profiles() == []


def test_non_dict_root_is_skipped(profile_factory) -> None:
    profile_factory("arr", shape="notdict")
    assert discovery.discover_profiles() == []


def test_dir_without_credentials_file_is_skipped(credentials_dir: Path) -> None:
    (credentials_dir / "empty-dir").mkdir()
    assert discovery.discover_profiles() == []


def test_non_matching_name_is_skipped(credentials_dir: Path) -> None:
    bad = credentials_dir / "has.dots"
    bad.mkdir()
    (bad / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "x"}})
    )
    assert discovery.discover_profiles() == []


def test_multiple_profiles_sorted(profile_factory) -> None:
    profile_factory("zeta")
    profile_factory("alpha")
    profile_factory("beta")
    names = [p.name for p in discovery.discover_profiles()]
    assert names == ["alpha", "beta", "zeta"]


def test_get_profile_by_name(profile_factory) -> None:
    profile_factory("account-a")
    assert discovery.get_profile("account-a") is not None
    assert discovery.get_profile("nonexistent") is None


def test_single_profile_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Primary dir empty, but CLAUDE_CONFIG_DIR has credentials directly.
    primary = tmp_path / "primary-empty"
    primary.mkdir()
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    (fallback / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "fallback-t"}})
    )
    monkeypatch.setenv("CLAUDE_LB_PROFILES_DIR", str(primary))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(fallback))

    profiles = discovery.discover_profiles()
    assert len(profiles) == 1
    assert profiles[0].name == "default"
    assert profiles[0].access_token == "fallback-t"


def test_discover_names_returns_strings(profile_factory) -> None:
    profile_factory("account-a")
    profile_factory("account-b")
    assert discovery.discover_names() == ["account-b", "account-a"]


def test_credentials_mtime_is_captured(profile_factory) -> None:
    cred = profile_factory("account-a")
    profile = discovery.discover_profiles()[0]
    assert profile.credentials_mtime == cred.stat().st_mtime


def test_subscription_type_extracted_from_modern_shape(profile_factory) -> None:
    """Modern credentials include subscriptionType; surface it on Profile."""
    profile_factory("account-a", shape="modern")
    profile = discovery.discover_profiles()[0]
    assert profile.subscription_type == "max"


def test_subscription_type_absent_when_legacy_shape(profile_factory) -> None:
    """Legacy shapes don't carry plan info; subscription_type stays None."""
    profile_factory("old", shape="legacy_oauth")
    profile = discovery.discover_profiles()[0]
    assert profile.subscription_type is None


def test_subscription_type_normalised_to_lowercase(
    credentials_dir: Path,
) -> None:
    """Defensive: if Anthropic ever returns 'Max' or 'TEAM', we lowercase it."""
    import json as _json

    name = "enterprise1"
    profile_dir = credentials_dir / name
    profile_dir.mkdir()
    (profile_dir / ".credentials.json").write_text(
        _json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "t",
                    "subscriptionType": "TEAM",
                }
            }
        )
    )
    profile = discovery.get_profile(name)
    assert profile is not None
    assert profile.subscription_type == "team"
