# claude-lb

[![Forma](https://img.shields.io/badge/forma-experimental-orange.svg)](https://github.com/forma-tools/forma)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.7.0-blue.svg)](CHANGELOG.md)

> Pick the healthiest Claude Code OAuth profile — health taxonomy + load balancer for local OAuth profiles. Works with any plan (Max / Pro / Team), with richer usage data on Max.

## Why this exists

If you run more than one Claude Code account — for redundancy, quota
spreading, or just personal-vs-work separation — there's nothing built-in
that picks the right one at dispatch time. Spawning against an expired,
dead, or quota-exhausted profile burns attempts on 401s and rate-limit
retries.

`claude-lb` is a **stateless one-shot CLI primitive** that answers
"which of my profiles is healthy right now?" as an exit-code-friendly
scripting call, reading utilization numbers straight from Anthropic's
OAuth usage endpoint.

```bash
AXIOM_CLAUDE_PROFILE=$(claude-lb pick) my-script ...
```

It works with any Claude Code OAuth account (Max / Pro / Team). On Max
plans you get the full session/weekly utilization picture; on Pro/Team
the tool still classifies health correctly (network / auth / rate-limit
states) but `usage` data is null.

## Install

```bash
# From PyPI (when published) or direct from a local clone:
uv tool install --editable .
```

## Setup

`claude-lb` needs more than one OAuth profile on disk to balance between
them. The standard `claude` CLI writes a single profile to
`~/.claude/.credentials.json`. `claude-lb add` imports that file into a
multi-profile layout under `~/.claude-profiles/<name>/.credentials.json`,
which is what `claude-lb` discovers and rotates between.

Typical onboarding:

```bash
# 1. Sign in to your first account (uses the standard Claude Code flow)
claude login

# 2. Import that credentials file as a named profile
claude-lb add personal

# 3. Sign in to a different account (overwrites ~/.claude/.credentials.json)
claude logout && claude login

# 4. Import the new state as another named profile
claude-lb add work

# 5. Confirm both are visible and ready
claude-lb status
```

`claude-lb add` copies (doesn't move) the source file, so the standard
`claude` command continues to work against whichever account you most
recently logged into.

**Custom locations:**

| Variable | Purpose |
|----------|---------|
| `CLAUDE_LB_PROFILES_DIR` | Override the profiles tree (default `~/.claude-profiles`) |
| `CLAUDE_CONFIG_DIR` | Single-profile fallback: if set to a dir with a direct `.credentials.json`, loaded as profile `default` |

## Quick Start

```bash
claude-lb probe                          # Live-probe all profiles
claude-lb status                         # Cached health table
claude-lb pick                           # → personal  (one profile name, exit 0)
claude-lb refresh --expired              # Refresh any profile whose token expired
eval "$(claude-lb pick --export)"        # AXIOM_CLAUDE_PROFILE=personal in shell
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
claude-lb pick --warn-at 80              # Stderr warning when session/weekly ≥80%
claude-lb pick --auto-refresh            # Refresh auth_expired profiles inline, then pick
claude-lb pick --count 3                 # Return up to 3 profiles (newline-separated)
claude-lb pick -n 3 --strategy least-used  # Short form; lowest-3 weekly%
```

`--auto-refresh` folds the `refresh --expired` preflight into `pick` itself:
any cached `auth_expired` profile with a stored refresh token is refreshed
before selection. Refresh failures are non-fatal — the expired profile is
filtered out and a healthy one is returned if any exists.

`--count N` returns up to N profiles in strategy order. If fewer than N pass
the filter ladder, returns what's available (exit 0). Each pick is logged to
`picks.log`; only the primary (first) pick updates the stickiness state, so
a subsequent single-pick call honours the primary. Stickiness is ignored when
`N > 1`. `--export` is rejected with `--count > 1` — use `--json` to consume
multiple names programmatically. The JSON shape flips to an array when
`N > 1`:

```bash
claude-lb pick --count 3 --json
# {"data": [{...}, {...}, {...}], "meta": {"count": 3, "requested": 3, ...}}
```

### Refresh

```bash
claude-lb refresh account-a                 # Refresh one profile's OAuth token
claude-lb refresh --all                  # Refresh every discovered profile
claude-lb refresh --expired              # Refresh only past-expiry profiles
claude-lb refresh --soon 30m             # Refresh anything expiring within 30 min
claude-lb refresh --soon 1h --json       # Cron-friendly: */15 * * * * <-- this
```

`--soon DURATION` is anticipatory: it covers already-expired tokens AND tokens
expiring within the window. Combined with a 15-min cron, it keeps the fleet
warm without `--auto-refresh` having to fire mid-spawn (with the latency cost
that implies).

`refresh` POSTs the stored refresh token to Anthropic's OAuth endpoint,
atomically rewrites `.credentials.json` (preserving non-oauth fields), and
invalidates the health cache. A cron hook like
`0 * * * * claude-lb refresh --expired --json` keeps the fleet warm.

### Running commands

```bash
# The north-star idiom: pick + refresh + dispatch in one call.
claude-lb exec --auto-refresh -- claude --dangerously-skip-permissions "write fn"

# With explicit strategy, timeout, and retry-on-rate-limit:
claude-lb exec --strategy least-used --timeout 300 --retry-on-429 1 -- claude ...

# Dry-run: show what would execute without talking to the API
claude-lb exec --dry-run -- claude --help
# → AXIOM_CLAUDE_PROFILE=account-a claude --help
```

`claude-lb exec` picks a profile, sets `AXIOM_CLAUDE_PROFILE=<name>` in the
child's env, and execs the command. The child's exit code becomes claude-lb's
exit code, so scripts can treat `exec` as a transparent wrapper. stdin/stdout/
stderr are inherited (interactive children work unchanged). Every run is
audited to `picks.log` with duration + rc.

Key flags:

- `--auto-refresh` — inline-refresh expired tokens before picking
- `--retry-on-429 N` — if the child fails AND the profile re-probes as
  rate-limited/session-exhausted, retry once with a different profile (best-effort)
- `--timeout S` — kill after S seconds (exit 124)
- `--dry-run` — print the env + command, don't execute
- `--var-name NAME` — override `AXIOM_CLAUDE_PROFILE`
- `--log-full-argv` — log the full child argv instead of just argv[0] (may leak secrets)

### Scripting (lower-level)

```bash
# If `exec` doesn't fit, drop to pick + eval yourself:
profile=$(claude-lb pick --auto-refresh 2>/dev/null)
case $? in
  0) export AXIOM_CLAUDE_PROFILE="$profile" ;;
  2) echo "All profiles dead or expired: run claude login --profile <name>" ;;
  5) echo "No profile is currently ok — retry or relax --require-ok" ;;
  6) echo "All profiles throttled — back off" ;;
  9) echo "All profiles exhausted — operator intervention required" ;;
esac

# Parallel dispatch to N accounts via --count
for profile in $(claude-lb pick --count 3 --strategy least-used); do
    AXIOM_CLAUDE_PROFILE=$profile axiom launch-parcel &
done
wait
```

### History

```bash
claude-lb history                        # Last 20 picks + exec runs
claude-lb history --tail 50              # Last 50
claude-lb history --profile account-a       # Filter to one profile
claude-lb history --since 1h             # Last hour ('30m', '1h', '2d', '1w', or seconds)
claude-lb history --json                 # Structured output
```

Reads `picks.log` (the audit trail every pick + exec writes to). Useful for
"what did the daemon dispatch in the last hour?" without grepping a
tab-separated file.

### Shell completion

```bash
claude-lb --install-completion           # Install for your shell (bash/zsh/fish/pwsh)
claude-lb --show-completion              # Print the script without installing
```

Once installed, `<TAB>` completes commands, flags, profile names (for `show`,
`probe`, `refresh`, `invalidate`, `history --profile`), and `--strategy`
values (`sticky | least-used | round-robin | weighted | first-healthy`).

### Diagnostics

```bash
claude-lb probe --raw                    # Dump untouched /api/oauth/usage body
claude-lb probe --raw --json             # Machine-readable raw dump
```

`--raw` bypasses classification and the cache write; useful for inspecting
unknown fields Anthropic might add, or for capturing test fixtures.

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
| 7 | `CONFLICT` — `refresh` lost a lock race; another process is refreshing |
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
claude-lb update --apply         # git pull --ff-only + uv tool install --reinstall --editable
claude-lb update --apply --no-pull  # Just re-sync deps (useful when a new dep was added locally)
```

**Windows self-upgrade caveat:** on Windows, `claude-lb update --apply` can't
reinstall itself while running — the current process has its own `.pyd` files
memory-mapped, so `uv tool install --reinstall` hits `EACCES` on those files.
claude-lb detects this and prints the exact workaround. Run the reinstall
from a shell that isn't claude-lb (bash, cmd, or PowerShell):

```bash
uv tool install --reinstall --editable "X:/Forge/claude-lb"
```

POSIX platforms (macOS, Linux) are unaffected — inode-swap semantics let
`os.replace` overwrite in-use shared libraries atomically.

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
  — created via `claude-lb add <name>` (see [Setup](#setup) above)
- Outbound HTTPS access to `api.anthropic.com`

## Monthly Overage (Max-only)

Anthropic Max plans support pay-as-you-go overage when the weekly window is
exhausted. `claude-lb` surfaces it via `usage.extra` on every probe (Pro/Team
profiles return `usage: null` since they don't expose this surface):

```json
"extra": {
  "is_enabled": true,
  "monthly_limit": 31000,
  "used_credits": 31280.0,
  "utilization": 100,
  "currency": "AUD"
}
```

`utilization ≥ 100` means the monthly overage budget is spent — the account
still works but falls back to the hard weekly/session caps until the next
month. The status table shows an **Overage** column (colour-coded) when any
profile has overage enabled.

## Recent updates

[**Releases on GitHub**](https://github.com/0xDarkMatter/claude-lb/releases) ·
[Full CHANGELOG](CHANGELOG.md)

### v0.7.0 — onboarding helper

- **`claude-lb add <name>`** — import an existing `.credentials.json` (default
  source: `~/.claude/.credentials.json`) into the multi-profile layout. Cuts
  onboarding from "manually copy files into a directory I haven't created"
  to one command.
- Audience widened: docs no longer assume Max-only — works for any Claude
  Code OAuth account (Max / Pro / Team), with richer usage data on Max.
- Identifying account names stripped from documentation and examples.

### v0.6.0 — operational polish

- **Shell completion** (`claude-lb --install-completion`) — bash/zsh/fish/pwsh,
  with profile-name + strategy completion.
- **`claude-lb history`** — read the picks.log audit trail with `--profile`,
  `--since 30m`, `--tail N` filters.
- **`refresh --soon DURATION`** — anticipatory refresh; cron-friendly
  (`*/15 * * * * claude-lb refresh --soon 30m --json`).
- **doctor refresh-token check** — warns on profiles missing a refreshToken
  (won't auto-heal at expiry).

### v0.5.0 — composing the north-star idiom

- **`claude-lb exec <cmd...>`** — pick a profile, set `AXIOM_CLAUDE_PROFILE`,
  exec the command, propagate child rc. With `--auto-refresh` and
  `--retry-on-429`.
- **`claude-lb pick --auto-refresh`** — inline-refresh expired tokens before
  picking. Folds the two-step preflight into one call.
- **`claude-lb pick --count N` (alias `-n N`)** — multi-pick for parallel
  dispatch workflows.

## Non-goals

- **Not a proxy.** Doesn't sit in the request path. Use
  [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) if you want that.
- **Not an interactive authenticator.** `claude login` creates the
  underlying credentials; `claude-lb add` imports them, `claude-lb refresh`
  extends their lifetime — the browser-based OAuth flow itself is owned by
  the `claude` CLI.
- **Not multi-provider.** Anthropic / Claude Code only.
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
