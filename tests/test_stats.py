"""Tests for stats: parse_pick_log, aggregate_stats, summarise_metric, sparkline, project_exhaustion."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from claude_lb.stats import (
    ProfileMetricSummary,
    aggregate_stats,
    parse_pick_log,
    project_exhaustion,
    sparkline,
    summarise_metric,
)

# ---------------------------------------------------------------------------
# parse_pick_log
# ---------------------------------------------------------------------------


def test_parse_pick_log_returns_empty_on_missing_file(tmp_path: Path) -> None:
    assert parse_pick_log(tmp_path / "nope.log") == []


def test_parse_pick_log_parses_pick_lines(tmp_path: Path) -> None:
    log = tmp_path / "picks.log"
    log.write_text(
        "2026-04-27T12:00:00Z\taccount-a\tleast-used\tscore=1.00\n"
        "2026-04-27T12:01:00Z\taccount-b\tsticky\tscore=0.50\n"
    )
    entries = parse_pick_log(log)
    assert len(entries) == 2
    assert entries[0]["profile"] == "account-a"
    assert entries[0]["action"] == "least-used"
    assert entries[0]["details"] == {"score": "1.00"}


def test_parse_pick_log_parses_exec_lines(tmp_path: Path) -> None:
    log = tmp_path / "picks.log"
    log.write_text(
        "2026-04-27T12:00:00Z\taccount-a\tEXEC\targv=claude\trc=0\tdur=1234ms\n"
    )
    entry = parse_pick_log(log)[0]
    assert entry["action"] == "EXEC"
    assert entry["details"]["rc"] == "0"
    assert entry["details"]["dur"] == "1234ms"
    assert entry["details"]["argv"] == "claude"


def test_parse_pick_log_skips_malformed_lines(tmp_path: Path) -> None:
    log = tmp_path / "picks.log"
    log.write_text(
        "2026-04-27T12:00:00Z\taccount-a\tleast-used\tscore=1.00\n"
        "garbage line with no tabs\n"
        "\n"
        "not-a-timestamp\tprofile\taction\n"  # bad timestamp
        "2026-04-27T12:01:00Z\taccount-b\tsticky\n"  # no details OK
    )
    entries = parse_pick_log(log)
    assert len(entries) == 2
    assert [e["profile"] for e in entries] == ["account-a", "account-b"]


def test_parse_pick_log_handles_z_suffix_and_offset(tmp_path: Path) -> None:
    log = tmp_path / "picks.log"
    log.write_text(
        "2026-04-27T12:00:00Z\ta\tsticky\n"
        "2026-04-27T12:00:00+00:00\tb\tsticky\n"
        "2026-04-27T13:00:00+01:00\tc\tsticky\n"
    )
    entries = parse_pick_log(log)
    assert len(entries) == 3
    # All timestamps should round-trip to the same UTC moment.
    assert entries[0]["timestamp"] == entries[1]["timestamp"]


# ---------------------------------------------------------------------------
# aggregate_stats
# ---------------------------------------------------------------------------


def _entry(ts: datetime, profile: str, action: str, **details) -> dict:
    return {
        "timestamp": ts,
        "profile": profile,
        "action": action,
        "details": {k: str(v) for k, v in details.items()},
    }


def test_aggregate_empty_returns_zero_counts() -> None:
    report = aggregate_stats([])
    assert report.pick_total == 0
    assert report.exec_total == 0
    assert report.window_start is None
    assert report.exec_p50_ms is None


def test_aggregate_counts_picks_by_profile_and_strategy() -> None:
    base = datetime(2026, 4, 27, 0, 0, 0, tzinfo=UTC)
    entries = [
        _entry(base, "a", "sticky"),
        _entry(base + timedelta(seconds=1), "a", "least-used"),
        _entry(base + timedelta(seconds=2), "b", "sticky"),
    ]
    report = aggregate_stats(entries)
    assert report.pick_total == 3
    assert report.pick_by_profile["a"] == 2
    assert report.pick_by_profile["b"] == 1
    assert report.pick_by_strategy["sticky"] == 2
    assert report.pick_by_strategy["least-used"] == 1


def test_aggregate_separates_pick_and_exec_totals() -> None:
    base = datetime(2026, 4, 27, tzinfo=UTC)
    entries = [
        _entry(base, "a", "sticky"),
        _entry(base, "a", "EXEC", argv="claude", rc=0, dur="100ms"),
    ]
    report = aggregate_stats(entries)
    assert report.pick_total == 1
    assert report.exec_total == 1


def test_aggregate_computes_percentiles_for_exec_durations() -> None:
    base = datetime(2026, 4, 27, tzinfo=UTC)
    durations = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    entries = [
        _entry(base, "a", "EXEC", argv="x", rc=0, dur=f"{d}ms")
        for d in durations
    ]
    report = aggregate_stats(entries)
    assert report.exec_p50_ms == 55.0  # interpolated between 50 and 60
    assert report.exec_p95_ms is not None
    assert 90 <= report.exec_p95_ms <= 100


def test_aggregate_computes_failure_rate() -> None:
    base = datetime(2026, 4, 27, tzinfo=UTC)
    entries = [
        _entry(base, "a", "EXEC", argv="x", rc=0, dur="10ms"),
        _entry(base, "a", "EXEC", argv="x", rc=1, dur="20ms"),
        _entry(base, "a", "EXEC", argv="x", rc=2, dur="30ms"),
        _entry(base, "a", "EXEC", argv="x", rc=0, dur="40ms"),
    ]
    report = aggregate_stats(entries)
    assert report.exec_failure_rate == 0.5  # 2 of 4 non-zero


def test_aggregate_window_start_end() -> None:
    base = datetime(2026, 4, 27, tzinfo=UTC)
    entries = [
        _entry(base + timedelta(hours=2), "a", "sticky"),
        _entry(base, "a", "sticky"),
        _entry(base + timedelta(hours=1), "a", "sticky"),
    ]
    report = aggregate_stats(entries)
    assert report.window_start == base
    assert report.window_end == base + timedelta(hours=2)


def test_aggregate_to_json_round_trip() -> None:
    base = datetime(2026, 4, 27, tzinfo=UTC)
    entries = [
        _entry(base, "a", "sticky"),
        _entry(base, "a", "EXEC", argv="x", rc=0, dur="100ms"),
    ]
    payload = aggregate_stats(entries).to_json()
    assert payload["pick"]["total"] == 1
    assert payload["exec"]["total"] == 1
    assert payload["pick"]["by_profile"] == {"a": 1}


def test_aggregate_handles_exec_without_dur() -> None:
    """Defensive: an EXEC line missing dur= shouldn't blow up percentiles."""
    base = datetime(2026, 4, 27, tzinfo=UTC)
    entries = [
        _entry(base, "a", "EXEC", argv="x", rc=0),  # no dur
    ]
    report = aggregate_stats(entries)
    assert report.exec_total == 1
    assert report.exec_p50_ms is None  # no dur samples to compute


