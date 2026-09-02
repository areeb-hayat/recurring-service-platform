# P1 — Handover

Package: **P1 — Backend & Data Foundation**. Backend and tests only; no frontend, no adapters, no
network calls. Nothing committed.

---

## 0. Git state — ABNORMAL, needs recovery before P1 can be committed

**HEAD is `c2db983`** — "Initialize recurring service platform requirements". That commit contains
**only the original README**. It is *not* a committed P0 base.

The P1 brief assumed "the latest **committed** P0 architecture is authoritative". That premise was
false: **P0 was never committed.** When P1 began, the repository had exactly one commit, and the
whole P0 freeze — `CLAUDE.md`, the three `docs/P0_*.md` files, and the substantially revised
`README.md` — existed only as uncommitted working-tree changes. They were read from disk in full and
treated as authoritative, so no decision was lost, but the consequence is structural:

> **The working tree currently contains two packages' worth of uncommitted work (P0 and P1) on top
> of a commit that predates both. Git history must be recovered before P1 is committed.**

Recommended recovery, in order:

1. Commit P0 first, on its own: `README.md` (modified), `CLAUDE.md`, `docs/P0_ARCHITECTURE_FREEZE.md`,
   `docs/P0_INVARIANTS_AND_ACCEPTANCE.md`, `docs/P0_HANDOVER.md`.
2. Then commit P1: `.gitignore`, `backend/**`, `docs/P1_HANDOVER.md`, and the P1-authored edits to
   `CLAUDE.md` and the two P0 docs (SYN-14, SYN-15, the §5.3 clarification).

Step 2 touches files created in step 1, so the split has to be made deliberately (stage by path, not
by file set). Do **not** squash them into one commit: the architecture freeze and its first
implementation are separately reviewable artefacts, and a reviewer needs to see what was frozen
before seeing what was built against it.

---

## 1. Scope implemented

Money and quantity primitives; the deterministic clock and business-date rule; the Alembic baseline
with all P0 §6 constraints for the eight P1 tables; authentication with the capability map and
tenant scoping; customer CRUD; daily service record / skip / correct / void with the append-only
ledger and audit trail; the reusable idempotency mechanism; and the test suite.

**Not built, deliberately:** frontend, PWA, IndexedDB, the bulk sync endpoint, billing cycles,
statements, payments, reminders, communication, commission, search, voice, speech, AI, jobs/cron,
dashboards, deployment. No `app/adapters/` package exists, and `app/ports/` is an explanatory
placeholder with no Protocol defined — a port with no implementation and no caller would be
speculative code. Guard tests assert each of these absences.

**Explicit historical service date — frozen V1 rule.** Ordinary recording takes its authoritative
business date from the server using the tenant timezone; `client_created_at` is advisory and is read
by nothing. An explicitly supplied `service_date` is validated separately and may not be in the
future relative to the tenant-local business date. There is **no maximum historical age and no
backdate window** — an arbitrary limit would be invented policy, and no period locking exists in V1.

**`row_version`** is drawn from one shared PostgreSQL sequence (`row_version_seq`) on `tenant`,
`customer`, `daily_service_record` and `ledger_entry`, and advances on every mutation — create,
customer PATCH, `ACTIVE -> SUPERSEDED`, and `ACTIVE -> VOIDED`. See §4a.

---

## 2. Stack and dependencies declared

| Dependency | Why |
| --- | --- |
| `fastapi`, `pydantic`, `pydantic-settings` | P0 frozen API layer and env-driven config |
| `SQLAlchemy>=2.0.36`, `alembic`, `psycopg[binary]` | P0 frozen ORM, migrations, PostgreSQL driver |
| `PyJWT` | short-lived access tokens (P0 §3.3) |
| `argon2-cffi` | password hashing (SEC-11) |
| dev: `pytest`, `hypothesis`, `httpx`, `uvicorn` | tests, money property tests, TestClient, local run |

Nothing else. No Redis, Celery, broker, RBAC framework, or generic repository layer.

Three declared deviations from the brief's suggested list, each with a reason:

