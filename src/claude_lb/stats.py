"""Aggregations over picks.log + usage-log.ndjson.

Powers `roost stats` (picks.log) and `roost report` (usage log).
Both commands are pure read-side: they never write to either log.

Schemas:

    picks.log line:
        {iso8601}\t{profile}\t{action}\tkey=value\tkey=value...

    where `action` is a strategy name (`sticky`, `least-used`, ...) for picks
    or `EXEC` for child runs. EXEC entries carry `argv=`, `rc=`, `dur=` keys.

    usage-log.ndjson line:
        one JSON object per line, see usage_log._record_for() for the schema.

`summarise_metric` is metric-name-agnostic — it reads whatever key is passed
via `metric=` off each record, so it already covers weekly_pct, session_pct,
sonnet_pct, opus_pct, overage_pct, fable_pct, and spend_pct without a
per-metric branch. A metric where every record is null (e.g. sonnet_pct/
opus_pct on accounts migrated to the new `limits[]` shape) naturally produces
zero grouped samples, so summarise_metric returns `[]` rather than raising or
dividing by zero — see test_summarise_metric_all_null_metric_returns_no_data
in test_stats.py.
"""

from __future__ import annotations

import logging
import statistics
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# picks.log parser (refactored out of cli.py for reuse)
# ---------------------------------------------------------------------------


def parse_pick_log(path: Path) -> list[dict[str, Any]]:
    """Parse picks.log into structured entries.

    Lines are tab-separated:
        {ts}\\t{profile}\\t{action}\\t{key=value}...
    Where action is a strategy name (`sticky`, `least-used`, ...) for picks
    or `EXEC` for child runs. Malformed lines are skipped silently — the log
    is rotated under load and a partial last line is plausible.
    """
    entries: list[dict[str, Any]] = []
    if not Path(str(path)).is_file():
        return entries
    try:
        text = Path(str(path)).read_text(encoding="utf-8")
    except OSError:
        return entries
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        ts_raw, profile, action = parts[0], parts[1], parts[2]
        try:
            iso = ts_raw[:-1] + "+00:00" if ts_raw.endswith("Z") else ts_raw
            ts = datetime.fromisoformat(iso)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
        except ValueError:
            continue
        details: dict[str, str] = {}
        for chunk in parts[3:]:
            if "=" in chunk:
                k, v = chunk.split("=", 1)
                details[k] = v
        entries.append({
            "timestamp": ts,
            "profile": profile,
            "action": action,
            "details": details,
        })
    return entries


# ---------------------------------------------------------------------------
# Stats: aggregations over picks.log
# ---------------------------------------------------------------------------


@dataclass
class StatsReport:
    """Aggregated picks.log report."""

    window_start: datetime | None = None
    window_end: datetime | None = None
    pick_total: int = 0
    pick_by_profile: Counter[str] = field(default_factory=Counter)
    pick_by_strategy: Counter[str] = field(default_factory=Counter)
    exec_total: int = 0
    exec_by_profile: Counter[str] = field(default_factory=Counter)
    exec_by_rc: Counter[str] = field(default_factory=Counter)
    exec_p50_ms: float | None = None
    exec_p95_ms: float | None = None
    exec_failure_rate: float | None = None  # rc != 0 / total

    def to_json(self) -> dict[str, Any]:
        return {
            "window_start": (
                self.window_start.isoformat().replace("+00:00", "Z")
                if self.window_start else None
            ),
            "window_end": (
                self.window_end.isoformat().replace("+00:00", "Z")
                if self.window_end else None
            ),
            "pick": {
                "total": self.pick_total,
                "by_profile": dict(self.pick_by_profile),
                "by_strategy": dict(self.pick_by_strategy),
            },
            "exec": {
                "total": self.exec_total,
                "by_profile": dict(self.exec_by_profile),
                "by_rc": dict(self.exec_by_rc),
                "p50_ms": self.exec_p50_ms,
                "p95_ms": self.exec_p95_ms,
                "failure_rate": self.exec_failure_rate,
            },
        }


