"""Live-refreshing TUI for `roost top`.

Wraps Rich's `Live` to render the existing `render_status_table` snapshot at
a configurable refresh interval. Designed to leave on a side monitor during
incidents.

Bounded iteration count (`max_iterations`) is a hidden test seam: production
calls leave it at None (infinite, exit on Ctrl+C); tests pass small ints to
get a finite, deterministic loop without spinning up signal handlers.
"""

from __future__ import annotations

import time
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.table import Table

from .cache import HealthCache
from .models import ProfileHealth
from .output import render_status_table


def _build_snapshot_table(entries: list[ProfileHealth]) -> Table:
    """Mirror of render_status_table's layout, returned as a Table for Live.

    We can't reuse render_status_table directly because it prints to stderr;
    Live needs a renderable to refresh in place. The columns and conditional-
    rendering rules match render_status_table so the appearance is consistent.
    """
    # Reuse the exact rendering by creating a Console that captures, then
    # asking it to render — but simpler: rebuild the table here, mirroring
    # the existing layout. Keep it terse since the column logic itself is
    # tested against render_status_table elsewhere.
    table = Table(title="claude-lb (live)", title_justify="left", show_lines=False)
    table.add_column("Profile", no_wrap=True)
    table.add_column("Health", no_wrap=True)
    table.add_column("Session %", justify="right")
    table.add_column("Weekly %", justify="right")
    for e in entries:
        usage = e.usage
        sess = (
            f"{usage.session_pct}%" if usage and usage.session_pct is not None
            else "—"
        )
        wk = (
            f"{usage.weekly_pct}%" if usage and usage.weekly_pct is not None
            else "—"
        )
        # Subtle colour cue: red if not OK.
        health_text = (
            f"[green]{e.health.value}[/green]" if e.health.value == "ok"
            else f"[red]{e.health.value}[/red]"
        )
        table.add_row(e.name, health_text, sess, wk)
    return table


def run_live(
    *,
    refresh_fn: Any,
    interval_s: float = 2.0,
    max_iterations: int | None = None,
    console: Console | None = None,
) -> int:
    """Drive the Live loop. `refresh_fn() -> (cache, names)` is called every
    `interval_s` seconds. Returns the number of frames rendered (handy for
    tests).

    `max_iterations=None` loops until the user hits Ctrl+C.
    """
    out = console or Console()
    frames = 0
    try:
        with Live(console=out, refresh_per_second=4, transient=False) as live:
            while True:
                cache, names = refresh_fn()
                entries = [
                    cache.profiles[n] for n in names if n in cache.profiles
                ]
                live.update(_build_snapshot_table(entries))
                frames += 1
                if max_iterations is not None and frames >= max_iterations:
                    break
                time.sleep(interval_s)
    except KeyboardInterrupt:  # pragma: no cover  -- interactive only
        pass
    return frames


# render_status_table is re-exported for tests that want the full-fat
# representation (with all conditional columns) — top uses the trimmed Table
# above to keep the live frame readable in narrow terminals.
__all__ = ["run_live", "render_status_table", "HealthCache"]