1. **`pytest-asyncio` not installed.** The backend is synchronous — sync SQLAlchemy sessions and
   sync FastAPI handlers, which FastAPI runs in a threadpool. There is no async code to drive, so
   the plugin would be an unused dependency. Add it if async is ever introduced.
2. **`email-validator` not installed.** Pydantic's `EmailStr` pulls in `email-validator` +
   `dnspython` for one field. §22 asks for email *format* validation, which a constrained pattern
   in `app/api/schemas.py` provides.
3. **UUIDv7 implemented locally** (~15 lines in `app/core/ids.py`) rather than adding a `uuid6`
   dependency. Python 3.12/3.13 has no `uuid.uuid7`.

**Python version:** P0 froze 3.12. This machine has 3.13.2 and 3.10 — no 3.12. `pyproject.toml`
declares `requires-python = ">=3.12"`, which 3.13 satisfies, and the suite runs on 3.13.2. No
3.13-only syntax is used, so a 3.12 interpreter will work unchanged.

---

## 3. Migration and tables

**Revision `0001_p1_baseline`** (`down_revision = None`), written by hand rather than autogenerated
so every P0 §6 constraint is explicit and constraint names are stable for the schema test.

Creates the shared sequence `row_version_seq` and exactly eight tables:

`tenant`, `app_user`, `user_session`, `customer`, `daily_service_record`, `ledger_entry`,
`audit_event`, `sync_operation`.

Constraints worth naming: the partial unique index `uq_daily_service_record_active_day`
(`WHERE status = 'ACTIVE'`) that *is* the duplicate-service guarantee; composite foreign keys
`(tenant_id, customer_id) -> customer(tenant_id, id)` on records and ledger entries; the
`skip_is_zero` CHECK enforcing FIN-7 in the database; `amount_non_zero` on the ledger;
`scope_matches_role` on `app_user` making the tenant/platform split structural; and
`uq_sync_operation_tenant_id_operation_id`.

`ledger_entry.posting_cycle_id` exists and is nullable with **no foreign key**, because
`billing_cycle` is a P2 table — see §7.

---

## 4. Tests

**254 passed, 0 failed, 0 skipped**, against PostgreSQL 16 in Docker. Full suite ~65s.

| File | Tests | Covers |
| --- | --- | --- |
| `test_schema.py` | 65 | live-schema assertions: table inventory, SEC-1/2, SYN-4, column types, CHECKs, SEC-7 |
| `test_service_records.py` | 40 | FIN-3/4/6/7, SYN-4, AUD-2..AUD-6, ledger postings, business-date rule, provenance |
| `test_architecture.py` | 31 | A-SLOT-5/6, A-AUD-1, FIN-1/12, AUD-7, SEC-3/9, no-future-scope |
| `test_money.py` | 28 | FIN-1/2/3 including hypothesis property tests |
| `test_auth_and_capabilities.py` | 27 | SEC-5/7/8/11, login/refresh/logout, customer API |
| `test_clock.py` | 19 | R4 business date, midnight and DST boundaries, unbounded backdating |
| `test_tenant_isolation.py` | 18 | SEC-2/3/4/6, route enumeration from OpenAPI |
| `test_idempotency.py` | 14 | SYN-1/2/3/13/14/15 including 5-thread concurrency |
| `test_row_version.py` | 12 | shared-sequence semantics on create, PATCH, supersede, void |

No test makes a network call, and none requires an adapter, AI or speech provider.

**The suite refuses to run without PostgreSQL.** `pytest_configure` raises a `UsageError` when
`TEST_DATABASE_URL` is unset, so a missing database aborts the run (exit 4) instead of skipping.
There is no SQLite fallback and no conditional skip anywhere: a green run always means the schema,
constraint and isolation suites actually executed.

---

## 4a. row_version verification

The review asked for the implementation to be inspected rather than the migration. Doing so found a
real defect, now fixed.

**Verified working:** create draws from the shared sequence; each customer PATCH advances it
(`V1 < V2 < V3`), and every individually mutable field does so, not just one; `ACTIVE -> SUPERSEDED`
advances the superseded row; `ACTIVE -> VOIDED` advances that row; values are unique across
`customer`, `daily_service_record` and `ledger_entry`, proving one shared sequence rather than
per-table counters; and `expected_row_version` drives optimistic concurrency (409
`ROW_VERSION_CONFLICT`).

