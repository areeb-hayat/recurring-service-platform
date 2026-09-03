# P2 — Handover

Package: **P2 — Financial Engine**. Backend and tests only. No frontend, no adapters, no network
call, nothing committed.

**Base commit:** `22df746` — "Implement P1 backend and data foundation", branch `main`, working tree
clean at start.
**Migration revision:** `0002_p2_financial_engine` (`down_revision = "0001_p1_baseline"`).

---

## 1. Scope implemented

Billing cycles with tenant-local calendar periods and close; posting-cycle resolution at the single
ledger append site; immutable issued statements with the P0 §5.4 movement split and carry-forward;
manual payments with void; the derived customer payment status; and the four P0 §11.1 reporting
derivations.

**Deliberately not built:** reminders, communication, commission, jobs/cron, the bulk sync HTTP
endpoint, dashboards, search, voice, speech, AI, frontend. No `app/adapters/` package exists and
`app/ports/` is still an empty placeholder. No payment provider, `payment_attempt` table, provider
column or callback route exists — and guard tests assert each absence rather than assuming it.

**Nothing P1 built was weakened.** The only P1 behaviour changed is that `post_entry` now resolves a
`posting_cycle_id` instead of writing `NULL`; correction and void semantics, `occurred_on`, the
adjustment-origin rule, idempotency, tenancy and audit are untouched.

---

## 2. Tables, constraints and the closed P1 boundary

Three tables added, exactly the P0 §6 set for this package.

**`billing_cycle`** — `tenant_id`, `period_start`, `period_end`, `status` (`OPEN | CLOSED`),
`closed_at`, `closed_by_user_id`, `created_at`.
`uq_billing_cycle_tenant_id_id` (composite FK target) · `uq_billing_cycle_tenant_id_period_start` ·
`uq_billing_cycle_one_open_per_tenant` — a **partial unique index** `WHERE status = 'OPEN'`, which
*is* the one-open-cycle guarantee · CHECKs `period_ordered`, `status_valid`,
`closed_at_matches_status`.

**`statement`** — the P0 §6 column list verbatim, including both origin-split movement columns.
`uq_statement_tenant_id_customer_id_cycle_id` · composite FKs to `customer` and `billing_cycle` ·
`ix_statement_tenant_id_cycle_id` · CHECK **`balance_identity`**, which enforces
`closing = opening + charges + service_adjustments − payments + payment_reversals` in the database ·
non-negativity CHECKs on charges, payments, payment reversals, service days and quantity.
A trigger `statement_immutable` rejects **UPDATE and DELETE** on the table.

**`payment`** — the P0 §6 column list verbatim. Composite FK `(tenant_id, customer_id)` ·
`ix_payment_tenant_id_customer_id_received_on` · CHECKs `amount_positive`, `method_valid`,
`status_valid`, `source_valid`, `voided_at_matches_status`, `void_requires_reason_and_actor`.
**No unique index touches `amount_minor` or `received_on`** — asserted by a test, because such an
index would be a correctness bug (PAY-6).

**`ledger_entry.posting_cycle_id`** — the P1 deferred boundary, closed. It now carries the composite
foreign key `fk_ledger_entry_tenant_id_posting_cycle_id → billing_cycle(tenant_id, id)`, so an entry
cannot post into another tenant's cycle. The column stays **nullable** and is not backfilled; see
§7 R1.

**`row_version` on `statement` and `payment`**, drawn from the shared P1 sequence. P0 §7.1 puts
statements and payment history in the client's authoritative offline snapshot and §7.4 pages that
snapshot on `row_version > since`, so both need their own cursor value; §6 had simply omitted the
column. `billing_cycle` deliberately does **not** get one — it is billing scaffolding, not a client
sync entity, and adding it for symmetry would make it one by accident. See §7 D4.

---

## 3. Domain behaviour and API

New modules: `app/billing/cycles.py`, `app/billing/statements.py`, `app/billing/reporting.py`,
`app/payments/{models,commands}.py`. `app/payments/` is a P0 §2.1 domain module, not a new layer.

**Cycles.** Monthly calendar periods in the tenant timezone, with `tenant.cycle_start_day` shifting
the boundary; a non-`MONTHLY` `cycle_type` raises rather than pretending (D7 stays deferred). The
open cycle is created lazily by the first posting that needs one — P2 has no job runner, and a cycle
created on demand is exactly as correct as one created at midnight. A cycle created this way is
always the full configured period **containing today**, so it can never be shortened, stretched or
back-dated into a period that has already ended.