def _percentile(sorted_values: list[float], pct: float) -> float | None:
    """Linear-interpolation percentile. None if empty."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * pct
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)


def aggregate_stats(
    entries: Iterable[dict[str, Any]],
) -> StatsReport:
    """Compute counts + p50/p95 over an iterable of parse_pick_log entries.

    Robust to malformed details: rc=, dur= are parsed defensively. Entries
    that can't yield a dur_ms are excluded from the percentile calc but still
    counted in exec_total + exec_by_*.
    """
    report = StatsReport()
    durations: list[float] = []
    failures = 0
    timestamps: list[datetime] = []

    for entry in entries:
        ts = entry.get("timestamp")
        if isinstance(ts, datetime):
            timestamps.append(ts)
        profile = entry.get("profile", "")
        action = entry.get("action", "")
        details = entry.get("details", {})

        if action == "EXEC":
            report.exec_total += 1
            report.exec_by_profile[profile] += 1
            rc_raw = details.get("rc", "?")
            report.exec_by_rc[rc_raw] += 1
            try:
                if int(rc_raw) != 0:
                    failures += 1
            except (TypeError, ValueError):
                pass
            dur_raw = details.get("dur", "")
            if dur_raw.endswith("ms"):
                try:
                    durations.append(float(dur_raw[:-2]))
                except ValueError:
                    pass
        elif action:
            # Treat any non-EXEC action as a strategy/pick.
            report.pick_total += 1
            report.pick_by_profile[profile] += 1
            report.pick_by_strategy[action] += 1

    if timestamps:
        report.window_start = min(timestamps)
        report.window_end = max(timestamps)
    if durations:
        durations.sort()
        report.exec_p50_ms = _percentile(durations, 0.5)
        report.exec_p95_ms = _percentile(durations, 0.95)
    if report.exec_total > 0:
        report.exec_failure_rate = failures / report.exec_total

    return report


# ---------------------------------------------------------------------------
# Report: aggregations over usage-log.ndjson (one record per probe)
# ---------------------------------------------------------------------------


@dataclass
class ProfileMetricSummary:
    """Min/max/avg/last for one numeric metric over one profile's records."""

    profile: str
    metric: str
    samples: int = 0
    minimum: float | None = None
    maximum: float | None = None
    average: float | None = None
    latest: float | None = None
    series: list[tuple[datetime, float]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "metric": self.metric,
            "samples": self.samples,
            "min": self.minimum,
            "max": self.maximum,
            "avg": self.average,
            "latest": self.latest,
        }


# Sparkline glyphs from low to high (Unicode block ascending).
_SPARK_BARS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float]) -> str:
    """Render values as a 1-line Unicode sparkline. Empty input -> empty string.

    All values mapped to the 8-bar range, normalised against this series's own
    min/max. Constant series rendered as a flat row of mid-bars (▄).
    """
    if not values:
        return ""
    if len(values) == 1:
        return _SPARK_BARS[len(_SPARK_BARS) // 2]
    lo = min(values)
    hi = max(values)
    span = hi - lo
    if span == 0:
        return _SPARK_BARS[len(_SPARK_BARS) // 2] * len(values)
    out = []
    for v in values:
        idx = int((v - lo) / span * (len(_SPARK_BARS) - 1))
        out.append(_SPARK_BARS[idx])
    return "".join(out)


def summarise_metric(
    records: Iterable[dict[str, Any]],
    *,
    metric: str,
    by_profile: bool = True,
) -> list[ProfileMetricSummary]:
    """Group records by profile and compute summary stats for one metric.

    Records missing the metric (e.g. `weekly_pct: null` for a Pro plan) are
    skipped per-profile so a profile that always reports None doesn't
    contaminate another profile's average.
    """
    grouped: dict[str, list[tuple[datetime, float]]] = {}

    for record in records:
        value = record.get(metric)
        if value is None:
            continue
        try:
            f = float(value)
        except (TypeError, ValueError):
            continue
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
        profile = record.get("profile", "")
        key = profile if by_profile else "*"
        grouped.setdefault(key, []).append((ts, f))

    summaries: list[ProfileMetricSummary] = []
    for profile, points in sorted(grouped.items()):
        # Sort by timestamp so latest + sparkline reflect chronological order.
        points.sort(key=lambda p: p[0])
        values = [p[1] for p in points]
        summary = ProfileMetricSummary(
            profile=profile,
            metric=metric,
            samples=len(values),
            minimum=min(values),
            maximum=max(values),
            average=statistics.fmean(values),
            latest=values[-1],
            series=points,
        )
        summaries.append(summary)
    return summaries


def project_exhaustion(
    summary: ProfileMetricSummary,
    *,
    target: float = 100.0,
) -> float | None:
    """Linear-extrapolation forecast: seconds until `metric` reaches `target`.

    Uses the slope of the last N samples. Returns None if:
      - fewer than 2 samples,
      - the metric is decreasing or flat (no exhaustion in this direction),
      - the target is already reached.

    Conservative — extrapolation is just a hint, not a contract.
    """
    if len(summary.series) < 2:
        return None
    if summary.latest is None or summary.latest >= target:
        return None
    # Use last min(8, N) points for the regression — recent burn rate matters
    # more than ancient samples.
    tail = summary.series[-min(8, len(summary.series)):]
    if len(tail) < 2:
        return None
    t0 = tail[0][0]
    xs = [(p[0] - t0).total_seconds() for p in tail]
    ys = [p[1] for p in tail]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den = sum((x - mean_x) ** 2 for x in xs)
    if den == 0:
        return None
    slope = num / den  # units per second
    if slope <= 0:
        return None
    intercept = mean_y - slope * mean_x
    target_x = (target - intercept) / slope
    last_x = xs[-1]
    delta = target_x - last_x
    if delta <= 0:
        return None
    return delta
