# P3 — Handover

Package: **P3 — Commercial Tracking / Commission**. Backend and tests only. No frontend, no
adapters, no network call, nothing committed.

**Base commit:** `ccf08a5` — "Implement P2 financial engine", branch `main`, working tree clean at
start.
**Migration revision:** `0003_p3_commission_engine` (`down_revision = "0002_p2_financial_engine"`).

**Session note.** P3 was implemented across two sessions; the first hit its usage limit after
writing `app/core/money.py` and `app/commission/models.py`. The second recovered state from git
(P2 committed at `ccf08a5`, only those two files uncommitted), kept both, and continued from the
plan/engine/settlement layer onward. Nothing was restarted or rewritten.

---

## 1. Scope implemented

The bounded commission engine of P0 §11: plans as data, snapshotted earning events, signed
adjustments at the original terms, aggregate settlement, the platform commercial position, and a
platform-only API surface.

**Deliberately not built:** reminders, communication, GHL, the job runner, the bulk sync HTTP
endpoint, dashboards, search, voice, speech, AI, frontend, any payment provider, any
settlement-allocation table, any `settlement_id` column. `app/adapters/` still does not exist and
`app/ports/` is still an empty placeholder. Guard tests assert each absence rather than assuming it.

**Nothing P1 or P2 built was weakened.** The four §11.1 tenant derivations, the ledger, statements,
cycles, payments, idempotency, tenancy and audit are unchanged in behaviour. The only edits to
existing modules are six one-line commission hooks at the accepting-command sites, a widened type
hint on `execute_idempotent`, and additive audit actions.

---

## 2. Tables and constraints

Four tables, exactly the P0 §6 set for this package. **None carries `row_version`** — P0 §6 lists
the `commission_*` family among the server-side tables that are not client sync entities, and a
schema test asserts the column's absence.

**`commission_plan`** — `tenant_id`, `basis`, `rate_bp`, `fixed_amount_minor`, `currency`,
`effective_from`, `effective_to`, `created_by_user_id`, `created_at`.
`uq_commission_plan_tenant_id_id` (composite FK target) · `ix_commission_plan_tenant_id_effective_from` ·
CHECKs `basis_valid`, `rate_bp_range` (0..10000), `fixed_amount_non_negative`,
`effective_range_ordered`, and **`exactly_one_term_for_basis`** — which encodes both halves of the
P0 rule in one predicate: a `PER_EVENT` plan carries a fixed amount and no rate, every other basis
carries a rate and no fixed amount.
**`ex_commission_plan_effective_range_no_overlap`** — an `EXCLUDE USING gist (tenant_id WITH =,
daterange(effective_from, effective_to, '[]') WITH &&)`. This *is* the non-overlap guarantee; a
`NULL` `effective_to` is an unbounded upper bound, so an open-ended plan excludes every later one
until it is closed.

**`commission_event`** — the P0 §6 column list verbatim.
`uq_commission_event_tenant_id_source_type_source_id` (COM-5) · `uq_commission_event_tenant_id_id` ·
composite FK `(tenant_id, plan_id) → commission_plan` · `ix_commission_event_tenant_id_occurred_on` ·
CHECKs `basis_snapshot_valid`, `source_type_valid`, `rate_bp_snapshot_range`,
`exactly_one_snapshot_for_basis`.

**`commission_adjustment`** — the P0 §6 column list verbatim.
`uq_commission_adjustment_tenant_id_source_type_source_id` (COM-5) · composite FK
`(tenant_id, commission_event_id) → commission_event` ·
`ix_commission_adjustment_tenant_id_commission_event_id` · CHECKs `source_type_valid` and
`reason_not_blank` (AUD-6 in the database). `created_by_user_id` is nullable: an adjustment is a
consequence of a business event, not an authored document.

**`commission_settlement`** — the P0 §6 column list verbatim.
`ix_commission_settlement_tenant_id_settled_on` · CHECKs `period_ordered` and **`amount_positive`**
(`amount_minor > 0`). It has **no foreign key to any earning row**, asserted by a test.