**`period_end` is inclusive, so a cycle may not be closed until it has passed.** The earliest valid
close is `business_date > period_end`, evaluated against the tenant-local business date the server
derived — closing on `period_end` itself is refused, because business recorded later that same day
must still be eligible for the cycle. A premature close raises `CYCLE_PERIOD_NOT_ENDED` (422) and
changes nothing: no status change, no statements, no synthetic cycle. There is no override flag.

**An `OPEN` cycle whose period has ended accepts nothing.** Rollover is not automatic, so August can
still be `OPEN` on 1 September; posting into it would file September's business under a finished
period. `ensure_open_cycle` refuses with `CYCLE_ROLLOVER_REQUIRED` (409) and does **not** auto-close
the stale cycle from inside a service or payment command — closing issues statements, and issuing a
customer's bill as a side effect of recording a delivery is not a write command's decision. A
scheduled rollover calls the real close operation. The guard is one-directional: an entry may post
to a cycle that *began* before it, never to one that *ended* before it, so backdating into a closed
period still posts to the current open cycle. Close is one-way; there is no reopen and **no period
locking beyond close**, which is what P0 §11.1 froze.

**Posting-cycle resolution.** Every entry posts to the tenant's currently OPEN cycle while keeping
its true `occurred_on`. One rule covers ordinary postings, backdated records and late corrections
alike, and it is what makes an issued statement unrewritable: the open cycle is always the latest, so
a closed cycle can never gain an entry afterwards.

**Statements** are issued **when a cycle closes**, in the same transaction. P0 §15 exposes no
statement-issuing route, and issuing from an open cycle would let a later posting contradict a
document FIN-8 declares immutable — so close is the only issue point. One statement per customer with
any ledger entry in this cycle *or* an earlier one, which is how a carry-forward-only customer still
receives a bill. Opening balance is the previous statement's closing balance, or the sum of entries
posted to earlier cycles where none exists; the two agree by construction. `service_days` and
`total_quantity` are derived from the same CHARGE entries as `charges_minor`, so they cannot disagree
with it.

**Payments.** `CASH | BANK_TRANSFER | OTHER`, `amount_minor > 0`, received date defaulting to the
tenant business date and never in the future. Full, partial and over-payment all behave; an
overpayment yields a negative (credit) balance and is never clamped. Recording appends exactly one
negative `PAYMENT` entry. Void is `RECORDED → VOIDED` with a mandatory reason, actor and timestamp,
the row never edited or deleted, and appends a compensating **payment-origin** `ADJUSTMENT` carrying
the payment's original `received_on` but posting to the open cycle. The transition advances the
payment's own `row_version` before the compensating entry draws its later one, so a delta that has
seen the void has necessarily also seen the ledger movement explaining it. Duplicate protection is
`operation_id` through the unchanged `execute_idempotent`; no payment-specific mechanism was added.

**Status** is one function, `customer_payment_status`, derived per read from the ledger: `PAID` when
outstanding ≤ 0, `UNPAID` when outstanding > 0 and nothing was collected against the current cycle
*net of reversals*, `PARTIALLY_PAID` otherwise. No stored column, asserted by a schema test.

**Routes added**, all from the P0 §15 surface, all capability-gated by the unchanged capability map:

```
GET  /api/v1/billing/cycles                       billing:read
POST /api/v1/billing/cycles/{cycle_id}/close      billing:close_cycle   (idempotent)
GET  /api/v1/statements/{statement_id}            billing:read
GET  /api/v1/customers/{customer_id}/statements   billing:read
POST /api/v1/payments                             payment:record        (idempotent)
POST /api/v1/payments/{payment_id}/void           payment:void          (idempotent)
```

`GET /customers/{id}` now also returns `payment_status` beside `outstanding_minor`, both derived.

---

## 4. Reporting derivations (P0 §11.1)

```
business_generated = Σ CHARGE + Σ ADJUSTMENT WHERE source_type = 'daily_service_record'
billed_value       = Σ over issued statements of (charges_minor + service_adjustments_minor)
collected          = − ( Σ PAYMENT + Σ ADJUSTMENT WHERE source_type = 'payment' )
outstanding        = Σ all ledger entries
```

Four separate functions in `app/billing/reporting.py`, each filtering on `source_type` and never on
sign, and none computed from another. Billed value is read from **statements**, not from the ledger.
No dashboard route was added — dashboards are a later package.

**The exact regression case passes**, asserted figure by figure: a 1000 service charge, a 500
payment, then that payment voided leaves outstanding **1000**, business generated **1000 — not
1500**, and collected **0**, with the void producing a payment-origin `ADJUSTMENT` of +500 and no
service-origin row changed.

