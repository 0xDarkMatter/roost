# Findings — open questions from HANDOFF.md

Status as of v0.1.0 · 2026-04-24

The HANDOFF listed five open questions to resolve during implementation. This
document records what is currently known, what remains unconfirmed against
live Anthropic infrastructure, and where the resolution lives in code.

---

## 1. What does Anthropic return for a bad OAuth token?

**Assumed:** HTTP 401 with `{"error": {"type": "authentication_error", "message": "..."}}`.

**Status:** matches `authentication_error` as Anthropic's public
documentation describes for invalid API keys. Bearer-token rejection is
assumed to take the same shape. Not yet confirmed against a live munged
credential — `test_taxonomy.py::test_401_missing_body_still_auth_dead`
covers the body-absent degenerate case so the classifier is resilient if
the concrete shape drifts.

**Code path:** `src/claude_lb/taxonomy.py` — `classify()` step 3 (HTTP 401
→ `AUTH_DEAD`). The `error.type` is only used when a body is present;
absence still yields `auth_dead` with a synthetic error message.

**Action for v0.2:** temporarily mutate one credential on a non-prod
machine, probe it, and commit the exact body under
`tests/fixtures/401-responses/<scenario>.json`.

---

## 2. Are `session_limit` vs `weekly_limit` 429 bodies distinguishable?

**Assumed:** yes, by substring matching on `error.message`. Session bodies
mention `"session"`, `"5-hour"`, `"hourly"`; weekly bodies mention
`"weekly"`, `"plan"`, `"7-day"`, `"Sunday"`.

**Status:** classifier is implemented with externalised keyword lists in
`src/claude_lb/patterns.py`. The `WEEKLY_KEYWORDS` check runs before
`SESSION_KEYWORDS` — important for messages that mention both (see
`test_429_weekly_wins_over_session_when_both_keywords_present`).

**Concrete fixtures:**

- `tests/fixtures/429-responses/session-limit.json` — 5-hour window message
- `tests/fixtures/429-responses/weekly-limit.json` — full plan-limit message
- `tests/fixtures/429-responses/weekly-limit-short.json` — shorter "Resets Sun" variant

**Action for v0.2:** capture more live 429 bodies. Anthropic's exact
wording is known to drift. When a new scenario appears, add a fixture and
expected classification; if the keyword match fails, extend
`SESSION_KEYWORDS` / `WEEKLY_KEYWORDS` rather than adding branches to the
classifier.

---

## 3. Is there a public usage API?

**Status:** not integrated in v0.1. The Max plan dashboard shows
session/weekly percentages, but no documented public endpoint exposes
them at the time of writing. The cache schema has a `usage` slot already
(`session_pct`, `weekly_pct`) so adding enrichment later is a one-module
change in `probe.py` plus a fixture in `tests/`.

**Impact on v0.1:** none. The taxonomy works on error-response
classification alone. `usage` is optional enrichment that makes the
`weighted` / `least-used` strategies smarter when available. Until then
`weekly_pct` defaults to 0 and those strategies degenerate to "any ok
profile."

**Action:** on each release, re-test `GET /v1/me`, `GET /v1/usage`, and
`GET /v1/organizations/<id>/usage` to see if a public surface appears. If
yes, wire it into `probe.py::_probe_once()` after the main GET succeeds
and populate `ProfileHealth.usage`.

---

## 4. Does `claude login --profile <name>` refresh an expired OAuth reliably?

**Status:** unconfirmed end-to-end in this build. The assumption built
into the `auth_dead` state is that the remediation is exactly this
command. The implicit-invalidation mechanism (mtime-bump on the
credentials file → cache entry invalidated) is independent of how the
refresh actually works: as long as `claude login --profile` rewrites
`.credentials.json`, the next `claude-lb probe` will re-classify.

**Code path:** `src/claude_lb/cache.py::is_entry_fresh()` compares
`credentials_mtime` between the cached entry and the current file stat.
Covered by `test_mtime_change_invalidates_auth_dead`.

**Action for v0.2:** end-to-end test on a live profile — deliberately
corrupt one credential, observe `auth_dead`, run `claude login --profile`,
confirm the next `claude-lb probe` returns to `ok`.

---

## 5. Prompt cache accounting on Max plans

**Status:** undocumented as of 2026-04. On pay-per-token API keys, prompt
cache hits cost ~10 % of input tokens (5-minute cache) and writes cost
~125 %. On Max subscriptions the pricing is flat-rate, but quotas are
token-counted — whether cache hits count less, equal, or not at all
against session/weekly budgets is not explicitly documented in Anthropic's
published billing material that I can find.

**Impact on the picker:** stickiness is valuable **regardless** of the
answer. Cache hits reduce latency and reduce wall-clock quota use (fewer
re-transmitted tokens). If cache hits also cost less against Max quotas,
stickiness also stretches budgets. The default stickiness window of
300 s in `src/claude_lb/pick.py::DEFAULT_STICKINESS_S` is conservative —
long enough to batch back-to-back parcels without locking a caller into
one profile through a session reset.

**Default posture:** assume the conservative case (cache hits still count
full-weight against Max quotas). Stickiness is still worthwhile for
latency. Document this in `--help` and README so operators can make an
informed choice.

**Action for v0.2:** if Anthropic publishes a concrete breakdown, link it
from `SPEC.md` §9 (sibling file) and consider widening the default stickiness window.

---

## CRITICAL: the SPEC §7 probe endpoint rejects OAuth tokens (2026-04-24)