**Immutability triggers.** `commission_event`, `commission_adjustment` and `commission_settlement`
each reject UPDATE and DELETE via `commission_row_is_immutable()`, the same hardening P2 applied to
`statement` (P2 §7 D3 recommended extending it). `commission_plan` deliberately has **no** trigger:
closing an open-ended range is its one permitted lifecycle transition.

**`btree_gist`.** The migration runs `CREATE EXTENSION IF NOT EXISTS btree_gist`, required for the
UUID equality operator inside the GiST exclusion constraint. What forced it: P0 §6 says effective
ranges "must not overlap per tenant", and P0 §3.4's standard for such guarantees is "impossible at
the database level, not merely unlikely" — the alternative is a read-then-write check that races.
It is a standard contrib module present on every host P0 §14 contemplates. A schema test asserts it
is installed, so a rebuilt database without it fails loudly rather than silently losing the
guarantee. `downgrade()` leaves it installed; dropping a database-wide facility is not this
migration's call.

---

## 3. Bases and triggers

New module `app/commission/` (a P0 §2.1 domain module, not a new layer): `models.py`, `plans.py`,
`engine.py`, `settlements.py`, `reporting.py`.

Commission is created by six hooks, each called from the command that accepts the source event,
**inside that command's transaction** (COM-2). There is no route that creates an event or an
automatic adjustment, and no client can reach one.

| Basis | Earns on | Base amount | Reverses on |
| --- | --- | --- | --- |
| `RECORDED_VALUE` | accepted `SERVICE` record | `charge_minor` (FIN-14) | correction / void |
| `PER_EVENT` | accepted `SERVICE` record | fixed amount, base recorded for traceability | void or correction to `SKIP` |
| `COLLECTED_VALUE` | accepted manual payment | `amount_minor` (FIN-16) | payment void |
| `BILLED_VALUE` | issued statement, at cycle close | `charges_minor + service_adjustments_minor` (FIN-15) | never — a statement is immutable |

**A `SKIP` earns nothing under any basis.** P0 §11 pays `PER_EVENT` "per accepted *service*
record", and a skip has no service value. This is the specific accident the basis invites and it is
tested directly.

**A correction chain earns once, at its head**, and later links post only the difference — exactly
as the ledger does. The commission adjustment carries the **same source identity as the compensating
ledger entry** (the record whose accepted life just ended), which is why COM-5 uniqueness can never
collide: a record is superseded or voided exactly once.

**Two paths through a correction**, and they are different facts:
* the chain already earned → one adjustment for the difference at the original terms;
* the chain never earned and the replacement is a commissionable `SERVICE` (a `SKIP` corrected into
  a delivery) → newly accepted service value, so it earns an event under the plan in force for its
  service date. Without this, correcting a skip into a real delivery would create service value that
  never earned anything.

**Zero-value adjustments are written.** COM-4 says a correction produces *exactly one* adjustment;
"this correction moved commission by nothing" (a `PER_EVENT` quantity correction, say) is a fact
worth reading back, and a missing row would be indistinguishable from a nil effect.

**Rounding** is `apply_rate_bp(base, rate_bp) = round_half_up(base * rate_bp / 10000)`, added to
`app/core/money.py` and sharing `_round_half_up` with `compute_charge_minor` — one rounding
implementation in the system, not two. It is symmetric about zero, so a reversal returns exactly
the magnitude it earned. Integer basis points in, integer minor units out, no float anywhere.

---

## 4. Adjustments and settlement

An adjustment never rewrites an event. It is signed, immutable, linked to the original event, and
computed **only** from that event's snapshotted terms — a plan change after the fact cannot reach
it. For a rated basis the amount is the rate applied to the *difference* in base; for `PER_EVENT`
the fee never depended on a base, so the only question is whether the accepted event still exists.

Settlement is independent and additive: it references no event, stamps nothing, and consumes
nothing. The period on the row is descriptive — it names what was agreed, and does not filter or
lock any event.

```
commission_outstanding = Σ event.commission_minor + Σ adjustment.amount_minor − Σ settlement.amount_minor
```

**The A-COM-6 regression passes exactly:** earn 1000 across two events → settle 400 → outstanding
600 → settle 600 → outstanding 0, with every earning row asserted **field by field** to be
unchanged from creation at each step. **A-COM-6b** passes too: settling 1200 against 1000 yields
−200 with no error, and a later adjustment still applies cleanly on top (−700).