---

## 5. Tests

**447 passed, 0 failed, 0 skipped** — final full run against PostgreSQL 16. The P1 suite
still passes unchanged in intent; 193 tests were added across P2 and the two review rounds (160 in
P2; 19 in the first review — 9 close boundary, 8 SYN-16 row versions, 2 schema; 14 in the final
review for the inclusive `period_end` boundary and the expired-open-cycle audit, covering the
August/September walkthrough A..H end to end).

| File | Tests | Covers |
| --- | --- | --- |
| `test_schema.py` | 65 | P1 live-schema assertions; inventory and versioned set now span P1+P2 |
| `test_service_records.py` | 40 | FIN-3/4/6/7, SYN-4, AUD-2..6 |
| `test_payments.py` | 38 | PAY-1..9, FIN-10, FIN-13, AUD-2/3/6/9, SYN-14 |
| `test_schema_p2.py` | 41 | live schema for the three new tables, the composite posting-cycle FK, the immutability trigger, the two new `row_version` columns |
| `test_architecture.py` | 35 | A-SLOT-5/6, A-AUD-1, FIN-1/8/12, AUD-7, no-future-scope |
| `test_auth_and_capabilities.py` | 29 | SEC-5/7/8/11 + the P2 financial capabilities |
| `test_money.py` | 28 | FIN-1/2/3 |
| `test_reporting.py` | 26 | FIN-4/5/11/14/15/16 including the A-FIN-14 regression |
| `test_tenant_isolation.py` | 25 | SEC-2/3/4/6 extended to every P2 route |
| `test_billing_cycles.py` | 47 | period arithmetic, one-open-cycle, the inclusive close boundary, expired-cycle rollover (A..H), posting resolution, FIN-9 |
| `test_clock.py` | 19 | R4 business date |
| `test_statements.py` | 19 | FIN-3/6/7/8/9, carry-forward, immutability |
| `test_idempotency.py` | 14 | SYN-1/2/3/13/14/15 |
| `test_row_version.py` | 21 | shared-sequence semantics; SYN-16 for payments and statements |

Still no network call, no adapter, no AI and no speech provider anywhere in the suite. The suite
still **aborts** (exit 4) without `TEST_DATABASE_URL` — no SQLite fallback, no conditional skip, no
skipped PostgreSQL correctness suite. The migration was also verified to `downgrade base` and
`upgrade head` cleanly.

Run it exactly as before:

```
cd backend
docker compose -f docker-compose.test.yml up -d
export TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:55432/rsp_test
pytest
```

The project virtualenv `.venv` (Python 3.13.2) was used, closing P1 risk R3.

---

## 6. Invariants: covered vs. deferred

**Newly and fully covered by P2:** FIN-8, FIN-9 (including the close boundary), FIN-10, FIN-11,
FIN-13, FIN-14, FIN-15, FIN-16, PAY-1..PAY-7, PAY-9, the new SYN-16, and the statement clauses of
FIN-3 and FIN-6 that P1 could only test at primitive level. AUD-1/2/3/6/7/9 now hold for payments as well as service records; SEC-1..SEC-4 and
SEC-6 extend to every new table and route; SYN-1/2/3/13/14/15 cover payments through the unchanged
idempotency mechanism.

**A-FIN-4/5** is now exercised in full — the property test replays random records, skips,
corrections, voids, payments and payment voids, which P1 could not do.

**Still partial or out of reach, and not claimed:**

| Criterion | Why not |
| --- | --- |
| A-FIN-1 (client half) | "no JavaScript arithmetic on `*_minor`" — no frontend exists |
| A-PAY-8 (offline half) | "record offline, restart the browser, sync" needs the PWA outbox. The server half — SYNC transport takes the identical validation path, and the audit row records it — *is* tested. **Superseded in P5:** A-PAY-8 was rewritten because payments are online-only in V1; the criterion is now "a `payment.record` sent to the sync endpoint is REJECTED with no effect", and it is tested |
| A-SEC-6 (platform half) | still vacuous: no `/platform/*` route exists |
| A-SEC-9 | no pre-commit or CI secret scanning exists. The repository contains no secret and `.env.example` is values-free, both asserted — but the criterion asks for scanning, which remains open from P1 |
| A-AUD-8 | the customer-history endpoint is P6. Voided payments are returned by `list_payments` and superseded records by the day listing, but the endpoint the criterion names does not exist |

