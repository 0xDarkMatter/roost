"""Terminal panel design system — Python port for roost.

A small, dependency-free renderer for the claude-mods terminal-panel grammar
(see claude-mods/docs/TERMINAL-DESIGN.md). This is the Python sibling of the
bash `skills/_lib/term.sh` and PowerShell `skills/_lib/term.ps1`
implementations. roost lives in its own repo, so it carries its own port
rather than sourcing the shared library — the registry keys and helper
semantics are kept in lockstep with the bash sibling.

Design invariants honoured here:
    - Color is signal, never the only signal (NO_COLOR strips ANSI, the
      glyphs + grid still read).
    - ASCII fallback for every Unicode glyph (TERM_ASCII=1 / non-UTF locale).
    - The left rail (`│`) tethers the body; sections attach at the panel edge.
    - Continuous rules; a single `●` terminator at the right of header/footer.

Detection is done against the chosen stream (default: stderr, since panels are
human chrome and stdout stays data-only per Forma §3).
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import unicodedata
from typing import IO

# ─── Color tokens (ANSI) ────────────────────────────────────────────────────
_ANSI = {
    "green": "\033[32m",
    "yellow": "\033[33m",
    "orange": "\033[38;5;208m",
    "red": "\033[31m",
    "cyan": "\033[36m",
    "magenta": "\033[35m",
    "dim": "\033[2m",
    "bold": "\033[1m",
}
_OFF = "\033[0m"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# ─── Glyph registries (unicode, ascii) ──────────────────────────────────────
# Brand emoji. roost is keyed here; the shared claude-mods registry doesn't
# know about roost (different repo) and doesn't need to.
_BRAND = {
    "roost": ("🐓", "[R]"),
    "fleet": ("⚡", "[F]"),
    "forge": ("🔨", "[B]"),
    "git": ("🌿", "[G]"),
}

# Health bullets (§4.11). Mirrors TERM_HEALTH_GLYPH in term.sh.
_HEALTH = {
    "healthy": ("•", "(+)"),
    "pending": ("•", "(.)"),
    "warning": ("•", "(!)"),
    "critical": ("•", "(!!)"),
    "busted": ("⬤", "(X)"),
    "unknown": ("•", "(?)"),
}
_HEALTH_COLOR = {
    "healthy": "green",
    "pending": "yellow",
    "warning": "orange",
    "critical": "red",
    "busted": "dim",
    "unknown": "dim",
}

# Checklist marks for the status-panel pattern (§5.3). roost-local — the bash
# sibling renders checklists with these same shapes.
_CHECK = {
    "ok": ("✓", "+", "green"),
    "warn": ("▲", "!", "orange"),
    "fail": ("✗", "x", "red"),
}


def _supports_color(stream: IO[str]) -> bool:
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def _ascii_only() -> bool:
    if os.environ.get("TERM_ASCII") == "1":
        return True
    enc = (os.environ.get("LC_ALL") or os.environ.get("LANG") or "").lower()
    if enc and "utf" not in enc:
        return True
    return False


def display_width(text: str) -> int:
    """Visible cell width: strips ANSI, counts wide glyphs as 2, marks as 0."""
    stripped = _ANSI_RE.sub("", text)
    width = 0
    for ch in stripped:
        if ch == "️":  # variation selector — zero width
            continue
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


class Term:
    """Stateful renderer. Construct once, ask it for panel lines.

    Every method returns a string (no I/O), so callers stay in control of the
    output stream. Use the module-level `panel` collector for the common
    open → body → close flow.
    """

    def __init__(
        self,
        *,
        stream: IO[str] | None = None,
        color: bool | None = None,
        ascii_mode: bool | None = None,
        width: int | None = None,
    ) -> None:
        out = stream if stream is not None else sys.stderr
        self.color_on = _supports_color(out) if color is None else color
        self.ascii = _ascii_only() if ascii_mode is None else ascii_mode
        if width is not None:
            self.width = width
        else:
            cols = shutil.get_terminal_size(fallback=(80, 24)).columns
            self.width = cols if cols >= 40 else 80

        # Glyphs resolved once, honouring ascii mode.
        g = self._g
        self.tl = g("╭", "+")
        self.bl = g("╰", "+")
        self.hrule = g("─", "-")
        self.term = g("●", "*")
        self.vert_g = g("│", "|")
        self.branch = g("├─", "+-")
        self.last = g("└─", "`-")
        self.alert_g = g("▲", "!")
        self.tip = g("💡", "(i)")
        self.ellipsis = g("…", "..")

    # ── primitives ──────────────────────────────────────────────────────
    def _g(self, uni: str, ascii_: str) -> str:
        return ascii_ if self.ascii else uni

    def paint(self, name: str, text: str) -> str:
        if not self.color_on or name not in _ANSI:
            return text
        return f"{_ANSI[name]}{text}{_OFF}"

    def brand_glyph(self, key: str) -> str:
        uni, asc = _BRAND.get(key, ("•", "*"))
        return asc if self.ascii else uni

    def health_glyph(self, state: str) -> str:
        uni, asc = _HEALTH.get(state, _HEALTH["unknown"])
        return asc if self.ascii else uni

    def truncate(self, text: str, max_cols: int) -> str:
        if display_width(text) <= max_cols:
            return text
        ell = self.ellipsis
        budget = max_cols - display_width(ell)
        out = ""
        for ch in text:
            if display_width(out + ch) > budget:
                break
            out += ch
        return out + ell

    # ── panel chrome ────────────────────────────────────────────────────
    def panel_open(self, brand_key: str, name: str, indicator: str | None = None) -> str:
        emoji = self.brand_glyph(brand_key)
        left_plain = f"{self.tl}{self.hrule}{self.hrule} {emoji} {name} "
        left = (
            f"{self.paint('cyan', self.tl + self.hrule + self.hrule)} "
            f"{emoji} {self.paint('cyan', name)} "
        )
        if indicator:
            right_plain = f" {indicator} {self.hrule * 3}{self.term}"
            right = (
                f" {self.paint('dim', indicator)} "
                f"{self.paint('cyan', self.hrule * 3 + self.term)}"
            )
        else:
            right_plain = f"{self.hrule * 3}{self.term}"
            right = self.paint("cyan", self.hrule * 3 + self.term)
        fill = self.width - display_width(left_plain) - display_width(right_plain)
        fill = max(fill, 4)
        return left + self.paint("cyan", self.hrule * fill) + right

    def panel_close(self, left_text: str | None = None, right_text: str | None = None) -> str:
        lt = left_text or ""
        left_plain = f"{self.bl}{self.hrule}{self.hrule} {lt} " if lt else f"{self.bl}{self.hrule}{self.hrule} "
        left = (
            f"{self.paint('cyan', self.bl + self.hrule + self.hrule)} {lt} "
            if lt
            else f"{self.paint('cyan', self.bl + self.hrule + self.hrule)} "
        )
        if right_text:
            right_plain = f" {right_text} {self.hrule * 3}{self.term}"
            right = f" {right_text} {self.paint('cyan', self.hrule * 3 + self.term)}"
        else:
            right_plain = f"{self.hrule * 3}{self.term}"
            right = self.paint("cyan", self.hrule * 3 + self.term)
        fill = self.width - display_width(left_plain) - display_width(right_plain)
        fill = max(fill, 4)
        return left + self.paint("cyan", self.hrule * fill) + right

    def vert(self) -> str:
        """A blank body spacer row — just the left rail."""
        return self.paint("dim", self.vert_g)

    # ── body components ─────────────────────────────────────────────────
    def section(self, label: str, count: int, *, color: str | None = None) -> str:
        rendered = self.paint(color, label) if color else label
        return (
            f"{self.paint('dim', self.branch + self.hrule)} "
            f"{rendered} {self.paint('dim', f'({count})')}"
        )

    def summary_line(self, text: str) -> str:
        return f"{self.paint('dim', self.branch + self.hrule)} {self.paint('dim', text)}"

    def leaf(
        self,
        name: str,
        *,
        detail: str = "",
        last: bool = False,
        name_col: int = 24,
    ) -> str:
        conn = self.last if last else self.branch
        trunc = self.truncate(name, name_col)
        pad = " " * max(name_col - display_width(trunc), 0)
        row = (
            f"{self.paint('dim', self.vert_g)}   "
            f"{self.paint('dim', conn + self.hrule)} {trunc}{pad}"
        )
        if detail:
            budget = self.width - display_width(row) - 2
            if budget > 4:
                row += f"  {self.paint('dim', self.truncate(detail, budget))}"
        return row

    def check_row(self, state: str, name: str, detail: str = "", *, name_col: int = 24) -> str:
        uni, asc, color = _CHECK.get(state, _CHECK["ok"])
        glyph = asc if self.ascii else uni
        trunc = self.truncate(name, name_col)
        pad = " " * max(name_col - display_width(trunc), 0)
        prefix = f"{self.paint('dim', self.vert_g)}   {self.paint(color, glyph)}  {trunc}{pad}"
        if detail:
            budget = self.width - display_width(prefix) - 1
            if budget > 8:
                detail = self.truncate(detail, budget)
                return f"{prefix} {self.paint('dim', detail)}"
        return prefix

    def alert_panel(self, severity: str, text: str) -> str:
        color = "red" if severity == "critical" else "orange"
        return (
            f"{self.paint('dim', self.vert_g)}   "
            f"{self.paint(color, self.alert_g)} {text}"
        )

    def health(self, state: str, text: str) -> str:
        color = _HEALTH_COLOR.get(state, "dim")
        return f"{self.paint(color, self.health_glyph(state))} {text}"


def ensure_utf8(stream: IO[str]) -> None:
    """Reconfigure a text stream to UTF-8 once, if it supports it.

    Windows defaults both stdout and stderr to the console codepage
    (cp1252/cp437), which cannot encode the box-drawing, bullet, and dash
    glyphs this module emits — they arrive as U+FFFD. The ASCII fallback
    exists for terminals that genuinely cannot display Unicode; a stream that
    merely defaults to a legacy codepage should be upgraded, not degraded.

    Idempotent and best-effort: a stream already at UTF-8 is left alone, and a
    replaced stream (pytest capture, StringIO) may lack `reconfigure` — neither
    is an error worth failing a command over.
    """
    encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
    if encoding in ("utf8", "utf8mb4"):
        return
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8")
    except (ValueError, OSError):  # pragma: no cover -- exotic stream
        pass


def emit_panel(lines: list[str], *, file: IO[str] | None = None) -> None:
    """Print pre-built panel lines to a stream (default stderr)."""
    out = file if file is not None else sys.stderr
    ensure_utf8(out)
    out.write("\n".join(lines) + "\n")
    out.flush()