**A settlement row is strictly positive** (`amount_minor > 0`, at the database, the domain and the
request schema). A settlement is money that moved from the tenant to the platform; a negative row
would be a commission adjustment wearing a settlement's clothes — moving outstanding with no
snapshotted terms, no link to an earning event and no source fact, which is exactly the
traceability COM-4 exists to guarantee. Commission moves through an adjustment or it does not move.

That is a **different question from the sign of the aggregate**, and the two are tested separately:
over-settlement stays fully representable, because settling 1200 against 1000 earned is a *positive*
row that drives outstanding to −200. The aggregate formula is unchanged.

---

## 5. Authorization

Commission is platform scope only, and P3 added **no capability**: P0 §3.2 froze
`commission:read | adjust | settle`, all on `PLATFORM_OWNER`, and the tenant set holds none of them.
That disjointness is what makes COM-7 true without per-route cleverness.

```
GET  /api/v1/platform/commission/summary?tenant_id=       commission:read
GET  /api/v1/platform/commission/plans?tenant_id=         commission:read
POST /api/v1/platform/commission/plans                    commission:adjust   (idempotent)
POST /api/v1/platform/commission/settlements              commission:settle   (idempotent)
```

`PlatformContext` is a new type beside `TenantContext`, not a flag on it: a tenant principal cannot
produce one, and the target tenant is the platform caller's **explicit** choice rather than a token
claim. There is deliberately no implicit "my tenant" for a platform principal — omitting `tenant_id`
is a 422, and an unknown tenant is a 404.

`tenant_id` in a platform request body is the opposite of the SEC-3 defect, not an instance of it:
SEC-3 forbids deriving a *tenant principal's* scope from the request. A platform principal has no
tenant of its own, so naming one is the authority being exercised, and it is audited as such.

**No manual-adjustment route was added.** COM-8 names "create an adjustment" as platform authority,
but P0 §15's frozen platform surface lists exactly summary, plans (GET/POST), settlements (POST) and
tenants — no adjustment route — and COM-4 defines an adjustment as arising from a corrected, voided
or reversed **source**. A manual adjustment has no source fact and would need an invented source
identity to satisfy COM-5. Every adjustment in the system is created by platform-owned engine code
that no tenant route can reach, which is what COM-8 protects. If a manual adjustment is genuinely
wanted later, it needs a source-identity decision first.

The isolation suite now enumerates the tenant and platform surfaces **separately** — mixing them
would make one of the two guarantees untestable — and asserts a tenant token is refused on every
platform route with a *valid* body (so the refusal is authorization, not request validation), a
platform token is accepted, and no new platform route can escape the suite.

---

## 6. Tests

**668 passed, 0 failed, 0 skipped** — final full run against PostgreSQL 16. The P1 and P2 suites
still pass unchanged in intent; **214 tests were added** by P3 (190 in the package, 24 in the
review), plus one existing P2 test rewritten (D5). The migration was also verified to
`downgrade 0002` / `upgrade head` and `downgrade base` / `upgrade head` cleanly.

| File | Tests | Covers |
| --- | --- | --- |
| `test_schema_p3.py` | 54 | live schema for the four tables, EXCLUDE constraint, triggers, no `row_version`, COM-11 |
| `test_commission_engine.py` | 49 | COM-2/3/4/5/9/10, all four bases, A-COM-2/3/4/10, the basis-correction matrix |
| `test_commission_plans.py` | 48 | COM-1/8/9, exactly-one-term, non-overlap, plan-transition safety |
| `test_commission_settlements.py` | 33 | COM-6/11, A-COM-6, A-COM-6b, settlement sign, immutability triggers |
| `test_commission_authority.py` | 30 | COM-7/8, SEC-5/6, no tenant leakage, idempotency |

The eight cases the brief named are all present and named in the code:

1. event at 250 bp → plan moves to 500 bp → the event still reads 250 bp, asserted **column by
   column** against its creation snapshot;