**Not applicable in P2, and not claimed:** REM-*, COM-*, VOI-* beyond the provenance column,
SYN-5..SYN-12 (client-side), A-SLOT-1..4 (no adapters exist, so they would pass vacuously).
No frontend, offline, voice, reminder or commission acceptance is claimed.

---

## 7. Defects found, and decisions worth reviewing

**D1 — the P1 route-enumeration guards were silently vacuous (real defect, fixed).**
`tenant_scoped_routes()` read `app.routes` directly. FastAPI ≥ 0.141 no longer flattens an included
router onto `app.routes`; it stores a `_IncludedRouter` wrapper. The helper therefore returned an
**empty list**, which made `test_SEC6_platform_token_rejected_on_every_tenant_route` and
`test_route_inventory_is_covered` pass over zero routes — exactly the "new scoped route escapes the
suite" failure A-SEC-3/4 exists to prevent. Only `test_openapi_enumerates_the_same_routes` failed,
and it was failing on this machine before any P2 code was written. Fixed by descending through
included routers, plus a new `test_route_enumeration_is_not_vacuous` that fails if the enumeration
ever returns nothing again. This is a genuine finding: without it, none of P2's six new routes would
have been covered by the isolation suite.

**D2 — statements are issued at cycle close, not through a route.** Not a defect, but a decision a
reviewer should agree with. P0 §15 lists no statement-issuing endpoint, the frozen capability map has
no capability for one, and issuing from an open cycle would let a later posting contradict an
immutable document. Close therefore issues, in the same transaction. If statements should instead be
issued by the daily job, that job simply calls the same command in P4.

**D3 — statement immutability is enforced by a database trigger.** P0 says "fully immutable after
issue" and A-FIN-8 says "attempt every mutating path against it; each is rejected". Resting that on
the absence of a route is true today and one careless `UPDATE` away from false, so the table rejects
UPDATE and DELETE outright. `ledger_entry` and `audit_event` are still protected by source-level
guards only; extending the same trigger to them is a recommended hardening, not done here because
they are P1 tables and P2 should not rewrite what it did not build.

**R1 — `posting_cycle_id` is nullable and was not backfilled.** P1 wrote every entry with `NULL`, and
that package was never deployed, so there is no historical row to place and inventing a cycle for one
would be fabricating financial history. Statement issue instead **fails closed** while any NULL row
exists, with a test proving it. If a P1 database exists anywhere, those entries must be assigned
before a cycle is closed on it.

**D4 — `payment` and `statement` had no `row_version` (real defect, fixed in review; was R2).**
P0 §6 listed neither column, while §7.1 puts payment history and statements in the client's
authoritative offline `snapshot` and §7.4 pages that snapshot on `row_version > since`. Those two
statements cannot both be true, so the omission was an internal inconsistency rather than a design
choice — and the ledger entry a payment posts is **not** a substitute, because a client pulling
payment history cannot page on a different record's version. Both columns now exist, draw from the
shared P1 sequence, and are exposed on the API; the payment version advances on
`RECORDED -> VOIDED`, before the compensating entry draws its own later value. `billing_cycle` was
deliberately left unversioned: it is not a client sync entity. Recorded in P0 as new invariant
SYN-16 with acceptance A-SYN-16, and in §6 as a clarification of which tables carry the column.

**D5 — early cycle close was invented behaviour (removed in review; was R3).** P2 originally let a
cycle be closed at any time, which silently shortened the period and pushed its remaining days into
the next bill. That was never a frozen client or product decision. A cycle now cannot be closed
until its `period_end` has **passed** — `period_end` is inclusive, so the earliest valid close is
the following day, and closing *on* the final day is refused too, because business recorded later
that same day must still reach the cycle. The attempt raises `CYCLE_PERIOD_NOT_ENDED` (422),
evaluated against the tenant-local business date, and changes nothing. No override flag, no
synthetic shortened or extended cycle. Recorded in P0 §5.5 and FIN-9.

**D6 — an expired-but-open cycle silently absorbed the next period's business (real defect, found by
the final review's audit and fixed).** `ensure_open_cycle` returned whatever cycle was `OPEN`,
without checking that its period still covered today. Because rollover is not automatic, an August
cycle nobody had closed would accept a 1 September service or payment — September's business filed
under August, a mis-stated bill rather than a late one, and invisible because the ledger and the
statement identity both still balanced. It now fails closed with `CYCLE_ROLLOVER_REQUIRED` (409),
naming the stale cycle and the business date.

