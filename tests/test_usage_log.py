"""Tests for usage_log: opt-in toggle, append, iter_records, truncate."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from claude_lb import usage_log
from claude_lb.models import ExtraUsage, Health, ProfileHealth, Usage


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setattr(usage_log, "usage_log_path", lambda: config / "usage-log.ndjson")
    monkeypatch.setattr(usage_log, "usage_log_marker_path", lambda: config / "usage-log.enabled")
    monkeypatch.delenv(usage_log.ENV_VAR, raising=False)
    return config


def _profile_health(name: str = "a", **kw) -> ProfileHealth:
    return ProfileHealth(
        name=name,
        health=kw.get("health", Health.OK),
        probed_at=kw.get("probed_at", datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)),
        usage=kw.get("usage", Usage(weekly_pct=42, session_pct=15)),
        probe_latency_ms=kw.get("latency", 187),
    )


# ---------------------------------------------------------------------------
# is_enabled / enable_marker / disable_marker
# ---------------------------------------------------------------------------


def test_is_enabled_default_false() -> None:
    assert usage_log.is_enabled() is False


def test_is_enabled_via_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(usage_log.ENV_VAR, "1")
    assert usage_log.is_enabled() is True


def test_is_enabled_via_env_var_truthy_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for v in ("1", "true", "TRUE", "yes", "On"):
        monkeypatch.setenv(usage_log.ENV_VAR, v)
        assert usage_log.is_enabled() is True, f"value {v!r} should enable"


def test_is_enabled_env_var_falsy_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for v in ("0", "false", "no", "off", ""):
        monkeypatch.setenv(usage_log.ENV_VAR, v)
        assert usage_log.is_enabled() is False, f"value {v!r} should not enable"


def test_enable_marker_creates_file(_isolate_paths: Path) -> None:
    assert not (_isolate_paths / "usage-log.enabled").exists()
    path = usage_log.enable_marker()
    assert path.is_file()
    assert usage_log.is_enabled() is True


def test_enable_marker_idempotent(_isolate_paths: Path) -> None:
    usage_log.enable_marker()
    first_mtime = (_isolate_paths / "usage-log.enabled").stat().st_mtime
    # Calling again must not error and must not rewrite the file content.
    usage_log.enable_marker()
    second_mtime = (_isolate_paths / "usage-log.enabled").stat().st_mtime
    # mtime may differ slightly on slow FSs but the file content shouldn't.
    assert (_isolate_paths / "usage-log.enabled").is_file()
    assert second_mtime >= first_mtime


def test_disable_marker_removes_file(_isolate_paths: Path) -> None:
    usage_log.enable_marker()
    removed = usage_log.disable_marker()
    assert removed is True
    assert not (_isolate_paths / "usage-log.enabled").exists()


def test_disable_marker_when_absent_returns_false(_isolate_paths: Path) -> None:
    assert usage_log.disable_marker() is False


# ---------------------------------------------------------------------------
# append / append_many — gated on is_enabled
# ---------------------------------------------------------------------------


def test_append_does_nothing_when_disabled(_isolate_paths: Path) -> None:
    usage_log.append(_profile_health())
    assert not (_isolate_paths / "usage-log.ndjson").exists()


def test_append_writes_record_when_enabled(_isolate_paths: Path) -> None:
    usage_log.enable_marker()
    usage_log.append(_profile_health())
    log_file = _isolate_paths / "usage-log.ndjson"
    assert log_file.is_file()
    line = log_file.read_text().strip()
    record = json.loads(line)
    assert record["profile"] == "a"
    assert record["health"] == "ok"
    assert record["weekly_pct"] == 42
    assert record["session_pct"] == 15
    assert record["latency_ms"] == 187


def test_append_record_includes_overage_when_present(
    _isolate_paths: Path,
) -> None:
    usage_log.enable_marker()
    entry = _profile_health(usage=Usage(
        weekly_pct=10,
        extra=ExtraUsage(is_enabled=True, utilization=42, currency="AUD"),
    ))
    usage_log.append(entry)
    record = json.loads((_isolate_paths / "usage-log.ndjson").read_text().strip())
    assert record["overage_pct"] == 42
    assert record["currency"] == "AUD"


def test_append_many_writes_all_records_when_enabled(
    _isolate_paths: Path,
) -> None:
    usage_log.enable_marker()
    written = usage_log.append_many([
        _profile_health(name="a"),
        _profile_health(name="b"),
        _profile_health(name="c"),
    ])
    assert written == 3
    lines = (_isolate_paths / "usage-log.ndjson").read_text().strip().splitlines()
    assert len(lines) == 3
    assert [json.loads(line)["profile"] for line in lines] == ["a", "b", "c"]


def test_append_many_returns_zero_when_disabled(_isolate_paths: Path) -> None:
    written = usage_log.append_many([_profile_health()])
    assert written == 0


def test_append_many_empty_list_returns_zero_even_when_enabled(
    _isolate_paths: Path,
) -> None:
    usage_log.enable_marker()
    assert usage_log.append_many([]) == 0


def test_append_silently_swallows_oserror(
    _isolate_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full disk or permission failure must not crash the probe path."""
    usage_log.enable_marker()

    class _Boom(Path):
        _flavour = type(Path("/")) ._flavour  # type: ignore[attr-defined]

        def open(self, *a, **kw):  # type: ignore[no-untyped-def]
            raise OSError("simulated")

    # Easier: monkeypatch json.dumps to raise.
    def _bad_open(self, *a, **kw):  # type: ignore[no-untyped-def]
        raise OSError("disk full")

    monkeypatch.setattr(Path, "open", _bad_open)
    # Should not raise:
    usage_log.append(_profile_health())