def test_aggregate_handles_exec_with_non_numeric_rc() -> None:
    base = datetime(2026, 4, 27, tzinfo=UTC)
    entries = [
        _entry(base, "a", "EXEC", argv="x", rc="?", dur="100ms"),
    ]
    report = aggregate_stats(entries)
    assert report.exec_total == 1
    # rc="?" can't be parsed as int -> not counted as failure or success
    assert report.exec_failure_rate == 0.0


# ---------------------------------------------------------------------------
# sparkline
# ---------------------------------------------------------------------------


def test_sparkline_empty_returns_empty_string() -> None:
    assert sparkline([]) == ""


def test_sparkline_single_value_returns_one_glyph() -> None:
    out = sparkline([42.0])
    assert len(out) == 1


def test_sparkline_constant_series_renders_uniform() -> None:
    out = sparkline([5.0, 5.0, 5.0, 5.0])
    assert len(out) == 4
    assert len(set(out)) == 1  # all the same glyph


def test_sparkline_ascending_series_renders_low_to_high() -> None:
    out = sparkline([1, 2, 3, 4, 5, 6, 7, 8])
    # First glyph is the lowest available, last is the highest.
    assert out[0] == "▁"
    assert out[-1] == "█"


def test_sparkline_renders_one_glyph_per_value() -> None:
    out = sparkline([1, 5, 3, 7, 2])
    assert len(out) == 5