2. correct 3 units to 2 after the rate moved → the adjustment is −625 (250 bp), explicitly asserted
   *not* to equal the 500 bp figure, and linked to both the source and the original event;
3. `COLLECTED_VALUE` payment then void → commission reverses to 0, while business generated stays
   100000 and the same void under `RECORDED_VALUE` moves no commission at all;
4. earn 1000 → settle 400 → 600 → settle 600 → 0, earning rows untouched throughout;
5. over-settlement 1200 against 1000 → −200, with a later adjustment applying on top;
6. an owner-admin token is refused on every commission route, and no commission field appears in any
   tenant response body, any tenant OpenAPI schema, or any tenant serializer;
7. basis switch `RECORDED_VALUE → COLLECTED_VALUE` → the prior event is byte-identical and only
   subsequent triggers differ;
8. a replayed operation and a repeated source create no second event or adjustment — proven through
   the register (DUPLICATE) *and* by the database refusing a direct duplicate insert.

A-COM-2 is tested at the transaction boundary: the acceptance runs without commit, a **second
connection** sees zero commission events, and exactly one appears after commit.

The review added three focused groups on top of those:

* **The basis-correction matrix**, with exact figures per basis — 1000 corrected to 700 adjusts on
  −300 and creates no second event; a `PER_EVENT` correction-then-void yields adjustments `[0, −700]`
  against one event and nets to zero; a 500 payment earns 50 and reverses 50 while business
  generated stays 1000. The `BILLED_VALUE` double-count the review asked about **does not occur**:
  a correction after issue produces no adjustment and no early reversal, and the next statement
  carries a base of −300, so the total is `rate x 700` exactly once. The void variant nets to zero
  the same way.
* **Plan-transition safety** — determinism, one plan per date (checked against PostgreSQL over a
  generated date series), unchanged event snapshots across a basis change, idempotent retry, a
  failed transition leaving the predecessor open, and backdating into the superseded range.
* **Settlement sign** — refused at the schema, the domain and the database, with the over-settlement
  case asserted to remain accepted.

The suite still **aborts** (exit 4) without `TEST_DATABASE_URL` — no SQLite fallback, no conditional
skip, no skipped PostgreSQL correctness test. Run it exactly as before:

```
cd backend
docker compose -f docker-compose.test.yml up -d
export TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:55432/rsp_test
pytest
```

---

## 7. Invariants covered, deferred, and decisions

**Newly and fully covered by P3:** COM-1 … COM-11 in full. SEC-5 and SEC-6 now have a real platform
surface to test against — A-SEC-6's platform half was vacuous in P1 and P2 and is no longer.
AUD-1 extends to the three commission history tables (database-enforced), AUD-6 to commission
adjustments, AUD-9 to platform-scope actions. SYN-1/2/3/14 cover the two platform writes through the
unchanged register. FIN-14/15/16 are re-asserted from the commission side: each basis reads its own
§11.1 derivation, and the payment-void case proves the two do not contaminate each other.

**Still open, and not claimed:** everything P2 listed — A-FIN-1 (client half), A-PAY-8 (offline
half), **A-SEC-9** (CI secret scanning, open since P1 and still cheap to close), A-AUD-8 (the
customer-history endpoint is P6) — plus REM-*, VOI-*, SYN-5..12 and A-SLOT-1..4, none of which P3
touches.

> **Updated 2026-09-03 (P5).** A-FIN-1's client half and SYN-5..12 were closed by P5. A-PAY-8 was
> *rewritten* rather than closed: payments are online-only in V1, so its offline half no longer
> exists as a criterion. A-SEC-9 and A-AUD-8 remain open.

**D1 — the plan is resolved by the source fact's business date, not by "today".** P0 §11 says an
event copies the terms "in force at the time"; the brief said "effective when the earning event was
created". These coincide for same-day work and differ for a backdated record or a plan changed
mid-period. P3 resolves on the source fact's own business date, because plan ranges are business
dates and pinning terms to the *business event* is the only reading under which snapshotting means
anything: a delivery made in March and synced in April must not earn April's rate.

**The exact date used, per basis.** It is also what `commission_event.occurred_on` is set to, so the
row records the date that chose its terms:

