# P11 — Hardening / CI / Advisory & Secret Closure (partial)

> **Branch:** `areeb`. **Status date:** 5 September 2026.
> **Not client-facing.** Internal handover.

P11 is the stabilisation phase (README §14, DEVELOPER_HANDOFF §13). It is not a
single commit of new product behaviour; it is the phase that *proves the existing
behaviour stays true* and closes the acceptance items that needed a CI home. This
handover records the part of P11 that is code-side and machine-verifiable. The
parts that need real infrastructure or a human — owner UAT, a physical
device/browser matrix, restore/recovery drills, real-provider outage testing —
are listed under "Not done here" and belong to P12 or to a scheduled UAT session.

P9 (voice/AI) and P10 (messaging) are **not** started and were deliberately left
alone: this work is orthogonal to them.

---

## 1. What this package changed

### 1.1 Dependency advisory closed — react-router-dom v6 → v7

DEVELOPER_HANDOFF §13 named `react-router-dom v6 advisories` as the known risk to
revisit in P11. Both advisories (GHSA-wrjc-x8rr-h8h6 open-redirect, and
GHSA-337j-9hxr-rhxg SSR-hydration constructor injection) affect the **entire** 6.x
line — there is no patched v6 release — so the only fix is the v7 upgrade.

- `react-router-dom` `^6.28.0` → `^7.18.3` (resolves 7.18.3).
- `npm audit` now reports **0 vulnerabilities** (was 2 moderate).
- The app uses only the declarative router surface — `BrowserRouter`, `Routes`,
  `Route`, `Navigate`, `NavLink`, `Outlet`, `Link`, `useNavigate`, `useParams`,
  `useLocation`, `MemoryRouter` — all retained unchanged in v7, so the upgrade
  touched no application source.

**Verified (local, this session):**

```
npm run typecheck   clean
npm test            187 passed
npm run build       clean (tsc + vite + PWA)
npm audit           0 vulnerabilities
```

E2E (`npm run e2e`) also passes — **5 passed** — but only after the fixture fix in
§1.2 below; it was already red at HEAD for a reason unrelated to the router.

### 1.2 E2E regression suite repaired — stale fixture server

The Playwright suite (`frontend/e2e/`) was **red at HEAD before any change in this
package**, and it is worth being precise about why, because it was masking the
suite entirely:

- `SyncEngine.seed()` (the first-sync path) has, since **P6**, read four endpoints
  in parallel — customers, the day, **payments and statements** — so a new device
  seeds financial history too.
- The fixture server `frontend/e2e/server.js` was last touched in **P5**
  (commit `4e6e4f8`) and never grew `/api/v1/payments` or `/api/v1/statements`.
  Those 404'd, the seed's `Promise.all` rejected, the first sync failed, and every
  test died at sign-in showing *"Unavailable offline — this device has not
  synchronised yet"*. It also still advertised `feed_version: 1` while the real
  feed is now **3**.

This is exactly the failure mode P5's own handover warned about: *"when adding a
new feed-visible entity or mutation path, update the feed writer serialization set
and the tests that pin correspondence."* P6/P7/P8 updated the Vitest fixtures
(`src/test/fixtures.tsx` carries `feed_version: 3`) but not the e2e one.

Fix (fixture only — no product code): added the two missing seed endpoints
returning the empty-list shape `{ items: [] }`, and aligned the feed to
`feed_version: 3` with `payment` and `statement` in the entities list. The suite's
own assertions are about service-record sync (CONFIRM/SKIP offline), so empty
financial history is correct for it.

**And the guard that was missing** — so this cannot rot silently again. P5's
handover asked for "the tests that pin correspondence"; there was none for the
frontend fixture. Added `frontend/e2e/first-sync-contract.spec.ts`: a fast,
browserless Playwright test that holds the client's first-sync contract *independently*
(the seed endpoints, the feed version, the entity set) and asserts the running
fixture satisfies it. It fails with a message that names the drift directly —
*"GET /api/v1/statements → 404; SyncEngine.seed() reads it"* — instead of an opaque
browser timeout three layers down. `SyncEngine.seed()` now carries a comment
pointing at it. Proven to fail on the exact P8 drift (fixture `feed_version` set
back to 1 → the version assertion fails, `Expected: 3, Received: 1`), then reverted.
The expectation deliberately lives in the test, not read out of the fixture — a
correspondence check that read its expected values from the thing it checks would
be tautological. **Result: 10 passed** (5 offline-sync + 5 contract).

### 1.3 CI pipeline — `.github/workflows/ci.yml` (new)

There was **no CI** in the repository. The acceptance contract already assumes one
exists (A-SEC-9 "runs in CI", A-SLOT-6 "runs in CI"), and DEVELOPER_HANDOFF §13
flagged the secret-scanning acceptance item as still open. This workflow is that
home. Four jobs, each mirroring exactly what CLAUDE.md already tells a developer to
run by hand:

