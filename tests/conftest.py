"""Shared pytest fixtures for claude-lb tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_dir() -> Path:
    return FIXTURES


@pytest.fixture
def credentials_dir(tmp_path: Path) -> Path:
    """Create an empty profiles directory and return its path."""
    d = tmp_path / "claude-profiles"
    d.mkdir()
    return d


def make_profile(
    credentials_root: Path,
    name: str,
    access_token: str = "sk-ant-oat01-testtoken",
    shape: str = "modern",
) -> Path:
    """Create a synthetic profile directory with a .credentials.json file."""
    profile_dir = credentials_root / name
    profile_dir.mkdir(parents=True, exist_ok=True)
    cred_path = profile_dir / ".credentials.json"

    payload: dict[str, Any]
    if shape == "modern":
        payload = {
            "claudeAiOauth": {
                "accessToken": access_token,
                "refreshToken": "sk-ant-ort01-test",
                "expiresAt": 99999999999999,
                "subscriptionType": "max",
            }
        }
    elif shape == "legacy_oauth":
        payload = {"oauthAccessToken": access_token}
    elif shape == "plain":
        payload = {"accessToken": access_token}
    elif shape == "broken":
        payload = {"not_a_token": "nothing"}
    elif shape == "notdict":
        cred_path.write_text(json.dumps([1, 2, 3]))
        return cred_path
    elif shape == "invalid_json":
        cred_path.write_text("{not json")
        return cred_path
    else:
        raise ValueError(f"Unknown shape: {shape}")

    cred_path.write_text(json.dumps(payload))
    return cred_path


@pytest.fixture
def profile_factory(credentials_dir: Path):
    """Factory fixture: make profiles under the isolated credentials_dir."""

    def _make(name: str, **kwargs: Any) -> Path:
        return make_profile(credentials_dir, name, **kwargs)

    return _make