# ---------------------------------------------------------------------------
# summarise_metric
# ---------------------------------------------------------------------------


def _record(ts: str, profile: str, **fields) -> dict:
    return {"ts": ts, "profile": profile, **fields}


def test_summarise_metric_groups_by_profile() -> None:
    records = [
        _record("2026-04-27T12:00:00Z", "a", weekly_pct=10),
        _record("2026-04-27T12:05:00Z", "a", weekly_pct=20),
        _record("2026-04-27T12:00:00Z", "b", weekly_pct=80),
    ]
    summaries = summarise_metric(records, metric="weekly_pct")
    by_name = {s.profile: s for s in summaries}
    assert set(by_name.keys()) == {"a", "b"}
    assert by_name["a"].minimum == 10
    assert by_name["a"].maximum == 20
    assert by_name["a"].average == 15
    assert by_name["a"].latest == 20
    assert by_name["b"].samples == 1


def test_summarise_metric_skips_records_with_null_value() -> None:
    records = [
        _record("2026-04-27T12:00:00Z", "a", weekly_pct=10),
        _record("2026-04-27T12:01:00Z", "a", weekly_pct=None),
        _record("2026-04-27T12:02:00Z", "a", weekly_pct=20),
    ]
    summary = summarise_metric(records, metric="weekly_pct")[0]
    assert summary.samples == 2
    assert summary.average == 15


def test_summarise_metric_skips_records_missing_metric_key() -> None:
    records = [
        _record("2026-04-27T12:00:00Z", "a", session_pct=10),
        _record("2026-04-27T12:01:00Z", "a", weekly_pct=20),
    ]
    summary = summarise_metric(records, metric="weekly_pct")
    # Only the second record contributes.
    assert summary[0].samples == 1
    assert summary[0].latest == 20


def test_summarise_metric_sorts_series_by_timestamp() -> None:
    """Records arriving out-of-order in the log should be sorted before the
    sparkline / latest are computed."""
    records = [
        _record("2026-04-27T13:00:00Z", "a", weekly_pct=99),
        _record("2026-04-27T12:00:00Z", "a", weekly_pct=10),
    ]
    summary = summarise_metric(records, metric="weekly_pct")[0]
    assert summary.latest == 99
    assert [v for _, v in summary.series] == [10, 99]


def test_summarise_metric_to_json_excludes_series() -> None:
    records = [_record("2026-04-27T12:00:00Z", "a", weekly_pct=10)]
    summary = summarise_metric(records, metric="weekly_pct")[0]
    payload = summary.to_json()
    assert "series" not in payload
    assert payload["samples"] == 1


def test_summarise_metric_fable_pct() -> None:
    records = [
        _record("2026-04-27T12:00:00Z", "a", fable_pct=5),
        _record("2026-04-27T12:05:00Z", "a", fable_pct=15),
        _record("2026-04-27T12:10:00Z", "a", fable_pct=25),
    ]
    summary = summarise_metric(records, metric="fable_pct")[0]
    assert summary.samples == 3
    assert summary.minimum == 5
    assert summary.maximum == 25
    assert summary.average == 15
    assert summary.latest == 25


def test_summarise_metric_spend_pct() -> None:
    records = [
        _record("2026-04-27T12:00:00Z", "a", spend_pct=10),
        _record("2026-04-27T12:05:00Z", "a", spend_pct=30),
    ]
    summary = summarise_metric(records, metric="spend_pct")[0]
    assert summary.samples == 2
    assert summary.average == 20
    assert summary.latest == 30


