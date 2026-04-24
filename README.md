# claude-lb

[![Forma](https://img.shields.io/badge/forma-experimental-orange.svg)](https://github.com/forma-tools/forma)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.3.0-blue.svg)](CHANGELOG.md)

> Pick the healthiest Claude Code Max profile — health taxonomy + load balancer for local OAuth profiles.

## Why this exists

Claude Code Max users often run multiple accounts stored as
`~/.claude-profiles/<name>/.credentials.json`. Nothing built-in picks the
right one at dispatch time — spawning a session with an expired, dead, or
quota-exhausted profile burns attempts on 401s and rate-limit retries.

`claude-lb` is a **stateless one-shot CLI primitive** that answers
"which profile is healthy right now?" as an exit-code-friendly scripting call,
reading utilization numbers straight from Anthropic's OAuth usage endpoint.

```bash
AXIOM_CLAUDE_PROFILE=$(claude-lb pick) axiom queue enqueue ...
```

## Install

```bash
uv tool install --editable .
```

## Quick Start

```bash
claude-lb probe                          # Live-probe all profiles
claude-lb status                         # Cached health table
claude-lb pick                           # → account-a  (one profile name, exit 0)
claude-lb refresh --expired              # Refresh any profile whose token expired
eval "$(claude-lb pick --export)"        # AXIOM_CLAUDE_PROFILE=account-a in shell
claude-lb status --json | jq '.meta'     # Machine-readable summary
```

## Commands

### Discovery

```bash
claude-lb list                           # Names of discovered profiles
claude-lb list --json                    # {data: [...], meta: {count}}
```

### Health

```bash
claude-lb probe                          # Live-probe all (concurrent)
claude-lb probe account-a                   # Live-probe one
claude-lb status                         # Cached table (stderr) + summary (stdout)
claude-lb status --no-cache              # Ignore cache; full re-probe
claude-lb show account-a                    # Detail for one profile
claude-lb invalidate account-a              # Drop this profile's cache entry
```

### Picking

```bash
claude-lb pick                           # Default: sticky (cache-locality)
claude-lb pick --strategy least-used     # Lowest weekly % wins
claude-lb pick --strategy round-robin    # Rotate through profiles
claude-lb pick --strategy weighted       # Combine weekly + session usage
claude-lb pick --require-ok              # Exit 5 if none are ok
claude-lb pick --export                  # Shell-sourceable VAR=value
claude-lb pick --json                    # {data: {name, health, rationale}}
```

### Refresh

```bash
claude-lb refresh account-a                 # Refresh one profile's OAuth token
claude-lb refresh --all                  # Refresh every discovered profile
claude-lb refresh --expired              # Refresh only past-expiry profiles
claude-lb refresh --expired --json       # Machine-readable output
```

`refresh` POSTs the stored refresh token to Anthropic's OAuth endpoint,
atomically rewrites `.credentials.json` (preserving non-oauth fields), and
invalidates the health cache. A cron hook like
`0 * * * * claude-lb refresh --expired --json` keeps the fleet warm.

### Scripting

```bash
# Preferred pre-flight: refresh expired tokens, then pick
claude-lb refresh --expired 2>/dev/null
profile=$(claude-lb pick 2>/dev/null)
case $? in
  0) export AXIOM_CLAUDE_PROFILE="$profile" ;;
  2) echo "All profiles dead or expired: claude-lb refresh --expired or claude login" ;;
  5) echo "No profile is currently ok — retry or relax --require-ok" ;;
  6) echo "All profiles throttled — back off" ;;
  9) echo "All profiles exhausted — operator intervention required" ;;
esac
```

## Health Taxonomy

Eight states. Signals come from a single probe against `/api/oauth/usage`
plus a local check of the stored token's `expiresAt`.

| State | Detection | TTL | Next action |
|-------|-----------|-----|-------------|
| `ok` | HTTP 200 with `five_hour < 100` and `seven_day < 100`; also HTTP 403 "scope requirement user:profile" | 5 min | — |
| `rate_limited` | 429 on usage endpoint | `retry-after` s, else 60 s | Retry in N seconds |
| `session_limit` | HTTP 200 with `five_hour.utilization >= 100` | until `five_hour.resets_at` | Skip until session reset |
| `weekly_limit` | HTTP 200 with `seven_day.utilization >= 100` | until `seven_day.resets_at` | Skip until weekly reset |
| `auth_expired` | Local: stored `expiresAt` is past (no network call) | invalidates on refresh | `claude-lb refresh <name>` |
| `auth_dead` | HTTP 401 on usage endpoint | manual | `claude login --profile <name>` |
| `network_error` | timeout / DNS / TLS / refused | 30 s | Transient; retry |
| `unknown` | other 4xx/5xx, malformed body | 60 s | Inspect with `show` |

Classification order: network exception → local-expiry → 200+utilization → 401 → 403+scope-check → 429 → unknown. See [`SPEC.md`](SPEC.md) §6 + [`src/claude_lb/taxonomy.py`](src/claude_lb/taxonomy.py).

## Picker Strategies

| Strategy | Behaviour |
|----------|-----------|
| `sticky` **(default)** | Stick to last-picked profile inside a 300 s window (cache-locality). Falls through to `least-used` otherwise. |
| `least-used` | Lowest `weekly_pct` wins. |
| `round-robin` | Rotate past the last-picked profile. |
| `weighted` | `weekly_pct / (session_pct + 1)` — lower is better. |
| `first-healthy` | First `ok` in discovery order. Deterministic for tests. |

Stickiness window: `--stickiness <s>` or `CLAUDE_LB_STICKINESS=<s>`. Set to `0` to disable.

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Unexpected error |
| 2 | `AUTH_REQUIRED` — all profiles `auth_dead` or `auth_expired`; run `claude-lb refresh --expired` or `claude login` |
| 3 | `NOT_FOUND` — unknown profile given to `show` / `probe` / `refresh` / `invalidate` |
| 4 | `VALIDATION` — bad flag (e.g. unknown strategy, or `refresh` with no args) |
| 5 | `FORBIDDEN` — `--require-ok` but no profile is `ok` |
| 6 | `RATE_LIMITED` — all profiles throttled |
| 8 | `TIMEOUT` |
| 9 | `UNAVAILABLE` — no profiles, or all terminal-bad |

## Environment Variables

| Variable | Default | Effect |
|----------|---------|--------|
| `CLAUDE_LB_STICKINESS` | `300` | Stickiness window in seconds. `0` disables. Overridden by `--stickiness <s>`. |
| `CLAUDE_LB_PROFILES_DIR` | `~/.claude-profiles` | Absolute path to the profiles tree. Useful for testing or multi-tenant setups. |
| `CLAUDE_CONFIG_DIR` | unset | Single-profile fallback: if set to a dir containing a direct `.credentials.json`, loaded as profile `default`. |
| `XDG_CONFIG_HOME` | unset | Linux/macOS: overrides `~/.config/claude-lb/`. Windows uses `%APPDATA%\claude-lb\`. |

## Diagnostics

```bash
claude-lb doctor                 # Check config dir, profiles, credentials, cache, network
claude-lb doctor --skip-network  # Offline variant
claude-lb doctor --json          # Machine-readable

claude-lb update                 # Version + git-upstream ahead/behind check
claude-lb update --json          # meta.update_available tells you if behind
```

## Cache & Filesystem

| Purpose | Path |
|---------|------|
| Cache (Linux/macOS) | `~/.config/claude-lb/health.json` |
| Cache (Windows) | `%APPDATA%\claude-lb\health.json` |
| Pick log | `<config>/picks.log` (tab-separated, 10 MB rotation) |
| Last-pick state | `<config>/last-pick.json` |
| Profiles source | `~/.claude-profiles/<name>/.credentials.json` |

Cache writes are atomic (tempfile + `os.replace`). A credentials file's
`mtime` change implicitly invalidates its cache entry — so `claude login` or
`claude-lb refresh` automatically refreshes the classifier on the next probe.

## Requirements

- Python 3.11+
- One or more OAuth profiles under `~/.claude-profiles/<name>/.credentials.json`
  (populated by `claude login --profile <name>`)
- Outbound HTTPS access to `api.anthropic.com`

## Recent Changes

### v0.3.0 (2026-04-24)

- Added `claude-lb refresh <name> | --all | --expired` for explicit OAuth token refresh
- Added 8th health state `auth_expired` — detected locally from `expiresAt` with no network call
- Atomic `.credentials.json` rewrite preserving non-oauth fields; health cache invalidated on refresh

### v0.2.0 (2026-04-24)

- Swapped probe endpoint `/v1/models` → `/api/oauth/usage` (the `/v1/*` path rejects OAuth tokens)
- Taxonomy derives `session_limit` / `weekly_limit` from real utilization numbers, not 429 keywords
- Reset timestamps come from response body, not "next Sunday" heuristics
- Populated `usage.session_pct` / `weekly_pct` / `sonnet_pct` / `opus_pct` on every healthy probe
- Retired `patterns.py` (429 keyword lists)

### v0.1.0 (2026-04-24)

- Initial release — seven-state health taxonomy, five picker strategies, semantic exit codes, atomic cache writes

[Full changelog](CHANGELOG.md)

## Non-goals

- **Not a proxy.** Doesn't sit in the request path. Use
  [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) if you want that.
- **Not an interactive authenticator.** `claude login` creates profiles;
  `claude-lb refresh` extends their lifetime — but the browser-based OAuth
  flow is owned by the `claude` CLI.
- **Not multi-provider.** Anthropic / Claude Code Max only.
- **Not persistent.** No daemon, no watchdog. Every invocation is one-shot.

## Forma Protocol

This tool follows the [Forma Protocol](https://github.com/forma-tools/forma):

- `--json` on every command with `{data, meta}` envelope
- Semantic exit codes (0–9)
- `stdout` = data only; `stderr` = tables, progress, warnings
- `[tool.forma]` metadata in [`pyproject.toml`](pyproject.toml)
- See [`SPEC.md`](SPEC.md) for the full specification

## License

MIT — see [LICENSE](LICENSE).