| Basis | Source | Date used for plan resolution and `occurred_on` |
| --- | --- | --- |
| `RECORDED_VALUE` | `daily_service_record` | `record.service_date` |
| `PER_EVENT` | `daily_service_record` | `record.service_date` |
| `COLLECTED_VALUE` | `payment` | `payment.received_on` |
| `BILLED_VALUE` | `statement` | `cycle.period_end` — the last day of the period the statement bills, **not** `issued_at`. The issue instant is operational (whoever closed the cycle, whenever they got to it); the period end is the business date the billed value belongs to, and it is stable however late the close happens. |

An **adjustment resolves no plan at all** — it reads the terms off the event it compensates, so no
date question arises for it.

Consequence worth knowing: business dated before the earliest plan earns nothing, which is the
conservative outcome (no commission on work done before the deal existed). A test backdates a record
into a superseded plan's range *after* the transition and asserts it earns the old rate.
**This is the one P0 clarification P3 needs**, and it is recorded here rather than by editing P0,
because P0's wording is compatible with it.

**D2 — settlement is `amount_minor > 0` (corrected in review).** P3 first shipped `<> 0`, reasoning
that an append-only table needs an opposite row to correct a mistake. That was wrong, and the review
caught it: a negative settlement is a back door into commission outstanding that bypasses
`commission_adjustment` entirely — no snapshotted terms, no event link, no source fact, nothing for
an audit to follow. COM-4 makes the adjustment the *only* instrument that moves earned commission,
and a negative settlement would be a second one carrying none of its guarantees. It is now `> 0` in
the CHECK (`ck_commission_settlement_amount_positive`), in `record_settlement`, and in the request
schema (`Field(gt=0)`).

Over-settlement is untouched by this and is a separate question: 1200 against 1000 earned is a
positive row that leaves outstanding at −200 (A-COM-6b). A settlement recorded in error is corrected
the way this system corrects everything — by recording what is actually true next period — not by
writing a payment that never happened.

**D3 — creating a plan closes its open-ended predecessor.** P0 §15 exposes no plan-edit route, and
the acceptance criteria require changing a rate and a basis, so `POST /plans` must either supersede
or refuse. The exact semantics, audited in review and left unchanged because no defect was found:

* A new plan starting on *D* sets the open-ended predecessor's `effective_to = D − 1 day`, in the
  same transaction, audited as `commission_plan.closed` with before/after. No gap, no overlap, and
  no dependence on when the call happened to be made.
* Anything else that would overlap — a plan already carrying an end date, or one starting on or
  after the new date — is a `COMMISSION_PLAN_OVERLAP` 409, never a silent re-dating.
* The close and the insert share one transaction, so a refused plan leaves the predecessor open.
* Earned history is never read or written by the transition: event snapshots are asserted unchanged
  across both a rate change and a basis change.
* Exactly one plan covers every date across the boundary — asserted through `effective_plan` *and*
  by asking PostgreSQL directly over a generated date series — and the EXCLUDE constraint makes two
  simultaneously applicable plans impossible whatever application code does.
* A retried plan creation is a `DUPLICATE` through the existing register: one plan, and the
  predecessor keeps the single end date it was given.
* The transition governs *dates*, not the wall clock: a record backdated into the superseded range
  after the change still earns the old terms (D1).

No retroactive-plan ban was introduced — none was needed, and it would have been invented product
policy.

**D4 — commission tables carry no `customer_id`.** P0 §6 lists none, and a test asserts none
appeared. Commission is a tenant-level commercial arrangement; per-customer attribution would be a
new product decision, not an implementation detail.

**D5 — a P2 test pinned the migration head (fixed).** `test_the_migration_chain_is_at_the_p2_head`
asserted `alembic_version == "0002_p2_financial_engine"`, which fails the moment any later package
adds a migration — as P3 did. Not a correctness defect, but a guard that would have had to be
rewritten by every future package. It now walks the Alembic script directory from the applied head
back to base and asserts P2's revision is in that ancestry, so it stays true as the head moves and
still fails if a future migration ever drops P2 out of the chain. The head assertion belongs to the
newest package and lives in `test_schema_p3.py`.