**Defect found and fixed:** in `correct_service`, the replacement record drew its `row_version`
*before* the superseded original was bumped, so the replacement sorted **older** than the
supersession it resolved. A P5 client pulling "everything after the superseded row" would have
received an `ACTIVE -> SUPERSEDED` transition with no visible successor. Fixed by bumping the
original first, so the replacement is always the later value. `test_row_version.py` asserts both the
pairwise ordering and that a three-link correction chain is strictly increasing.

No P5 sync endpoint was built.

---

## 5. Coverage: invariant implemented vs. acceptance criterion verified

The distinction matters and the previous draft blurred it. **"Implemented"** means the rule holds in
code and has a test that fails without it. **"Acceptance verified"** means the *entire* P0 acceptance
criterion was executed — several of which reference statements, payments or a UI that P1 does not
build, and which therefore cannot be fully verified yet.

| Invariant | Implemented | Full P0 acceptance criterion | Gap |
| --- | --- | --- | --- |
| FIN-1 | yes | **partial** | A-FIN-1's "no JavaScript arithmetic on `*_minor`" clause has no frontend to scan |
| FIN-2 | yes | yes | |
| FIN-3 | yes | **partial** | A-FIN-3 requires `sum(charges) == statement.charges_minor`; statements are P2. The drift-free summation property is tested at primitive level |
| FIN-4 | yes | **partial** | A-FIN-4/5's property test includes payments and payment voids, which do not exist |
| FIN-6 | yes (record + ledger) | **partial** | A-FIN-6 also asserts the *statement* still shows the old price; statements are P2 |
| FIN-7 | yes | yes | also enforced by a DB CHECK |
| FIN-12 | yes | yes | |
| SEC-1, SEC-2 | yes | yes | schema test + direct-SQL rejection |
| SEC-3, SEC-4 | yes | yes *(for existing routes)* | route list is enumerated from OpenAPI and fails when a new scoped route appears uncovered |
| SEC-5, SEC-7, SEC-8, SEC-11 | yes | yes | |
| SEC-6 | yes | **partial** | A-SEC-6's "owner-admin gets 403 on every `/platform/*` route" is vacuous — no platform route exists yet. The reverse direction is fully tested |
| SEC-9 | **partial** | **no** | The repository contains no secret and `.env.example` is values-free, both asserted. **No pre-commit or CI secret scanning exists** — A-SEC-9 requires it and it is not implemented |
| SYN-1, SYN-2, SYN-3, SYN-4 | yes | yes | |
| SYN-13, SYN-14, SYN-15 | yes | yes | |
| AUD-1..AUD-7, AUD-9 | yes | yes | |
| AUD-8 | **partial** | **no** | Superseded and voided rows are retained, returned and tested via `GET /service/day/{date}?include_history=true`. A-AUD-8 requires a **customer-history endpoint**, which is P6 scope; it was not added merely to earn the label |
| A-SLOT-5, A-SLOT-6 | yes | yes | |

**Partial by scope (invariant not yet fully exercisable):** FIN-5 (holds for charges and
adjustments; payments are P2), FIN-11 (`outstanding_minor` is derived per read and never stored, but
the PAID / PARTIALLY_PAID / UNPAID projection needs payments), SEC-10 (no internal job endpoint
exists).

**Not applicable in P1:** FIN-8/9/10/13/14/15/16, PAY-*, REM-*, COM-*, VOI-* beyond the
`input_method` provenance column, SYN-5..SYN-12 (all client-side), A-SLOT-1..4 (no adapters exist,
so these would pass vacuously and are not claimed).

Frontend, Playwright and offline acceptance tests are **not** implemented and are not claimed.

**Two follow-ups this audit produced:** add CI secret scanning to close A-SEC-9, and re-run A-FIN-6,
A-FIN-3, A-FIN-4/5 in full once statements and payments exist in P2.

---

## 6. Local prerequisites

PostgreSQL is required; SQLite is never substituted. No local PostgreSQL was installed, but Docker
was available, so the suite uses a minimal test-only container:

