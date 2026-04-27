"""Platform-aware paths for cache, pick log, and profile discovery."""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_config_dir

APP_NAME = "claude-lb"


def config_dir() -> Path:
    """Return ~/.config/claude-lb (Linux/macOS) or %APPDATA%/claude-lb (Windows).

    Respects $XDG_CONFIG_HOME on Linux/macOS via platformdirs.
    """
    return Path(user_config_dir(APP_NAME, appauthor=False, roaming=True))


def cache_path() -> Path:
    """Full path to health.json cache file."""
    return config_dir() / "health.json"


def pick_log_path() -> Path:
    """Full path to picks.log (tab-separated audit trail)."""
    return config_dir() / "picks.log"


def last_pick_path() -> Path:
    """Full path to last-pick.json (stickiness state)."""
    return config_dir() / "last-pick.json"


def usage_log_path() -> Path:
    """Full path to usage-log.ndjson (per-probe usage trail; opt-in).

    Append-only, no rotation. NDJSON one-record-per-line so it streams
    cleanly without loading the whole file. Disabled by default — see
    usage_log_marker_path() for the opt-in mechanism.
    """
    return config_dir() / "usage-log.ndjson"


def usage_log_marker_path() -> Path:
    """Full path to usage-log.enabled (opt-in marker file).

    Existence of this file (or the CLAUDE_LB_USAGE_LOG=1 env var) enables
    the per-probe usage logger. The marker file is preferred over an env var
    for daemon contexts where preserving env across restarts is fragile.
    """
    return config_dir() / "usage-log.enabled"


def profiles_dir() -> Path:
    """Directory to walk for profile discovery.

    Resolution order:
      1. $CLAUDE_LB_PROFILES_DIR — explicit override (absolute path)
      2. ~/.claude-profiles/     — default canonical path
    """
    override = os.environ.get("CLAUDE_LB_PROFILES_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude-profiles"


def single_profile_fallback() -> Path | None:
    """If $CLAUDE_CONFIG_DIR points at a dir with a direct .credentials.json,
    return that dir. Otherwise return None.
    """
    val = os.environ.get("CLAUDE_CONFIG_DIR")
    if not val:
        return None
    p = Path(val).expanduser()
    if (p / ".credentials.json").is_file():
        return p
    return None


def ensure_config_dir() -> Path:
    """Create the config dir if missing; return it."""
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d