The stale cycle is deliberately **not** auto-closed from inside a service or payment command:
closing issues statements, and issuing a customer's bill as a side effect of somebody recording a
delivery is not a decision a write command may take. A scheduled rollover (P4's job runner) calls
the real close operation. The guard is one-directional and leaves the frozen rules untouched — an
entry may post to a cycle that *began* before it (backdating, and the §5.5 late-correction rule that
keeps `occurred_on` and posts the adjustment forward), never to one that *ended* before it. Recorded
in P0 §5.5 and FIN-9.

**R6 — `service_days` and `total_quantity` describe what the cycle billed, not what was delivered.**
They count the CHARGE entries posted to the cycle. A correction billed in a later cycle moves
`service_adjustments_minor` on that later statement but does not restate the earlier statement's
quantity — which is exactly what immutability requires, and is noted so nobody reads the two figures
as a delivery report.

**R7 — no load has been measured.** The four derivations are indexed integer sums with no cache, as
P0 requires. Correct at this scale; measure in P6 before considering materialisation.

**R8 — Docker dependency** (P1 R1, unchanged): no local PostgreSQL exists, so the suite runs against
the test-only container. CI must provide a PostgreSQL 16 service.

**P0 edits made in review, narrowly.** `P0_ARCHITECTURE_FREEZE.md`: §6 now says which tables carry
`row_version` and why, `statement` and `payment` list the column, and §5.5 gains the close-boundary
rule. `P0_INVARIANTS_AND_ACCEPTANCE.md`: new SYN-16 + A-SYN-16, and FIN-9 / A-FIN-9 gain the inclusive
close boundary and the expired-open-cycle rule. Nothing else was touched and no settled prose was rewritten for style. D2 and D3 remain
implementation decisions recorded here only.

---

## 8. Files

**Created**

```
backend/alembic/versions/0002_p2_financial_engine.py
backend/app/billing/cycles.py  statements.py  reporting.py
backend/app/payments/__init__.py  models.py  commands.py
backend/tests/_ops.py
backend/tests/test_billing_cycles.py  test_statements.py  test_payments.py
              test_reporting.py  test_schema_p2.py
docs/P2_HANDOVER.md
```

**Modified**

```
backend/app/billing/models.py     + BillingCycle, Statement; posting_cycle_id FK
backend/app/billing/ledger.py     posting-cycle resolution; post_payment, post_payment_adjustment
backend/app/core/clock.py         validate_business_date extracted; validate_service_date delegates
backend/app/tenancy/context.py    + now, cycle_type, cycle_start_day
backend/app/audit/models.py       + payment.recorded, payment.voided, billing_cycle.closed
backend/app/audit/service.py      + payment and billing_cycle audit allow-lists
backend/app/db_models.py          + P2_TABLES, ALL_TABLES, payments models import
backend/app/api/schemas.py        + CloseCycleRequest, RecordPaymentRequest, VoidPaymentRequest
backend/app/api/routes.py         + billing, statement and payment routers
backend/app/core/errors.py        + CyclePeriodNotEndedError (CYCLE_PERIOD_NOT_ENDED, 422)
                                  + CycleRolloverRequiredError (CYCLE_ROLLOVER_REQUIRED, 409)
backend/app/main.py               registers the three new routers
backend/tests/conftest.py         truncates ALL_TABLES
backend/tests/test_architecture.py, test_schema.py, test_tenant_isolation.py,
              test_auth_and_capabilities.py, test_row_version.py
CLAUDE.md                         phase
docs/P0_ARCHITECTURE_FREEZE.md    §6 row_version scope; statement/payment columns;
                                  §5.5 inclusive close boundary + expired-open-cycle rule
docs/P0_INVARIANTS_AND_ACCEPTANCE.md  + SYN-16/A-SYN-16; FIN-9/A-FIN-9 close boundary
```

---

## 9. Recommended next package

**P3 — Commission engine** (platform scope), or **P4 — reminders and the daily job**, in either
order. P3 is the better next step: the four reporting derivations it needs as a basis
(`RECORDED_VALUE`, `BILLED_VALUE`, `COLLECTED_VALUE`) now exist and are tested, D4 can be answered
against real semantics, and the commission tables are the last purely-financial ones. P4 depends on
statements existing, which they now do, but also wants the job runner and the `CommunicationProvider`
port — a wider surface.

The payment/statement sync cursor is **no longer** an open item — it was closed in this review
(§7 D4). **A-SEC-9** (CI secret scanning) is still open from P1 and is cheap to close.

---

## 10. Git state

Branch `main`, base `22df746`. **Nothing was committed and nothing was pushed**, as instructed —
neither for P2 nor for this review. `git diff --check` reports no whitespace errors. All changes are
in the working tree: the modified files listed in §8 plus the untracked new files.
