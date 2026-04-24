# claude-lb

[![Forma](https://img.shields.io/badge/forma-experimental-orange.svg)](https://github.com/forma-tools/forma)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.1.0-blue.svg)](CHANGELOG.md)

> Pick the healthiest Claude Code Max profile — health taxonomy + load balancer for local OAuth profiles.

## Why this exists

Claude Code Max users often run multiple accounts stored as
`~/.claude-profiles/<name>/.credentials.json`. Nothing built-in picks the
right one at dispatch time — spawning a session with a dead profile burns
attempts on 401s, rate-limit retries, or quota-exhausted accounts.

Existing tools (CLIProxyAPI, vibeproxy, TeamClaude, CCS) solve variants of
this at the **transparent HTTP proxy** level. `claude-lb` is different: it
is a **stateless one-shot CLI primitive** that exposes "which profile is
healthy right now?" as an exit-code-friendly scripting call.

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

### Scripting

```bash
profile=$(claude-lb pick 2>/dev/null)
case $? in
  0) export AXIOM_CLAUDE_PROFILE="$profile" ;;
  2) echo "All profiles need re-auth: claude login --profile <name>" ;;
  5) echo "No profile is currently ok — retry or relax --require-ok" ;;
  6) echo "All profiles throttled — back off" ;;
  9) echo "All profiles exhausted — operator intervention required" ;;
esac
```

## Health Taxonomy

Seven states, distinct signal sources, distinct next-actions.

| State | Detection | TTL | Next action |
|-------|-----------|-----|-------------|
| `ok` | HTTP 200 | 5 min | — |
| `rate_limited` | 429 + retry-after | `retry-after` s | Retry in N seconds |
| `session_limit` | 429 + session keywords | until session reset (5h) | Skip until reset |
| `weekly_limit` | 429 + weekly keywords | until Sunday 02:00 UTC | Consider plan upgrade |
| `auth_dead` | HTTP 401 | never (manual) | `claude login --profile <name>` |
| `network_error` | timeout / DNS / TLS / refused | 30 s | Transient; retry |
| `unknown` | anything else (5xx, etc.) | 60 s | Inspect with `show` |

Classification order is strict: network-level exception → 200 → 401 → 429
subtypes → 403 heuristic → unknown. See [`SPEC.md`](SPEC.md) §6 for the
full table and [`src/claude_lb/taxonomy.py`](src/claude_lb/taxonomy.py) for
the implementation.

## Picker Strategies

| Strategy | Behaviour |
|----------|-----------|
| `sticky` **(default)** | Stick to last-picked profile inside a 300 s window (cache-locality). Falls through to `least-used` otherwise. |
| `least-used` | Lowest `weekly_pct` wins. |
| `round-robin` | Rotate past the last-picked profile. |
| `weighted` | `weekly_pct / (session_pct + 1)` — lower is better. |
| `first-healthy` | First `ok` in discovery order. Deterministic for tests. |

Stickiness window can be overridden with `--stickiness <s>` or
`CLAUDE_LB_STICKINESS=<s>` in the environment. Set to `0` to disable.

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Unexpected error |
| 2 | `AUTH_REQUIRED` — all profiles `auth_dead` |
| 3 | `NOT_FOUND` — unknown profile given to `show` / `probe` / `invalidate` |
| 4 | `VALIDATION` — bad flag (e.g. unknown strategy) |
| 5 | `FORBIDDEN` — `--require-ok` but no profile is `ok` |
| 6 | `RATE_LIMITED` — all profiles throttled |
| 8 | `TIMEOUT` |
| 9 | `UNAVAILABLE` — no profiles, or all terminal-bad |

## Cache & Filesystem

| Purpose | Path |
|---------|------|
| Cache (Linux/macOS) | `~/.config/claude-lb/health.json` |
| Cache (Windows) | `%APPDATA%\claude-lb\health.json` |
| Pick log | `<config>/picks.log` (tab-separated, 10 MB rotation) |
| Last-pick state | `<config>/last-pick.json` |
| Profiles source | `~/.claude-profiles/<name>/.credentials.json` |

Cache writes are atomic (tempfile + `os.replace`). A credentials file's
`mtime` change implicitly invalidates its cache entry — so re-running
`claude login --profile <name>` automatically refreshes the classifier on
the next probe.

## Requirements

- Python 3.11+
- One or more OAuth profiles under `~/.claude-profiles/<name>/.credentials.json`
  (populated by `claude login --profile <name>`)
- Outbound HTTPS access to `api.anthropic.com`

## Recent Changes

### v0.1.0 (2026-04-24)

- Initial release.
- Seven-state health taxonomy with per-state TTL.
- Concurrent probing via `httpx.AsyncClient`.
- Five picker strategies plus default stickiness.
- Atomic cache writes, mtime-based implicit invalidation.
- JSON envelope `{data, meta}` on every command.
- Semantic exit codes (0, 1, 2, 3, 4, 5, 6, 8, 9).

[Full changelog](CHANGELOG.md)

## Non-goals

- **Not a proxy.** Doesn't sit in the request path. Use
  [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) if you want that.
- **Not an authenticator.** Reads credentials `claude login` already placed
  on disk. Token refresh is out of scope for v0.1.
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
