# roost — session memory

Auto-loaded by `/sync`. Notes that should survive across sessions.

## Standing operator directives

- **No `git push` without explicit approval.** Local commits, branches, and tags are fine. Pushing requires the operator to say so for THIS push — prior approvals don't carry forward. See AGENTS.md rule 19 for the canonical statement.
- **Use `uv` (not `pip`) for everything.** Editable install: `uv tool install --reinstall --editable "X:/Forge/claude-lb"`.
- **Run reinstalls from a non-roost shell on Windows.** The tool can't overwrite its own `.pyd` files while running. `roost update --apply` detects this and prints the workaround; don't try to "force" past it.

## Current state (last update: 2026-04-27)

- **Version:** v0.4.0 on `main`. Released 2026-04-27 with the Tier 1–4 batch (symmetric API, observability, integration surfaces, reliability).
- **Remote:** `origin` = https://github.com/0xDarkMatter/roost (private). Tags pushed: v0.1.0, v0.2.0, v0.3.0, v0.4.0, plus `hackathon-submission` (annotated, points at the v0.3.0 squash commit).
- **Tests:** 723 mocked + 28 live = **751 total** (`uv run pytest` for the mocked suite, `uv run pytest -m live` for the integration suite). All green at last check.
- **Live fleet:** 3 profiles under `~/.claude-profiles/` — `account-a`, `account-b`, `account-c`. All `max` plan. Variety of overage/usage states — useful as a real-world testbed for `pytest -m live`.
- **v0.3.0 highlights:** platform-status awareness — `status` and `doctor` consult `https://status.claude.com/api/v2/summary.json`, surfaced as a one-line stderr header above the `status` table when there's a non-resolved incident. Cache at `<config>/platform-status.json`, 60s TTL with stale-fallback. Shared module `src/claude_lb/platform_status.py`. AGENTS.md rule 20 codifies "best-effort, never load-bearing".

### [Unreleased] Tier 1–4 batch (2026-04-27)

Built and tested in one session. Six phases, ~28 new mocked tests per
phase + 28 live tests. CHANGELOG `[Unreleased]` has the complete list.
Highlights worth remembering:

- **Symmetric API**: `roost remove`, `roost rename`, `roost which` (read-only `pick`).
- **Pick filters**: `--avoid` (repeatable), `--max-cost <pct>`, `--fallback <name>`, `--explain` (decision tree to stderr or fold into JSON `data.explain`).
- **New strategy**: `lowest-overage` — minimum monthly overage utilization wins. Profiles without overage data treat as 0 (best).
- **Observability**: opt-in `usage-log.ndjson` (toggle via `roost config usage-log on` or `CLAUDE_LB_USAGE_LOG=1`). `roost stats` aggregates `picks.log`; `roost report` aggregates the usage log with sparklines + linear burn-rate projection.
- **Reliability**: per-profile exponential `network_error` backoff (30s → 60s → 120s → 240s → 480s, capped). New `ProfileHealth.consecutive_failures` field — Pydantic default 0 keeps backwards-compat with v0.3.0 cache files. `refresh --jitter <s>` for cron-spread.
- **Integration surfaces**: `roost shellinit` (bash/zsh/fish/pwsh templates), `roost trace` (verbose probe with token redaction), `roost top` (Rich `Live` TUI).
- **Live tests**: `tests/test_live.py` marked `pytest.mark.live`, default-skipped via `addopts = "-m 'not live'"`. Run explicitly with `pytest -m live`.
- **AGENTS.md** gained 6 new rules (21–26) covering the new surfaces — read these before extending any of the new features.

## Known traps that already cost time

1. **Auto-refresh stale-Profile bug** (fix in `_attempt_auto_refresh`, commit `ae03041`): after `refresh_many_sync` mutates disk, you MUST re-discover the Profile before re-probing — otherwise `_local_auth_expired` short-circuits on the stale `access_token_expires_at` and silently undoes the refresh. Tests that stub `probe_many_sync` at the top level miss this; the regression test (`test_auto_refresh_rediscovers_profile_after_refresh`) seeds the disk and asserts the second probe sees a post-refresh mtime.
2. **Windows EACCES on self-upgrade** (handled): `roost update --apply` from within the uv-tool install can't overwrite its own mapped `.pyd` files. `updater._would_self_lock()` detects + emits the workaround. Never bypass.
3. **CI workflow file** removed (commit `1a700c9`): pushing the workflow needs the gh token's `workflow` scope. Don't re-add `.github/workflows/` until CI is actually wanted.

## Useful one-liners

```bash
# Reinstall (run from any shell that ISN'T roost)
uv tool install --reinstall --editable "X:/Forge/claude-lb"

# Full mocked test suite (default: 723 tests, live skipped)
uv run pytest

# Live integration tests (28 tests against real fleet)
uv run pytest -m live

# Spot-check the live install
roost status --json | jq '.data[] | {name, health, weekly_pct: .usage.weekly_pct}'
roost history --tail 10
roost refresh --soon 1h --json

# New observability stack
roost config usage-log on            # one-time opt-in
roost probe                          # appends to usage-log.ndjson
roost report --metric weekly_pct --sparkline --project
roost stats --since 1d

# New decision-debugging
roost which --explain                # what would pick choose, why?
roost pick --avoid account-a --fallback account-c

# New shell integration
eval "$(roost shellinit)"           # bash/zsh; fish/pwsh have own pipe forms
```

## Deferred ideas (not yet wanted)

Most of the original deferred list shipped in the [Unreleased] batch. Still
worth not building without a concrete driver:

- `exec --concurrent N` (compose with `pick --count` for parallel dispatch)
- Detached-process pattern for Windows `update --apply` (current "print workaround" is fine)
- `--raw` schema diff (warn on unknown response fields from Anthropic)
- PyPI publish (still install-from-clone only; pyproject ready)
- Standalone binary via pyinstaller (separate distribution decision)
- `cli.py` refactor — currently ~2900 lines; works fine, would only be hygiene
- Snapshot tests for Rich tables (cosmetic-regression coverage)

Add when there's a concrete driver, not before.