- **backend** — `postgres:16-alpine` service + Python 3.12, `pip install -e ".[dev]"`,
  `pytest`. Migrations self-provision `pg_trgm` / `btree_gist`, so a stock image
  needs no extra setup. This job also runs the source-level architecture guards in
  `tests/test_architecture.py` (A-SLOT-5 import boundary, **A-SLOT-6 vendor grep**,
  FIN-1 no-float, AUD-1 append-only, SEC-9 env-example) — so those acceptance
  items now execute in CI, not only on a developer's machine.
- **frontend** — `npm ci`, typecheck, Vitest, production build, then Playwright
  e2e against `dist/`.
- **secret-scan** — **closes A-SEC-9.** gitleaks (official image, default ruleset)
  over the full git history (`fetch-depth: 0`); a hit fails the build.
- **dependency-audit** — `npm audit --audit-level=moderate` (frontend) and
  `pip-audit` (backend), so a re-introduced advisory is caught. Threshold note is
  in the workflow.

Runs on push/PR to `main`, `yahya`, `areeb`; one run per ref, newer cancels older.

### 1.4 Security review (manual, this session)

Read the security-sensitive surface. **No blocking findings.** The posture is
mature:

| Area | Finding |
| --- | --- |
| Password hashing | Argon2id (`argon2-cffi`). SEC-11 satisfied. |
| Access token | JWT **HS256 with an explicit `algorithms=` allowlist** — no `alg=none` / algorithm-confusion. Requires `exp`/`iat`/`sub`; expiry checked against the injected clock. |
| Refresh token | Opaque 48-byte (`secrets.token_urlsafe`), stored SHA-256-hashed, server-revocable. |
| Tenant scoping | `require_tenant_context` derives the tenant from the **authenticated principal only**, never from path/query/body (SEC-3/4/6). Platform principal → 403 on tenant routes. |
| Job endpoint | Shared secret via `hmac.compare_digest` (constant-time); **unset ⇒ 503 disabled, never open**; takes no tenant parameter. |
| CORS | Middleware added only when origins are configured; explicit origin list with credentials — **no wildcard-with-credentials trap**. |
| Config/secrets | All secrets from env; `require_jwt_secret` / `require_internal_job_secret` fail closed and loud. |
| Secret sweep | Local pattern sweep over all tracked files: **no keys, private keys, or committed JWTs.** `.gitignore` ignores `.env` / `.env.*` (keeps `.env.example`). |

**One accepted risk (not a blocker):** the frontend stores tokens in
`localStorage` (`frontend/src/auth/session.ts`) — the standard XSS-exposure
tradeoff, chosen for offline-first survival across tab close and documented in the
file. Mitigated by the 60-minute access token and the server-revocable refresh
token. Revisit only if a richer session model is ever wanted; not required for V1.

---

## 2. Not done here (needs infrastructure or a human)

These are real P11 line items that cannot be honestly completed in this
environment. None is blocked by code; each needs a runtime or a person.

- **Full backend suite run.** No Docker/PostgreSQL was available in this session,
  so `pytest` was **not** run here. The CI backend job runs it on every push — that
  is the intended proof. A developer with Docker should also run it once locally
  per CLAUDE.md before merge.
- **Owner acceptance testing (UAT)** and the **manual smoke test** in
  DEVELOPER_HANDOFF §11 — a person driving the real business workflow.
- **Physical device / browser matrix** (mobile Daily Register, real offline
  toggling on a phone). The Playwright suite covers the Service Worker in Chromium;
  it is not a substitute for a real device pass.
- **Restore / recovery drill** — requires the P12 backup infrastructure to exist
  first; test the restore, don't just assume the dump.
- **Real-provider outage testing** — the communication provider is still the mock
  (P10 not built); there is no real transport to fail yet. The mock-failure
  invariant (A-REM-6) is already tested in `test_reminders`.

---

## 3. How to verify this package

```bash
# frontend (Node 20+; done locally this session except e2e)
cd frontend && npm ci && npm run typecheck && npm test && npm run build
npx playwright install chromium && npm run e2e

# backend (needs Docker) — CI runs this automatically
cd backend
docker compose -f docker-compose.test.yml up -d
export TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:55432/rsp_test
pip install -e ".[dev]" && pytest

# CI itself: push the branch; watch the four jobs on GitHub Actions.
```

---

## 4. Files touched

```
.github/workflows/ci.yml     new — CI pipeline (backend/frontend/secret-scan/dependency-audit)
frontend/package.json        react-router-dom ^6.28.0 -> ^7.18.3
frontend/package-lock.json   lockfile for the above
frontend/e2e/server.js               fixture: +/payments +/statements; feed_version 1->3; exported constants; import-guarded listen
frontend/e2e/first-sync-contract.spec.ts  new — the correspondence guard (browserless)
frontend/e2e/server.d.ts             new — types so the guard can import BUSINESS_DATE under strict tsc
frontend/src/sync/engine.ts          comment only — signpost at seed() pointing at the guard
docs/P11_HANDOVER.md                  this file
docs/SECURITY_REVIEW.md               the review notes above, standalone
```

No **application behaviour** changed — the one source-tree edit
(`frontend/src/sync/engine.ts`) is a comment. Everything else is CI, docs, or the
e2e test harness, none of which ships. No invariant was touched. No table, no
runtime dependency beyond the router version bump (CI tooling aside), and no
background service was added to the product.