**No defect was found in P1 or P2 *behaviour*.** The P2 review had already closed the
route-enumeration vacuity (P2 D1), and P3's platform surface is covered by the extended version of
that same mechanism.

**R1 — the correction adjustment is rate × Δbase, not a recomputation.** For a chain of corrections,
`Σ` of the rounded differences can differ by a minor unit from `rate × final_base` rounded once.
The brief specifies the difference form ("commission adjustment based on −300 at ORIGINAL plan
terms") and it is the only form that can use the original terms at all once a plan has moved, so it
is what P3 implements. The divergence is at most one minor unit per correction and is inherent to
correcting at historical rates.

**R2 — no load has been measured.** The four position figures are indexed integer sums with no
cache, as P0 requires. Correct at this scale; measure in P6 before considering materialisation.

**R3 — Docker dependency** (P1 R1, P2 R8, unchanged): CI must provide a PostgreSQL 16 service, and
it must permit `CREATE EXTENSION btree_gist`.

---

## 8. Files

**Created**

```
backend/alembic/versions/0003_p3_commission_engine.py
backend/app/commission/__init__.py  models.py  plans.py  engine.py
                                    settlements.py  reporting.py
backend/tests/_commission.py
backend/tests/test_commission_plans.py  test_commission_engine.py
              test_commission_settlements.py  test_commission_authority.py
              test_schema_p3.py
docs/P3_HANDOVER.md
```

**Modified**

```
backend/app/core/money.py         + RATE_BP_SCALE, _round_half_up (shared), validate_rate_bp,
                                    apply_rate_bp; compute_charge_minor now uses the shared rule
backend/app/core/errors.py        + CommissionPlanOverlapError (COMMISSION_PLAN_OVERLAP, 409)
backend/app/tenancy/context.py    + PlatformContext
backend/app/sync/idempotency.py   execute_idempotent accepts a PlatformContext
backend/app/service/commands.py   + 3 commission hooks (record, correct, void)
backend/app/payments/commands.py  + 2 commission hooks (record, void)
backend/app/billing/statements.py + 1 commission hook (statement issued)
backend/app/audit/models.py       + commission_plan.created/.closed, commission_settlement.recorded
backend/app/audit/service.py      + commission_plan and commission_settlement allow-lists
backend/app/db_models.py          + P3_TABLES, ALL_TABLES, commission models import
backend/app/api/schemas.py        + CreateCommissionPlanRequest, RecordCommissionSettlementRequest
backend/app/api/deps.py           + build_platform_context
backend/app/api/routes.py         + platform_commission_router and its four routes
backend/app/main.py               registers the platform router
backend/tests/conftest.py         + platform_user fixture (platform_token now derives from it)
backend/tests/test_architecture.py    commission in DOMAIN_PACKAGES and APPEND_ONLY_*;
                                      forbidden symbols now target settlement allocation
backend/tests/test_schema.py          commission tables no longer forbidden; allocation is
backend/tests/test_schema_p2.py       the head-pinning assertion became an ancestry assertion (D5)
backend/tests/test_tenant_isolation.py  tenant and platform surfaces enumerated separately
CLAUDE.md                         phase, module list, commission invariant
```

`docs/P0_ARCHITECTURE_FREEZE.md` and `docs/P0_INVARIANTS_AND_ACCEPTANCE.md` were **not** modified —
P3 found no defect in them and D1 above is a clarification P0's own wording permits.

---

## 9. Recommended next package

**P4 — reminders and the daily job.** It is the last purely-backend package: it needs the job
runner, `job_run`, the `reminder` table and the `CommunicationProvider` port with a mock. Statements
exist (P2) and the commercial engine is closed (P3), so nothing financial blocks it, and P2's D6
noted that a scheduled cycle rollover is exactly what the job runner should own — that rollover is
the first thing P4 should wire, because an expired-but-open cycle currently fails writes closed
until someone closes it by hand.

**A-SEC-9** (CI secret scanning) is still open from P1 and remains cheap to close.

---

## 10. Git state

Branch `main`, base `ccf08a5`. **Nothing was committed and nothing was pushed**, as instructed.
`git diff --check` reports no whitespace errors. All changes are in the working tree: the modified
files listed in §8 plus the untracked new files.