def test_summarise_metric_all_null_metric_returns_no_data() -> None:
    """sonnet_pct/opus_pct are null on every record under the new response
    shape — summarise_metric must return an empty (no-data) result rather
    than raising or dividing by zero."""
    records = [
        _record("2026-04-27T12:00:00Z", "a", sonnet_pct=None, weekly_pct=10),
        _record("2026-04-27T12:05:00Z", "a", sonnet_pct=None, weekly_pct=20),
        _record("2026-04-27T12:00:00Z", "b", sonnet_pct=None, weekly_pct=30),
    ]
    summaries = summarise_metric(records, metric="sonnet_pct")
    assert summaries == []

    # Downstream consumers (sparkline over an absent summary's series) must
    # also degrade cleanly rather than raising.
    assert sparkline([]) == ""


def test_summarise_metric_missing_key_entirely_returns_no_data() -> None:
    """A metric name that never appears in any record (not even as null)
    must also produce a clean empty result."""
    records = [_record("2026-04-27T12:00:00Z", "a", weekly_pct=10)]
    assert summarise_metric(records, metric="fable_pct") == []


# ---------------------------------------------------------------------------
# project_exhaustion (linear burn-rate forecast)
# ---------------------------------------------------------------------------


def test_project_exhaustion_returns_none_for_too_few_samples() -> None:
    summary = ProfileMetricSummary(
        profile="a", metric="weekly_pct", samples=1,
        latest=10.0, series=[(datetime(2026, 4, 27, tzinfo=UTC), 10.0)],
    )
    assert project_exhaustion(summary) is None


def test_project_exhaustion_returns_none_for_decreasing_metric() -> None:
    base = datetime(2026, 4, 27, tzinfo=UTC)
    summary = ProfileMetricSummary(
        profile="a", metric="weekly_pct", samples=2,
        latest=5.0,
        series=[(base, 10.0), (base + timedelta(hours=1), 5.0)],
    )
    assert project_exhaustion(summary) is None


def test_project_exhaustion_returns_none_when_already_at_target() -> None:
    base = datetime(2026, 4, 27, tzinfo=UTC)
    summary = ProfileMetricSummary(
        profile="a", metric="weekly_pct", samples=2,
        latest=100.0,
        series=[(base, 50.0), (base + timedelta(hours=1), 100.0)],
    )
    assert project_exhaustion(summary) is None


def test_project_exhaustion_linear_forecast_to_100() -> None:
    """Two samples 1h apart, going from 10% to 60% -> reach 100% in 0.8h."""
    base = datetime(2026, 4, 27, tzinfo=UTC)
    summary = ProfileMetricSummary(
        profile="a", metric="weekly_pct", samples=2,
        latest=60.0,
        series=[(base, 10.0), (base + timedelta(hours=1), 60.0)],
    )
    secs = project_exhaustion(summary)
    assert secs is not None
    # 50 percentage points in 3600s -> 50/h -> 40 more pts to 100% -> 0.8h = 2880s
    assert 2870 < secs < 2890


def test_project_exhaustion_uses_only_tail_samples() -> None:
    """A long, ancient prefix of flat-then-tiny growth followed by recent rapid
    growth should produce a faster projection than the naive global slope.

    The implementation looks at the last 8 samples — so a series with the
    rapid growth concentrated in the tail should outpace a series where the
    rapid growth is buried at the start.
    """
    base = datetime(2026, 4, 27, tzinfo=UTC)
    # Recent tail dominates (last 4 samples: 30 → 60 over 3 hours).
    series = [
        (base + timedelta(hours=0), 10.0),
        (base + timedelta(hours=1), 10.0),
        (base + timedelta(hours=2), 10.0),
        (base + timedelta(hours=3), 10.0),
        (base + timedelta(hours=4), 30.0),
        (base + timedelta(hours=5), 40.0),
        (base + timedelta(hours=6), 50.0),
        (base + timedelta(hours=7), 60.0),
    ]
    summary = ProfileMetricSummary(
        profile="a", metric="weekly_pct", samples=len(series),
        latest=60.0, series=series,
    )
    secs = project_exhaustion(summary)
    assert secs is not None
    # Reasonable bound: the recent climb of 50 pts in 4h would project ~3.2h
    # to reach 100; the dilution from the flat samples can pull this out but
    # not beyond ~8h.
    assert 0 < secs < 8 * 3600
