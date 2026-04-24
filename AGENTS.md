# claude-lb — AI Assistant Guide

Context for AI coding assistants working on this repo and for agents
invoking `claude-lb` as part of a Claude Code spawn pre-flight.

## What this is

A one-shot stateless CLI that answers "which Anthropic Max profile is
healthy right now?" It reads local OAuth credentials from
`~/.claude-profiles/<name>/.credentials.json`, probes
`GET /api/oauth/usage` (with `anthropic-beta: oauth-2025-04-20`),
classifies the response into eight health states, caches results, and
picks the best profile for downstream scripts. It can also exchange
stored refresh tokens for fresh access tokens via
`claude-lb refresh <name>`.

## Command reference

| Command | Purpose |
|---------|---------|
| `claude-lb list` | Enumerate discovered profiles |
| `claude-lb probe [<name>]` | Live probe all profiles or one |
| `claude-lb status` | Cached health table + stdout summary |
| `claude-lb show <name>` | One profile's full health detail |
| `claude-lb pick [--strategy S]` | Return the best profile name (exit 0) |
| `claude-lb refresh <name> \| --all \| --expired` | OAuth refresh, rewrites `.credentials.json` |
| `claude-lb invalidate <name>` | Drop cache entry for re-probe |
| `claude-lb doctor` | Diagnose local setup (dirs, creds, cache, network) |
| `claude-lb update` | Version + upstream ahead/behind check |

Every command accepts `--json` and returns a `{data, meta}` envelope.

## Common operations

```bash
# Pre-flight for spawning a `claude` subprocess
claude-lb refresh --expired 2>/dev/null
profile=$(claude-lb pick 2>/dev/null) && \
  AXIOM_CLAUDE_PROFILE=$profile claude ...

# Machine-readable status for dashboards
claude-lb status --json | jq '.meta'

# Diagnose a single misbehaving profile
claude-lb show account-a --json
```

## Authentication

`claude-lb` does not create profiles — it consumes OAuth credentials
that `claude login --profile <name>` has already placed on disk. Token
lifecycle:

- **Create/rotate identity:** `claude login --profile <name>` (browser flow)
- **Extend access token:** `claude-lb refresh <name>` (headless, uses stored refresh token)
- **Detect staleness without network:** `Profile.access_token_expires_at` + 8th state `auth_expired`

`claude-lb` itself has no login/logout commands and stores no credentials of its own.

## Agent rules

Invariants an agent cannot intuit from `--help`:

1. **Don't break the `pick` contract.** `claude-lb pick` emits one profile name, newline-terminated, to stdout and nothing else. Every downstream script depends on this.
2. **Always check exit codes before acting on stdout.** Exit 2/5/6/9 mean stdout is either empty or contains an error envelope (when `--json`). See [README.md](README.md) exit-code table.
3. **Always use `--json` when parsing output programmatically.** The human text tables on stderr are not a stable contract; the JSON envelope is.
4. **Try `claude-lb refresh --expired` before concluding a profile is dead.** Exit 2 from `pick` is ambiguous between `auth_dead` (refresh token gone) and `auth_expired` (fixable with one refresh call).
5. **Never widen scope to other providers.** Anthropic / Claude Code Max only. OpenAI, Gemini, etc. are out of scope.
6. **Never add a daemon, proxy, or persistent service.** This is a stateless CLI. Continuous behaviour belongs in a separate tool.
7. **Never transmit credentials off-device.** Tokens are read from local disk, used for exactly one outbound request per probe/refresh, and never logged at default verbosity.
8. **`stdout` is sacred.** Only data or a single profile name goes to stdout. Tables, progress, warnings, colors → stderr.
9. **Classification order matters.** Network exception → local-expired → 200 (+ utilization check) → 401 → 403 (scope check) → 429 → unknown. Preserve this in `src/claude_lb/taxonomy.py`.
10. **Utilization thresholds are the signal** for session/weekly limits — not 429 message keywords. `utilization >= 100` = exhausted. Don't reintroduce keyword matching.
11. **Atomic writes only.** Both `health.json` (cache) and `.credentials.json` (refresh) use `tempfile` + `os.replace`. Never write directly.
12. **Token-shape fallback matters.** For reading: try `claudeAiOauth.accessToken`, then `oauthAccessToken`, then `accessToken`. For writing (refresh): modern shape only.
13. **Stickiness is the default.** Don't change the default strategy to `round-robin` or similar without explicit operator approval — it thrashes Anthropic's per-account prompt cache.
14. **Refresh doesn't need `--dry-run`** because failed refreshes never mutate `.credentials.json` (tempfile-only writes — the server-side rejection is detected before rename). But warn if you're designing auto-refresh: two parallel `claude-lb pick` invocations both consuming the same refresh token is a real race.

**Prompt injection:** not applicable. `claude-lb` returns only its own telemetry — profile names, utilization numbers, timestamps — never user-authored content from Anthropic's API.

## Code layout

| File | Responsibility |
|------|---------------|
| `src/claude_lb/cli.py` | Typer entry point, command dispatch, exit-code mapping |
| `src/claude_lb/discovery.py` | Walk `~/.claude-profiles/`, read credentials, extract token + expiresAt |
| `src/claude_lb/probe.py` | Async httpx probe against `/api/oauth/usage`; local-expired short-circuit |
| `src/claude_lb/taxonomy.py` | 8-state classifier (the core of the tool) |
| `src/claude_lb/refresh.py` | OAuth refresh grant client; atomic credentials rewrite |
| `src/claude_lb/cache.py` | Atomic read/write of `health.json`, TTL logic |
| `src/claude_lb/pick.py` | Strategies, stickiness, filter ladder, pick log |
| `src/claude_lb/output.py` | JSON envelope, stream separation, status table |
| `src/claude_lb/doctor.py` | Local-setup diagnostics |
| `src/claude_lb/updater.py` | Version + upstream check |
| `src/claude_lb/paths.py` | Platform-aware paths (config, cache, pick log) |
| `src/claude_lb/models.py` | Pydantic models shared across modules |

## Testing

- `tests/fixtures/oauth-usage/` — captured + synthetic `/api/oauth/usage` bodies (ok, session-exhausted, weekly-exhausted, both-exhausted, 403-scope-missing). Classifier must correctly label each.
- `tests/fixtures/401-auth-error.json` — auth-dead fixture.
- `tests/test_refresh.py` — uses `respx` to mock `/v1/oauth/token`, asserts atomic credentials rewrite and preserves non-oauth fields on success; verifies failed refreshes never clobber credentials.
- Run: `uv run pytest` (or `pytest` inside the venv).
- Coverage target: 90%+ on `taxonomy.py`, `discovery.py`, `pick.py`, `refresh.py`.

## Forma protocol compliance

This tool adheres to Forma Protocol v1.4. See [SPEC.md](SPEC.md) for the full mapping. Key points:

- `{data, meta}` JSON envelope
- Semantic exit codes (0–9)
- `stdout` = data, `stderr` = humans
- `--json` on every command
