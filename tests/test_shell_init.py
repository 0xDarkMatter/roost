"""Tests for shell_init: shell detection, template rendering, snapshot."""

from __future__ import annotations

import pytest

from claude_lb.shell_init import (
    SUPPORTED_SHELLS,
    detect_shell,
    template_for,
)

# ---------------------------------------------------------------------------
# detect_shell
# ---------------------------------------------------------------------------


def test_detect_shell_bash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.delenv("PSModulePath", raising=False)
    assert detect_shell() == "bash"


def test_detect_shell_zsh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELL", "/usr/local/bin/zsh")
    monkeypatch.delenv("PSModulePath", raising=False)
    assert detect_shell() == "zsh"


def test_detect_shell_fish(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELL", "/usr/bin/fish")
    monkeypatch.delenv("PSModulePath", raising=False)
    assert detect_shell() == "fish"


def test_detect_shell_pwsh_via_psmodulepath(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SHELL", raising=False)
    monkeypatch.setenv("PSModulePath", "C:\\Users\\me\\Documents\\PowerShell")
    assert detect_shell() == "pwsh"


def test_detect_shell_falls_back_to_bash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SHELL", raising=False)
    monkeypatch.delenv("PSModulePath", raising=False)
    assert detect_shell() == "bash"


def test_detect_shell_unknown_shell_falls_back_to_bash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shell binary we don't recognise (e.g. /bin/csh) shouldn't crash."""
    monkeypatch.setenv("SHELL", "/bin/csh")
    monkeypatch.delenv("PSModulePath", raising=False)
    assert detect_shell() == "bash"


# ---------------------------------------------------------------------------
# template_for
# ---------------------------------------------------------------------------


def test_template_bash_contains_function_definition() -> None:
    out = template_for("bash")
    assert "claude()" in out
    assert "roost exec --auto-refresh -- claude" in out
    assert '"$@"' in out


def test_template_zsh_uses_same_template_as_bash() -> None:
    """Bash and zsh share syntax for our use case."""
    assert template_for("bash") == template_for("zsh")


def test_template_fish_uses_fish_syntax() -> None:
    out = template_for("fish")
    assert "function claude" in out
    assert "$argv" in out
    assert "end" in out


def test_template_pwsh_uses_pwsh_syntax() -> None:
    out = template_for("pwsh")
    assert "function claude {" in out
    assert "@args" in out
    assert "& roost exec" in out


def test_template_unknown_shell_raises() -> None:
    with pytest.raises(ValueError, match="unknown shell"):
        template_for("ksh")


def test_template_case_insensitive() -> None:
    """`--shell BASH` should work just like `--shell bash`."""
    assert template_for("BASH") == template_for("bash")
    assert template_for("FISH") == template_for("fish")


def test_supported_shells_constant_is_complete() -> None:
    """Sanity: every shell in SUPPORTED_SHELLS must have a template."""
    for shell in SUPPORTED_SHELLS:
        out = template_for(shell)
        assert out  # non-empty
