# claude-lb — session memory

Auto-loaded by `/sync`. Notes that should survive across sessions.

## Standing operator directives

- **No `git push` without explicit approval.** Local commits, branches, and tags are fine. Pushing requires the operator to say so for THIS push — prior approvals don't carry forward. See AGENTS.md rule 19 for the canonical statement.
- **Use `uv` (not `pip`) for everything.** Editable install: `uv tool install --reinstall --editable "X:/Forge/claude-lb"`.
- **Run reinstalls from a non-claude-lb shell on Windows.** The tool can't overwrite its own `.pyd` files while running. `claude-lb update --apply` detects this and prints the workaround; don't try to "force" past it.

## Current state (last update: 2026-04-25)

- **Version:** v0.8.0 on `main`.
- **Remote:** `origin` = https://github.com/0xDarkMatter/claude-lb (private). v0.5.0 / v0.6.0 / v0.7.0 / v0.8.0 tags pushed.
- **Tests:** 328 passing (`uv run pytest`). Doctor now covers `exec_cmd` and `platform_status` in its `subcommand_imports` check.
- **Live fleet:** 3 profiles under `~/.claude-profiles/` — `account-a`, `account-b`, `account-c`. All `max` plan. Variety of overage/usage states — useful as a real-world testbed.
- **v0.8.0 highlights:** platform-status awareness — `status` and `doctor` consult `https://status.claude.com/api/v2/summary.json`, surfaced as a one-line stderr header above the `status` table when there's a non-resolved incident. Cache at `<config>/platform-status.json`, 60s TTL with stale-fallback. Shared module `src/claude_lb/platform_status.py`. AGENTS.md rule 20 codifies "best-effort, never load-bearing".

## Known traps that already cost time

1. **Auto-refresh stale-Profile bug** (fix in `_attempt_auto_refresh`, commit `ae03041`): after `refresh_many_sync` mutates disk, you MUST re-discover the Profile before re-probing — otherwise `_local_auth_expired` short-circuits on the stale `access_token_expires_at` and silently undoes the refresh. Tests that stub `probe_many_sync` at the top level miss this; the regression test (`test_auto_refresh_rediscovers_profile_after_refresh`) seeds the disk and asserts the second probe sees a post-refresh mtime.
2. **Windows EACCES on self-upgrade** (handled): `claude-lb update --apply` from within the uv-tool install can't overwrite its own mapped `.pyd` files. `updater._would_self_lock()` detects + emits the workaround. Never bypass.
3. **CI workflow file** removed (commit `1a700c9`): pushing the workflow needs the gh token's `workflow` scope. Don't re-add `.github/workflows/` until CI is actually wanted.

## Useful one-liners

```bash
# Reinstall (run from any shell that ISN'T claude-lb)
uv tool install --reinstall --editable "X:/Forge/claude-lb"

# Full test suite
uv run pytest

# Spot-check the live install
claude-lb status --json | jq '.data[] | {name, health, weekly_pct: .usage.weekly_pct}'
claude-lb history --tail 10
claude-lb refresh --soon 1h --json
```

## Deferred ideas (not yet wanted)

- `claude-lb watch` (TUI live status, ~30 lines with Rich Live)
- `exec --concurrent N` (compose with `pick --count` for parallel dispatch)
- Detached-process pattern for Windows `update --apply` (current "print workaround" is fine for v1)
- `--raw` schema diff (warn on unknown response fields from Anthropic)
- `pick --max-cost` (skip profiles near monthly overage cap)

Add when there's a concrete driver, not before.
