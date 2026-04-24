# AGENTS.md — claude-lb

Context for AI coding assistants working on this repo.

## What this is

A one-shot stateless CLI that answers "which Anthropic Max profile is healthy right now?" It reads local OAuth credentials from `~/.claude-profiles/<name>/.credentials.json`, probes `GET /v1/models`, classifies responses into seven health states, caches results, and picks the best profile for downstream scripts.

## Agent rules

1. **Never add a daemon, proxy, or persistent service.** This is a stateless CLI. If a feature requires running continuously, it belongs in a separate tool.
2. **Never transmit credentials off-device.** Tokens are read from local disk, used for one outbound request per probe, and never logged at default verbosity.
3. **Never widen scope to other providers.** Anthropic / Claude Code Max only. OpenAI, Gemini, etc. are out of scope.
4. **`stdout` is sacred.** Only data or a single profile name goes to stdout. All human-facing output (tables, progress, warnings) goes to stderr.
5. **Don't break the `pick` contract.** `claude-lb pick` must emit one profile name, newline-terminated, to stdout and nothing else. Every downstream shell script depends on this.
6. **Classification order matters.** The 7-state taxonomy has a strict priority: network_error → ok → auth_dead → 429 subtypes → 403 → unknown. Preserve the order in `src/claude_lb/taxonomy.py`.
7. **Keyword lists live in `patterns.py`.** Never inline Anthropic error-message substrings in match statements — they drift. Extend `patterns.py` instead.
8. **Atomic cache writes only.** Use `tempfile.NamedTemporaryFile` + `os.replace`. Never write directly to `health.json`.
9. **Stickiness is the default.** Don't change the default strategy to `round-robin` or similar without explicit operator approval.
10. **Token-shape fallback matters.** Try `claudeAiOauth.accessToken`, then `oauthAccessToken`, then `accessToken`. Format drift is expected.

## Code layout

| File | Responsibility |
|------|---------------|
| `src/claude_lb/cli.py` | Typer entry point, command dispatch, exit-code mapping |
| `src/claude_lb/discovery.py` | Walk `~/.claude-profiles/`, read credentials, extract tokens |
| `src/claude_lb/probe.py` | Async httpx probe against `/v1/models` |
| `src/claude_lb/taxonomy.py` | 7-state classifier (the core of the tool) |
| `src/claude_lb/patterns.py` | Externalised keyword lists for 429 classification |
| `src/claude_lb/cache.py` | Atomic read/write of `health.json`, TTL logic |
| `src/claude_lb/pick.py` | Strategies, stickiness, filter ladder, pick log |
| `src/claude_lb/output.py` | JSON envelope, stream separation helpers |
| `src/claude_lb/paths.py` | Platform-aware paths (config, cache, pick log) |
| `src/claude_lb/models.py` | Pydantic models shared across modules |

## Testing

- `tests/fixtures/429-responses/` holds captured Anthropic 429 bodies. Add one file per scenario; the classifier must correctly label each.
- `tests/fixtures/credentials/` holds synthetic `.credentials.json` files for discovery tests — never real tokens.
- Run: `uv run pytest` or `pytest`.
- Coverage target: 90% on `taxonomy.py`, `discovery.py`, `pick.py`.

## Forma protocol compliance

This tool adheres to Forma Protocol v1.4. See `SPEC.md` for the full mapping. Key points:

- `{data, meta}` JSON envelope
- Semantic exit codes (0–9)
- `stdout = data, stderr = humans`
- `--json` on every command

## Known gaps (v0.1)

Open questions tracked in `docs/findings.md`. Do not block development on answering them — document as you discover.