**Finding:** Live-probing all three operator profiles (`account-b`,
`account-c`, `account-a`) against `GET https://api.anthropic.com/v1/models`
with a `Bearer <claudeAiOauth.accessToken>` header returns HTTP 401
with body:

```json
{
  "error": {
    "type": "authentication_error",
    "message": "OAuth authentication is currently not supported."
  }
}
```

Classifier correctly labels this as `auth_dead`, but the built-in
remediation hint (`claude login --profile <name>`) is wrong: no amount
of re-logging in will make OAuth tokens work on `/v1/models`.

**Implication for the tool:** claude-lb v0.1 in its current form cannot
actually distinguish healthy from unhealthy OAuth profiles, because the
probe endpoint rejects every OAuth token unconditionally. The whole
seven-state taxonomy works correctly for the responses the server
sends, but the responses tell us nothing useful about per-profile
health.

**Candidate resolutions to investigate in v0.2:**

1. **Endpoint swap.** Find an API route that Anthropic accepts with
   OAuth tokens. Candidates to test:
   - `POST /v1/messages` with `max_tokens: 1` and `model: claude-3-5-haiku-latest` — known to work for Claude Code itself; costs ~1 cached input token per probe.
   - `GET /v1/me` or `GET /v1/organizations/<id>` — may or may not exist publicly.
   - A dedicated introspection endpoint if Anthropic adds one.
2. **Token-shape sanity check at probe time.** If the operator stores
   an API key (`sk-ant-api03-…`) in `.credentials.json` instead of an
   OAuth token (`sk-ant-oat01-…`), `/v1/models` WILL work — but
   `claude login` doesn't produce those. Document the distinction.
3. **Classify this error as a new terminal state.** Add `unsupported`
   (or similar) so the remediation hint differs from `auth_dead`. Gate
   it on the exact message substring `"OAuth authentication is
   currently not supported"` so future shape changes don't silently
   break.

**Workaround for today:** do not rely on `claude-lb` for health routing
until the probe endpoint is fixed. Tools that need to pick a profile
can still call `claude-lb list` (discovery works), `claude-lb pick`
against an empty cache will return `unknown`-state entries in
discovery order (first-healthy behaviour), and the rest of the
plumbing (caching, stickiness, exit codes, JSON envelope) is
independently correct.

**Action item:** replace the SPEC §7 probe endpoint. Until then, this
is the single biggest blocker between v0.1 and v0.1 being *useful*.

---

## Other findings discovered during implementation

- **403 is plan-exhaustion in disguise** (sometimes). Anthropic has been
  observed returning 403 rather than 429 when a Max account is past its
  weekly cap. The classifier has a `prev_health` hook: if the profile was
  previously `ok` and now returns 403, classify as `weekly_limit`;
  otherwise `unknown`. See `classify()` step 5. This is a heuristic, not a
  contract — revisit if the API stabilises.

- **Token extraction drift is real.** Three shapes are tried in order:
  `claudeAiOauth.accessToken` (modern), `oauthAccessToken` (legacy),
  `accessToken` (plain). Each covered by a test. If Claude Code CLI ever
  produces a fourth shape, add it to `_TOKEN_PATHS` in
  `src/claude_lb/discovery.py` and a fixture, nothing else changes.

- **Atomic cache writes don't need OS locks.** `tempfile.mkstemp()` in the
  same directory + `os.replace()` is POSIX-atomic on Linux/macOS and
  sufficiently atomic on NTFS for our purposes. The rare worst case is
  a reader seeing a slightly-stale snapshot — which triggers exactly the
  re-probe the caller wants. Reads never take a lock.

---

## extra_usage `monthly_limit` / `used_credits` unit (2026-04-25)

**Finding:** Anthropic's `/api/oauth/usage` returns an `extra_usage` block
on Max plans with the shape:

```json
{
  "is_enabled": true,
  "monthly_limit": 31000,
  "used_credits": 31280.0,
  "utilization": 100,
  "currency": "AUD"
}
```

The `currency` field is a real ISO 4217 code (observed: `AUD`, `USD`).
`utilization` is a 0–100 percentage. But the unit of `monthly_limit` and
`used_credits` is **undocumented**.

**Empirical interpretation:** the numbers only make sense as **currency
minor units (×100, i.e. cents)**. Three live profiles observed
2026-04-25:

| monthly_limit (raw) | currency | Implied $ value | Plausibility |
|---------------------|----------|-----------------|--------------|
| 31000 | AUD | $310 AUD | Plausible Max overage cap |
| 40000 | USD | $400 USD | Plausible |
| 100000 | AUD | $1000 AUD | Plausible heavy-user cap |

If `monthly_limit` were raw dollars, the implied caps ($31k–$100k AUD/USD
*per month per profile*) would be ~$360k–$1.2M annually for a single Max
seat. Implausible.

**Code path:** `src/claude_lb/models.py::ExtraUsage` stores both fields as
raw `float | None`. `is_exhausted` derives from `utilization >= 100` only,
which is the unit-safe signal. Tests in `tests/test_taxonomy.py` round-trip
the raw integers without reinterpreting.

**Action for the future:** if Anthropic ever publishes the unit (or if
per-token billing changes the meaning), revisit. Don't add a "divided"
helper field — the operator can divide at the call site, and we can't
guarantee the unit is stable. README's "Monthly Overage" section now
documents the empirical finding so users don't read 31000 AUD literally.
