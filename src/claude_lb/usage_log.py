"""Per-probe usage NDJSON log (opt-in observability).

Disabled by default for privacy/disk-footprint conservatism. Enable via:

  - `roost config usage-log on` (writes a marker file at
    `<config>/usage-log.enabled`), or
  - `CLAUDE_LB_USAGE_LOG=1` environment variable

When enabled, every successful probe appends one JSON object per line to
`<config>/usage-log.ndjson`. Records are best-effort: a write failure must
never crash the probe path. Format:

    {"ts": "...", "profile": "...", "health": "ok",
     "session_pct": 45, "weekly_pct": 62, "sonnet_pct": 70, "opus_pct": 30,
     "fable_pct": 12, "overage_pct": null, "spend_pct": null,
     "currency": "USD", "latency_ms": 187}

`roost report` reads this back. Users are responsible for rotation; the file
is plain text and `> usage-log.ndjson` truncates safely.

This log is append-only with no rotation and no migration: every line ever
written stays parseable forever, and `iter_records` is the only reader for
all of them at once. Any future change to the record shape MUST keep every
key added so far, because old lines on disk will never gain the new key
retroactively — `iter_records` has to keep reading `sonnet_pct`/`opus_pct`
lines from before `fable_pct`/`spend_pct` existed, and future readers will
have to keep reading these lines the same way. Never remove a key here.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import ProfileHealth
from .paths import usage_log_marker_path, usage_log_path

log = logging.getLogger(__name__)

ENV_VAR = "CLAUDE_LB_USAGE_LOG"


def is_enabled() -> bool:
    """True iff the env var is truthy OR the marker file exists.

    Checked once per probe — cheap (env lookup + stat) but called frequently
    enough that we keep both checks unconditional rather than caching.
    Disabled value semantics: env var must be `1`, `true`, `yes` (case-insensitive);
    the marker file just needs to exist (its contents are ignored).
    """
    val = os.environ.get(ENV_VAR, "").strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    try:
        return usage_log_marker_path().is_file()
    except OSError:  # pragma: no cover  -- stat failure on a path we own
        return False


def enable_marker() -> Path:
    """Create the opt-in marker file. Returns the path."""
    target = usage_log_marker_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text("# roost: per-probe usage logging enabled\n")
    return target


def disable_marker() -> bool:
    """Remove the opt-in marker file. Returns True if removed, False if absent."""
    target = usage_log_marker_path()
    if not target.exists():
        return False
    try:
        target.unlink()
    except OSError as exc:  # pragma: no cover  -- cleanup fails rarely; logged
        log.warning("usage_log: could not remove marker %s: %s", target, exc)
        return False
    return True


def _record_for(entry: ProfileHealth) -> dict[str, Any]:
    """Build the JSON record for one probe outcome.

    Only the fields useful for time-series analysis. We deliberately don't
    log the full ProfileHealth (which includes credentials_mtime, error
    objects, etc.) — keep records small for grep-ability and skim-ability.
    """
    usage = entry.usage
    extra = usage.extra if usage else None
    spend = usage.spend if usage else None
    return {
        "ts": entry.probed_at.replace(tzinfo=entry.probed_at.tzinfo or UTC)
            .isoformat().replace("+00:00", "Z"),
        "profile": entry.name,
        "health": entry.health.value,
        "session_pct": usage.session_pct if usage else None,
        "weekly_pct": usage.weekly_pct if usage else None,
        # sonnet_pct/opus_pct are `null` on every account under the new
        # response shape (per-model capacity moved to `limits[]`) but are
        # kept here, not removed: they may still populate for Pro/Team or
        # older server builds, and dropping the key would break
        # `iter_records` for every historical line already on disk.
        "sonnet_pct": usage.sonnet_pct if usage else None,
        "opus_pct": usage.opus_pct if usage else None,
        "fable_pct": usage.fable_pct if usage else None,
        "overage_pct": extra.utilization if extra else None,
        "spend_pct": spend.percent if spend else None,
        "currency": extra.currency if extra else None,
        "latency_ms": entry.probe_latency_ms,
    }


def append(entry: ProfileHealth) -> None:
    """Append one probe record to usage-log.ndjson if logging is enabled.

    Best-effort: silently swallows OSError so a full disk or permission
    failure doesn't cascade into the probe path. The cost of losing one
    log line is negligible vs. crashing a `roost pick` invocation.
    """
    if not is_enabled():
        return
    target = usage_log_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(_record_for(entry)) + "\n"
        with target.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError as exc:
        log.debug("usage_log: append failed (%s); skipping", exc)


def append_many(entries: list[ProfileHealth]) -> int:
    """Batch append. Returns the number of records actually written.

    Used by probe.py after a multi-probe; wins one open() over N for the
    common case of fanning out to all profiles in one call.
    """
    if not is_enabled() or not entries:
        return 0
    target = usage_log_path()
    written = 0
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(_record_for(e)) + "\n")
                written += 1
    except OSError as exc:
        log.debug("usage_log: batch append failed at record %d (%s)", written, exc)
    return written


def iter_records(
    *,
    since: datetime | None = None,
    profile: str | None = None,
    path: Path | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield records from the usage log, optionally filtered.

    Filters are applied lazily so very large logs stream without loading
    everything into memory. Malformed lines (corrupt JSON, missing fields)
    are skipped silently — the log is rotated by truncation at user
    discretion and a partial trailing line is plausible.
    """
    target = path or usage_log_path()
    if not target.is_file():
        return
    try:
        with target.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                if profile and record.get("profile") != profile:
                    continue
                if since is not None:
                    ts_raw = record.get("ts")
                    if not isinstance(ts_raw, str):
                        continue
                    try:
                        iso = ts_raw[:-1] + "+00:00" if ts_raw.endswith("Z") else ts_raw
                        ts = datetime.fromisoformat(iso)
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=UTC)
                    except ValueError:
                        continue
                    if ts < since:
                        continue
                yield record
    except OSError:  # pragma: no cover  -- file vanished mid-read
        return


def truncate() -> bool:
    """Empty the usage log file (preserves the file itself for tail -f users).

    Returns True if a non-empty file was truncated, False if the file was
    absent or already empty.
    """
    target = usage_log_path()
    if not target.is_file():
        return False
    try:
        if target.stat().st_size == 0:
            return False
        target.write_text("")
    except OSError:  # pragma: no cover  -- best-effort
        return False
    return True