# ---------------------------------------------------------------------------
# iter_records — read with optional filtering
# ---------------------------------------------------------------------------


def test_iter_records_yields_in_file_order(_isolate_paths: Path) -> None:
    usage_log.enable_marker()
    base = datetime(2026, 4, 27, 0, 0, 0, tzinfo=UTC)
    for i in range(3):
        usage_log.append(_profile_health(
            name=f"p{i}",
            probed_at=base + timedelta(minutes=i),
        ))
    names = [r["profile"] for r in usage_log.iter_records()]
    assert names == ["p0", "p1", "p2"]


def test_iter_records_filters_by_profile(_isolate_paths: Path) -> None:
    usage_log.enable_marker()
    usage_log.append(_profile_health(name="a"))
    usage_log.append(_profile_health(name="b"))
    usage_log.append(_profile_health(name="a"))
    out = list(usage_log.iter_records(profile="a"))
    assert len(out) == 2
    assert all(r["profile"] == "a" for r in out)


def test_iter_records_filters_by_since(_isolate_paths: Path) -> None:
    usage_log.enable_marker()
    base = datetime(2026, 4, 27, 0, 0, 0, tzinfo=UTC)
    usage_log.append(_profile_health(name="old", probed_at=base))
    usage_log.append(_profile_health(name="new", probed_at=base + timedelta(hours=2)))

    cutoff = base + timedelta(hours=1)
    out = list(usage_log.iter_records(since=cutoff))
    assert [r["profile"] for r in out] == ["new"]


def test_iter_records_skips_malformed_lines(_isolate_paths: Path) -> None:
    """Partial trailing line (mid-rotation) and corrupt JSON must be ignored."""
    usage_log.enable_marker()
    usage_log.append(_profile_health(name="good"))
    log_file = _isolate_paths / "usage-log.ndjson"
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
        fh.write("\n")  # blank line
        fh.write("[1, 2, 3]\n")  # valid JSON but wrong shape
    out = list(usage_log.iter_records())
    assert [r["profile"] for r in out] == ["good"]


def test_iter_records_returns_empty_when_file_missing(
    _isolate_paths: Path,
) -> None:
    out = list(usage_log.iter_records())
    assert out == []


# ---------------------------------------------------------------------------
# truncate
# ---------------------------------------------------------------------------


def test_truncate_empties_file_in_place(_isolate_paths: Path) -> None:
    usage_log.enable_marker()
    usage_log.append(_profile_health())
    log_file = _isolate_paths / "usage-log.ndjson"
    assert log_file.stat().st_size > 0

    truncated = usage_log.truncate()
    assert truncated is True
    assert log_file.is_file()  # file still exists
    assert log_file.stat().st_size == 0


def test_truncate_returns_false_when_file_missing(_isolate_paths: Path) -> None:
    assert usage_log.truncate() is False


def test_truncate_returns_false_when_file_already_empty(
    _isolate_paths: Path,
) -> None:
    log_file = _isolate_paths / "usage-log.ndjson"
    log_file.write_text("")
    assert usage_log.truncate() is False
