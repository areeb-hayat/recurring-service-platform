# Security Review — P11

> **Status date:** 5 September 2026. Branch `areeb`. Internal.
> Scope: the authentication, authorization, tenancy, secret-handling and
> token-storage surface as it stands at the end of P8. Companion to
> `docs/P11_HANDOVER.md`.

**Outcome: no blocking findings.** One accepted risk (token storage), noted below.
This review is static (code reading + a repository secret sweep); it does not
replace the dynamic tenant-isolation and idempotency suites, which already exist in
`backend/tests/` and run in CI.

---

## Reviewed and sound

### Authentication (`app/core/security.py`)
- Passwords: **Argon2id** via `argon2-cffi`. Minimum length enforced at hash time.
  Satisfies SEC-11.
- Access token: **JWT HS256 with an explicit `algorithms=[JWT_ALGORITHM]`
  allowlist** on decode — closes `alg=none` and HS/RS algorithm-confusion. Decode
  requires `exp`, `iat`, `sub`; expiry is checked against the injected clock so the
  application has one source of time.
- Refresh token: opaque `secrets.token_urlsafe(48)` (~384 bits), stored only as a
  SHA-256 hash, revocable server-side via `user_session`. A slow KDF is correctly
  *not* used — the input is already high-entropy.

### Authorization & tenancy (`app/api/deps.py`)
- `require_tenant_context` derives the tenant **from the authenticated principal
  only** — never from a path, query, or body parameter. This is what makes
  SEC-3/4/6 structural rather than a matter of per-route care.
- A platform principal on a tenant business route → 403; a suspended/missing tenant
  → 404 (existence not disclosed).
- `require_capability` is a dependency factory; tenant roles hold no `commission:*`
  capability (SEC-5), so commission routes are unreachable by a tenant token.

### Internal job endpoint (`app/api/deps.py::require_job_secret`)
- Shared secret compared with `hmac.compare_digest` (constant-time).
- **Unset secret ⇒ 503 disabled, never open** — a public "dun everyone" URL is the
  one failure this route must never have (SEC-10).
- Takes no tenant parameter; the runner builds its own per-tenant context from the
  tenants the server found.

### CORS & config (`app/main.py`, `app/core/config.py`)
- CORS middleware is added **only when origins are configured**, with an explicit
  origin list — no `allow_origins=["*"]` combined with `allow_credentials=True`.
- All secrets come from environment variables; `require_jwt_secret` and
  `require_internal_job_secret` fail closed and loud when unset.

### Secret hygiene
- `.gitignore` ignores `.env` and `.env.*` (keeps `.env.example`), `node_modules`,
  `dist`, Playwright artifacts.
- A local pattern sweep over all tracked files (AWS keys, PEM private keys, Slack /
  OpenAI / GitHub / Google tokens, committed JWTs) returned **nothing**.
- `tests/test_architecture.py::TestSEC9NoSecrets` asserts `.env.example` carries
  names only, no `.env` is committed, and no default password sits in `bootstrap`.
- CI now runs **gitleaks over the full git history** on every push (A-SEC-9).

---

## Accepted risk (not blocking)

**Frontend token storage in `localStorage`** (`frontend/src/auth/session.ts`).
The standard XSS-exfiltration tradeoff. Chosen deliberately for offline-first
survival across tab close, and documented in the file. Mitigations already in
place: 60-minute access-token lifetime and a server-revocable refresh token. No
change required for V1; revisit only if a stronger session model is wanted later.

---

## Verified separately (existing automated coverage)

These are not re-proven here because tests already prove them and run in CI:
tenant isolation over every route (A-SEC-3/4, route-enumerated), idempotency and
duplicate-submission (A-SYN-1/2/14/15), append-only history (A-AUD-1), no-float
money (FIN-1), vendor/adapter boundary (A-SLOT-5/6), reminder provider-failure
isolation (A-REM-6).

## Recommended for P11/P12 continuation
- Run the full PostgreSQL backend suite (needs Docker) — CI does this per push.
- Owner UAT and a real mobile/offline device pass.
- After P12 backups exist: a genuine restore drill.
- Re-run this review's dynamic equivalents when a real communication/speech
  provider lands (P9/P10).