```
cd backend
docker compose -f docker-compose.test.yml up -d
export TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:55432/rsp_test
pytest
```

`TEST_DATABASE_URL` may point at any PostgreSQL 16. Without it the suite **aborts** with a
`UsageError` (exit 4) rather than skipping or falling back to SQLite — verified by running the suite
with the variable unset. CI therefore cannot go green on a missing database.

Dependencies were installed into the machine's global Python 3.13 with `pip install -e ".[dev]"`.
A virtualenv would be cleaner and is recommended before further work.

---

## 7. Deliberate boundaries left for P2

**Posting-cycle resolution.** `ledger_entry.posting_cycle_id` is nullable with no FK, and every
entry is written with `posting_cycle_id = NULL`. The frozen late-correction rule (P0 §5.5) says an
adjustment keeps its original `occurred_on` but posts to the *open* cycle. `occurred_on` is already
written correctly — always the original service date, including on corrections and voids — so P2
adds cycle resolution at the single `post_entry` call site plus a migration adding the FK. No
correction semantics change.

**Register status vocabulary.** P1 registers `APPLIED` operations only. A rejection or conflict
commits no effect, so persisting it would let a transient validation failure permanently poison an
`operation_id`. The column keeps the full `APPLIED | REJECTED | CONFLICT` vocabulary so P5 can
extend without a migration.

**`row_version` bumping** is explicit in application code, not a trigger. P1 has three write paths;
if that grows, move it into a `BEFORE UPDATE` trigger so it cannot be forgotten.

---

## 8. Architecture defects found and fixed

**1. Ledger uniqueness vs. correction chains (real bug, caught by test).** P0 §6 allows one
`ADJUSTMENT` per `(tenant, source_type, source_id)`. Posting a correction's delta against the
*replacement* record meant that voiding that replacement later needed a second `ADJUSTMENT` on the
same source — a unique-constraint violation. Fixed by attaching the adjustment to the record whose
active life is *ending*, which also unified correction and void under one rule: *post
`(replacement_charge - own charge)` against the record being closed; a void is
`replacement_charge = 0`*. A record is either superseded or voided, never both, so it can only ever
carry one adjustment. P0 §5.3 gained a clarifying paragraph.

**2. Idempotency race (real bug, only reproduced under full-suite load).** Running the effect
before inserting the register row let a *business* constraint win the race: five concurrent
identical envelopes collided on the daily-record active-day index and surfaced as `CONFLICT` when an
identical replay must be `DUPLICATE`. Fixed by claiming `(tenant_id, operation_id)` **before**
running the effect, making the register the serialization point. Recorded as new invariant SYN-15.

**3. Idempotency-key reuse was unspecified.** SYN-2 defines identical replay but says nothing about
the same `operation_id` arriving with a *different* payload. Implemented as fail-closed
(`IDEMPOTENCY_KEY_REUSE`, 409): the earlier result is not returned as though the requests matched,
and the new request is not applied. Recorded as new invariant SYN-14, as §29 of the brief requires.

**4. Alembic mangled explicit CHECK names.** Alembic applies the metadata naming convention to
`CheckConstraint`, so explicit names were double-prefixed and two exceeded PostgreSQL's 63-character
limit and were silently truncated with a hash. Fixed by passing bare names and letting the
convention expand them. Without the schema-assertion test this would have shipped unnoticed.

**5. JWT expiry ignored the injected clock.** PyJWT validated `exp` against real system time while
the app used an injected clock, so frozen-clock tests failed spuriously. Expiry is now checked
against the injected clock, giving one source of time and making token expiry testable without
sleeping.

**6. Replacement records sorted older than the rows they superseded** (found in the review pass).
See §4a — a correction's replacement drew its `row_version` before the superseded original was
bumped, which would have broken the P5 change feed. Fixed by reordering the two sequence draws.

P0 changes were limited to: new SYN-14 and SYN-15 with acceptance criteria, and one clarifying
paragraph in §5.3. Nothing else was rewritten.

---

## 9. Risks

