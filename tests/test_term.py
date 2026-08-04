"""Tests for the terminal-panel renderer (claude_lb.term)."""

from __future__ import annotations

import pytest

from claude_lb.term import Term, display_width, emit_panel

# A no-color, unicode, fixed-width terminal — deterministic for assertions.
COLOR_OFF = dict(color=False, ascii_mode=False, width=80)


def _term(**over) -> Term:
    cfg = {**COLOR_OFF, **over}
    return Term(**cfg)


# ─── display_width ──────────────────────────────────────────────────────────


def test_display_width_ascii() -> None:
    assert display_width("hello") == 5


def test_display_width_strips_ansi() -> None:
    assert display_width("\033[32mgreen\033[0m") == 5


def test_display_width_emoji_is_two_cells() -> None:
    # Rooster brand emoji renders as a wide (2-cell) glyph.
    assert display_width("🐓") == 2


def test_display_width_variation_selector_is_zero() -> None:
    # ⚠️ = warning sign + VS16; the selector adds no width.
    assert display_width("a️b") == 2


# ─── color + ascii toggles ──────────────────────────────────────────────────


def test_paint_off_returns_plain() -> None:
    t = _term()
    assert t.paint("green", "x") == "x"


def test_paint_on_wraps_ansi() -> None:
    t = _term(color=True)
    out = t.paint("green", "x")
    assert out.startswith("\033[32m") and out.endswith("\033[0m")


def test_no_color_env_disables_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    t = Term(width=80)  # color auto-detected
    assert t.color_on is False


def test_force_color_env_enables_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)
    t = Term(width=80)
    assert t.color_on is True


def test_ascii_mode_swaps_glyphs() -> None:
    t = _term(ascii_mode=True)
    assert t.tl == "+" and t.hrule == "-" and t.term == "*"
    assert t.brand_glyph("roost") == "[R]"


def test_term_ascii_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM_ASCII", "1")
    t = Term(width=80, color=False)
    assert t.ascii is True


# ─── truncate ───────────────────────────────────────────────────────────────


def test_truncate_short_passthrough() -> None:
    t = _term()
    assert t.truncate("abc", 10) == "abc"


def test_truncate_appends_ellipsis() -> None:
    t = _term()
    out = t.truncate("abcdefghij", 5)
    assert out.endswith("…")
    assert display_width(out) <= 5


def test_truncate_ascii_ellipsis() -> None:
    t = _term(ascii_mode=True)
    out = t.truncate("abcdefghij", 5)
    assert out.endswith("..")


# ─── panel chrome ───────────────────────────────────────────────────────────


def test_panel_open_exact_width_with_indicator() -> None:
    t = _term()
    line = t.panel_open("roost", "roost · pick", indicator="least-used")
    assert display_width(line) == 80
    assert line.startswith("╭──")
    assert line.endswith("●")
    assert "least-used" in line


def test_panel_open_exact_width_no_indicator() -> None:
    t = _term()
    line = t.panel_open("roost", "roost · doctor")
    assert display_width(line) == 80


def test_panel_close_exact_width() -> None:
    t = _term()
    line = t.panel_close(left_text="pick decision", right_text="• ok")
    assert display_width(line) == 80
    assert line.startswith("╰──")
    assert line.endswith("●")


def test_panel_open_narrow_keeps_min_fill() -> None:
    t = _term(width=20)
    line = t.panel_open("roost", "a-very-long-name-here", indicator="x")
    # Fill clamps to >= 4; the line may exceed the requested width but never
    # collapses the rule entirely.
    assert "────" in line or "----" in line


# ─── body components ────────────────────────────────────────────────────────


def test_section_label_and_count() -> None:
    t = _term()
    out = t.section("CHOSEN", 1, color="green")
    assert "├─" in out and "CHOSEN" in out and "(1)" in out


def test_leaf_branch_vs_last() -> None:
    t = _term()
    assert "├─" in t.leaf("foo", last=False)
    assert "└─" in t.leaf("foo", last=True)


def test_leaf_detail_rendered() -> None:
    t = _term()
    assert "score=10.00" in t.leaf("acct", detail="score=10.00")


def test_leaf_truncates_long_name() -> None:
    t = _term()
    out = t.leaf("x" * 60, last=True)
    assert "…" in out


def test_check_row_states() -> None:
    t = _term()
    assert "✓" in t.check_row("ok", "thing")
    assert "▲" in t.check_row("warn", "thing")
    assert "✗" in t.check_row("fail", "thing")


def test_check_row_ascii_states() -> None:
    t = _term(ascii_mode=True)
    assert t.check_row("ok", "x").lstrip().startswith("|")
    assert "+" in t.check_row("ok", "x")
    assert "x" in t.check_row("fail", "y")


def test_check_row_truncates_long_detail() -> None:
    t = _term()
    out = t.check_row("ok", "name", "z" * 200)
    assert display_width(out) <= 80


def test_alert_panel_severity_glyph() -> None:
    t = _term()
    assert "▲" in t.alert_panel("critical", "boom")
    assert "boom" in t.alert_panel("warning", "boom")


def test_health_bullet() -> None:
    t = _term()
    assert t.health("healthy", "all clear").startswith("•")
    assert "all clear" in t.health("healthy", "all clear")


def test_health_busted_large_glyph() -> None:
    t = _term()
    assert t.health("busted", "down").startswith("⬤")


def test_vert_is_rail() -> None:
    t = _term()
    assert t.vert() == "│"


# ─── emit_panel ─────────────────────────────────────────────────────────────


def test_emit_panel_writes_lines(capsys: pytest.CaptureFixture[str]) -> None:
    emit_panel(["one", "two"])
    err = capsys.readouterr().err
    assert err == "one\ntwo\n"
