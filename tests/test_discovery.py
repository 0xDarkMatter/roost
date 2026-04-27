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
    assert discovery.discover_names() == ["account-a", "account-b"]


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


# ---------------------------------------------------------------------------
# remove_profile_dir / rename_profile_dir (Phase A)
# ---------------------------------------------------------------------------


def test_remove_profile_dir_happy_path(profile_factory, credentials_dir: Path) -> None:
    profile_factory("account-a")
    target = credentials_dir / "account-a"
    assert target.is_dir()

    result = discovery.remove_profile_dir("account-a")

    assert result.ok is True
    assert result.name == "account-a"
    assert result.path == target
    assert not target.exists()


def test_remove_profile_dir_unknown_returns_not_found(credentials_dir: Path) -> None:
    result = discovery.remove_profile_dir("never-existed")
    assert result.ok is False
    assert result.error_code == "NOT_FOUND"
    assert result.error_message and "no such profile directory" in result.error_message


def test_remove_profile_dir_rejects_invalid_name() -> None:
    result = discovery.remove_profile_dir("../escape")
    assert result.ok is False
    assert result.error_code == "VALIDATION_ERROR"


def test_remove_profile_dir_refuses_when_path_is_file(credentials_dir: Path) -> None:
    """Defensive: rmtree on a regular file would raise; we want a clean error."""
    bogus = credentials_dir / "bogus"
    bogus.write_text("not a dir")
    result = discovery.remove_profile_dir("bogus")
    assert result.ok is False
    assert result.error_code == "VALIDATION_ERROR"
    assert bogus.exists()  # untouched


def test_remove_profile_dir_recursively_drops_extra_files(
    profile_factory, credentials_dir: Path
) -> None:
    """A profile dir may contain a lock file or user-added cruft; remove takes
    everything."""
    profile_factory("account-a")
    extra = credentials_dir / "account-a" / "extra.txt"
    extra.write_text("scratch")
    lock = credentials_dir / "account-a" / ".credentials.json.lock"
    lock.write_text("")

    result = discovery.remove_profile_dir("account-a")

    assert result.ok is True
    assert not (credentials_dir / "account-a").exists()


def test_rename_profile_dir_happy_path(profile_factory, credentials_dir: Path) -> None:
    profile_factory("old-name")
    result = discovery.rename_profile_dir("old-name", "new-name")
    assert result.ok is True
    assert result.name == "new-name"
    assert not (credentials_dir / "old-name").exists()
    assert (credentials_dir / "new-name" / ".credentials.json").is_file()


def test_rename_profile_dir_missing_source_returns_not_found(
    credentials_dir: Path,
) -> None:
    result = discovery.rename_profile_dir("nope", "newname")
    assert result.ok is False
    assert result.error_code == "NOT_FOUND"


def test_rename_profile_dir_destination_exists_without_force(
    profile_factory, credentials_dir: Path
) -> None:
    profile_factory("a")
    profile_factory("b")
    result = discovery.rename_profile_dir("a", "b")
    assert result.ok is False
    assert result.error_code == "CONFLICT"
    # Both still present.
    assert (credentials_dir / "a").is_dir()
    assert (credentials_dir / "b").is_dir()


def test_rename_profile_dir_destination_exists_with_force(
    profile_factory, credentials_dir: Path
) -> None:
    profile_factory("src", access_token="from-src")
    profile_factory("dst", access_token="from-dst")
    result = discovery.rename_profile_dir("src", "dst", force=True)
    assert result.ok is True
    # dst now contains src's credentials.
    payload = json.loads(
        (credentials_dir / "dst" / ".credentials.json").read_text()
    )
    assert payload["claudeAiOauth"]["accessToken"] == "from-src"
    assert not (credentials_dir / "src").exists()


def test_rename_profile_dir_rejects_invalid_old_name() -> None:
    result = discovery.rename_profile_dir("bad/name", "valid")
    assert result.ok is False
    assert result.error_code == "VALIDATION_ERROR"
    assert result.error_message and "old name" in result.error_message


def test_rename_profile_dir_rejects_invalid_new_name(profile_factory) -> None:
    profile_factory("good")
    result = discovery.rename_profile_dir("good", "bad/name")
    assert result.ok is False
    assert result.error_code == "VALIDATION_ERROR"
    assert result.error_message and "new name" in result.error_message


def test_rename_profile_dir_rejects_identical_names(profile_factory) -> None:
    profile_factory("same")
    result = discovery.rename_profile_dir("same", "same")
    assert result.ok is False
    assert result.error_code == "VALIDATION_ERROR"
    assert result.error_message and "identical" in result.error_message