**R1 — The suite depends on Docker on this machine.** No local PostgreSQL exists, so tests run
against a `docker compose -f docker-compose.test.yml` container. CI must provide a PostgreSQL 16
service. This is no longer a silent-skip risk — the suite now aborts without a database — but the
service still has to exist in CI.

**R2 — Historical dates are unbounded by design (an invented 90-day window was removed).** An
earlier draft capped explicit backdating at 90 days. That was invented product policy, not a client
decision, and it is gone. The only V1 rule is "not in the future". A typo of `2019-03-15` is
therefore accepted. If the client wants a bound, that is a product decision; period locking is
properly a billing-cycle concern for P2, not an arbitrary constant here.

**R3 — Global Python install.** Dependencies went into the machine's global Python 3.13. Move to a
virtualenv before P2.

**R4 — No load has been measured.** `outstanding_minor` is an indexed integer sum with no cache, as
P0 requires. It is correct and fine at P1 scale; measure during P6 before considering
materialisation.

**R5 — Correction re-prices at the original snapshot, by design.** A correction reuses the
superseded record's `unit_price_minor`, never today's price, so fixing a quantity cannot silently
re-price history. If the client ever needs "the price was wrong too", that is a distinct operation
and needs its own design.

---

## 10. Recommended next package

**P2 — Financial Engine.** In order:

1. `billing_cycle` + `statement` tables, and the FK on `ledger_entry.posting_cycle_id`.
2. Posting-cycle resolution at the `post_entry` call site, implementing the P0 §5.5 late-correction
   rule (`occurred_on` stays; posting moves to the open cycle).
3. The `payment` table and manual payment record/void, reusing `execute_idempotent` unchanged — PAY-1..PAY-9.
4. Statement issue with the origin-split movement columns (FIN-8), and the derived status
   projection (FIN-11).
5. The §11.1 reporting derivations, with A-FIN-14/15/16 as the tests that keep business generated,
   billed value and collections from being conflated.

P2 should not need to modify anything P1 built, other than adding the `posting_cycle_id` FK.

---

## 11. Confirmation of exclusions

No GHL, no Groq, no Whisper or speech provider, no AI interpreter, no voice route, no payment
provider, no online payment of any kind, no frontend, and no outbound network call exist in this
package. `input_method = VOICE` is a provenance column only, accepted by the API and proven
behaviour-neutral; there is no voice endpoint. Guard tests in `tests/test_architecture.py` assert
each absence, including that no HTTP client library is imported by application code.

No secret is in source control. `.env` is git-ignored, `.env.example` contains names with empty
values only, and the bootstrap tool reads passwords from the environment and refuses to invent a
default.

---

## 12. Files created

```
.gitignore
backend/pyproject.toml  .env.example  alembic.ini  docker-compose.test.yml
backend/alembic/env.py  script.py.mako  versions/0001_p1_baseline.py
backend/app/  main.py  db_models.py  bootstrap.py
  core/      config.py clock.py money.py ids.py errors.py security.py db.py
  tenancy/   models.py context.py
  identity/  models.py capabilities.py service.py
  customers/ models.py commands.py
  service/   models.py commands.py
  billing/   models.py ledger.py
  audit/     models.py service.py
  sync/      models.py idempotency.py
  api/       deps.py schemas.py routes.py
  ports/     __init__.py  (placeholder, no Protocol defined yet)
backend/tests/ conftest.py _source.py test_{money,clock,schema,service_records,
               idempotency,tenant_isolation,auth_and_capabilities,architecture}.py
docs/P1_HANDOVER.md
```

Plus `backend/tests/test_row_version.py`, added in the review pass.

Modified: `CLAUDE.md` (phase), `docs/P0_ARCHITECTURE_FREEZE.md` (§5.3 clarification),
`docs/P0_INVARIANTS_AND_ACCEPTANCE.md` (SYN-14, SYN-15 + acceptance). `README.md` untouched by P1 —
its modification belongs to the uncommitted P0 work described in §0.

One structural addition to P0 §2.1: an `app/audit/` module. Audit is cross-cutting and belongs to no
listed domain; `core/` is scoped to primitives, and attaching it to one arbitrary domain would be
worse.
