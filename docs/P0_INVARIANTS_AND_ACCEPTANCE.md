# P0 — Invariants and Acceptance Criteria

The rules an implementation must satisfy, and the tests that prove it.

Every invariant has a stable ID. Cite the ID in test names (`test_FIN3_price_change_does_not_alter_history`)
so a future package can prove coverage. An invariant with no failing-case test is not considered
implemented.

Terms: **accepted** = committed on the central server. **outstanding** = the ledger sum defined in
FIN-4. **minor units** = the integer money representation. **tenant scope / platform scope** = the
two principal kinds from the architecture freeze.

---

## 1. Financial invariants (FIN)

| ID | Invariant |
| --- | --- |
| FIN-1 | All money is an integer count of minor units in the database, the domain, and the JSON API. No float or IEEE-754 value ever holds, transports, or computes money. |
| FIN-2 | Quantity is `NUMERIC(12,3)` / `Decimal` and is never assumed to be an integer. The unit label is tenant configuration, never a literal in code. |
| FIN-3 | `charge_minor = round_half_up(quantity × unit_price_minor)`, rounded exactly once, at the daily service record. Nothing downstream re-rounds. |
| FIN-4 | `outstanding(customer) = SUM(ledger_entry.amount_minor)` for that tenant and customer. No other definition of a balance exists anywhere in the system. |
| FIN-5 | `Previous Outstanding + Cycle Charges − Payments (± Adjustments) = Current Outstanding` holds for every customer at every instant. |
| FIN-6 | Every accepted daily service record snapshots the `unit_price_minor` and `unit_label` used. Changing a customer's current price never alters any existing record, charge, ledger entry, or statement. |
| FIN-7 | A `SKIP` records a real row with `quantity = 0` and `charge_minor = 0`, and creates **no** ledger entry. |
| FIN-8 | A statement is immutable once issued. Its `closing_balance_minor` equals `opening + charges + service_adjustments − payments + payment_reversals` for entries posted to its cycle, and its opening balance equals the previous statement's closing balance. Adjustments are stored split by origin, never as one mixed figure. |
| FIN-9 | A correction or void affecting a closed cycle keeps its original `occurred_on` but posts to the currently `OPEN` cycle. No issued statement is ever rewritten. `period_end` is inclusive, so a cycle may not be closed until `business_date > period_end`: every cycle covers a full configured period, and business dated on `period_end` stays eligible for it. An `OPEN` cycle whose period has already ended accepts no new entry — the write fails closed asking for the rollover rather than filing new business under a finished period — while backdating into an earlier, closed period is still accepted and posts to the current open cycle. *(Both sentences added in P2: closing early silently shortened a period and moved its remaining days into the next bill, and an expired-but-open cycle silently swallowed the next period's business. Neither was a client decision.)* |
| FIN-10 | Payments support full, partial, and none. Any positive amount is accepted, including an overpayment, which yields a negative (credit) outstanding rather than an error. |
| FIN-11 | Customer status is derived from FIN-4 on every read; it is never a stored, cached, or client-computed field. |
| FIN-12 | `ledger_entry` is append-only. No code path issues `UPDATE` or `DELETE` against it. |
| FIN-13 | Payments are manual in V1 and the financial engine is complete without any external provider: full, partial, none, and overpayment all behave, and the payment ledger, derived status, and reversal rules carry no gateway dependency. |
| FIN-14 | **Business generated / recorded service value** = `Σ CHARGE + Σ ADJUSTMENT WHERE source_type = 'daily_service_record'`. Payment-origin adjustments are excluded from it. Voiding a payment changes outstanding and collections; it never changes business-generated value. |
| FIN-15 | **Billed value** = `Σ over issued statements of (charges_minor + service_adjustments_minor)`. It is defined separately from FIN-14 and never conflated with it: service in the open cycle is generated but not yet billed. |
| FIN-16 | **Collected** = `−(Σ PAYMENT + Σ ADJUSTMENT WHERE source_type = 'payment')`. Payment-origin adjustments *are* included here. Business generated, billed value, collected, and outstanding are four distinct figures derived from one ledger by adjustment **origin**, not by sign. |

### Acceptance — FIN

- **A-FIN-1** A repository-wide scan finds no `float`, `Decimal`-typed money column, or JavaScript
  arithmetic on a `*_minor` field. A property test over random quantities and prices asserts every
  computed charge is an `int`.
- **A-FIN-2** `quantity = 1.5` at `unit_price_minor = 12000` yields `charge_minor = 18000`.
  `quantity = 0.333` at `unit_price_minor = 10000` yields `3330`.
- **A-FIN-3** Half-up boundary: `quantity = 0.5` at `unit_price_minor = 25` yields `13`, not `12`.
  A property test asserts `sum(individual charges) == statement.charges_minor` for 1000 random
  records — no drift.
- **A-FIN-4/5** Property test: a random sequence of records, skips, corrections, voids, payments,
  and payment voids, replayed in random order, always leaves
  `outstanding == opening + Σcharges + Σadjustments − Σpayments`.
- **A-FIN-6** Record 3 units at Rs. 250, change the customer price to Rs. 300, then read the record,
  the ledger entry, and the statement: all still show Rs. 250 and a Rs. 750 charge.
- **A-FIN-7** SKIP a day, then assert one `daily_service_record` exists with `kind = SKIP`, zero
  ledger entries for that date, and an unchanged outstanding.
- **A-FIN-8** Issue a statement, then attempt every mutating path against it; each is rejected. A
  three-cycle chain asserts `cycle[n].opening == cycle[n-1].closing`, and every statement satisfies
  `closing == opening + charges + service_adjustments − payments + payment_reversals`.
- **A-FIN-9** Close January, correct a 5 January record in February, then assert the January
  statement is unchanged in every field from its issued form and the adjustment appears on the February
  statement with `occurred_on = 5 January`. Assert also that attempting to close January on or
  before 31 January is refused, changes nothing, and issues no statement; that a service and a
  payment dated 31 January still post to the January cycle; and that with January still `OPEN` on
  1 February, a new 1 February service or payment fails closed rather than posting into January.
- **A-FIN-10** Bill 1000, pay 400 → `PARTIALLY_PAID`, outstanding 600. Pay 600 more → `PAID`,
  outstanding 0. Pay 100 more → outstanding −100, status `PAID`, no error.
- **A-FIN-13** With no payment-provider configuration of any kind present in the application,
  recording a cash payment succeeds and updates outstanding.
- **A-FIN-14** *(the payment-reversal case — the exact defect this rule prevents)* Record a 1000
  service charge, record a 500 payment, then void that payment. Assert: outstanding returns to
  1000; the void produced a **payment-origin** `ADJUSTMENT` of +500; business generated is
  **1000, not 1500**; and no service-origin row changed.
- **A-FIN-15** With one closed cycle and further service recorded in the open cycle, assert billed
  value counts only the issued statement while business generated counts both — the two figures
  differ, and neither is computed from the other.
- **A-FIN-16** In the A-FIN-14 scenario, collected moves 500 → 0 while business generated stays
  1000. A property test over random histories asserts that business generated is invariant under
  payment-origin adjustments, and that collected is invariant under service-origin adjustments.

---

## 2. Tenant and security invariants (SEC)

| ID | Invariant |
| --- | --- |
| SEC-1 | Every business row carries a non-null `tenant_id`. |
| SEC-2 | No row may reference a row belonging to another tenant. Composite foreign keys `(tenant_id, parent_id)` make this impossible at the database level, not merely unlikely. |
| SEC-3 | Every query issued on behalf of a tenant principal is filtered by that principal's `tenant_id`. There is no repository entry point that can omit it. |
| SEC-4 | A tenant principal requesting another tenant's resource identifier receives 404, never 403 and never data. Existence is not disclosed across tenants. |
| SEC-5 | No tenant role holds any `commission:*` capability. The tenant and platform capability sets are disjoint. |
| SEC-6 | Platform-scope endpoints reject tenant principals, and tenant business endpoints reject platform principals. |
| SEC-7 | The customer is not a login principal in V1: no credential column, no authentication route, no session can be issued for a customer. |
| SEC-8 | The `OPERATOR` role exists as a reserved value with an empty capability set and grants nothing. |
| SEC-9 | No secret appears in source control, in a document, in a fixture, or in a log line. Secrets are read from environment variables only. |
| SEC-10 | The internal job endpoint requires the shared job secret and is never reachable with a user token. |
| SEC-11 | Passwords are stored only as a modern slow hash. Refresh tokens are stored hashed and are revocable. |

### Acceptance — SEC

- **A-SEC-1/2** A schema test asserts every business table has `tenant_id NOT NULL`, and that every
  foreign key between business tables is composite and includes `tenant_id`. A direct SQL insert
  attempting a cross-tenant reference is rejected by the database.
- **A-SEC-3/4** The isolation suite creates tenant A and tenant B with identical data shapes, then
  drives **every** route as A against B's identifiers. Every response is 404. This test enumerates
  routes from the OpenAPI schema so a newly added route cannot silently escape it.
- **A-SEC-5** A unit test asserts `CAPABILITIES[OWNER_ADMIN] & {c for c in ALL if c.startswith("commission:")} == set()`.
- **A-SEC-6** An owner-admin token receives 403 on every `/platform/*` route; a platform token
  receives 403 on every tenant business route.
- **A-SEC-7** `/auth/login` with a customer's phone or identifier cannot produce a session; no
  credential column exists on `customer`.
- **A-SEC-9** A pre-commit secret scan runs in CI over the whole tree and fails the build on a hit.
- **A-SEC-10** `POST /internal/jobs/run-daily` returns 401 with no secret, with a wrong secret, and
  with a valid user access token.

---

## 3. Offline and idempotency invariants (SYN)

| ID | Invariant |
| --- | --- |
| SYN-1 | Every mutating operation carries a client-generated `operation_id`, generated once at user intent and never regenerated on retry. |
| SYN-2 | `(tenant_id, operation_id)` is unique. A replay creates nothing, fires no side effect, and returns the **same logical result** as the original acceptance — the same authoritative entity, semantically equal. Byte-identical serialization is explicitly *not* required and must not be promised: the stored result is JSONB, and raw response bytes are not retained. |
| SYN-3 | The `sync_operation` register row is written in the same transaction as the effect it records. A partial outcome — effect without register, or register without effect — is impossible. |
| SYN-4 | At most one `ACTIVE` daily service record exists per `(tenant, customer, service_date)`, enforced by a partial unique index, not by a pre-read check. |
| SYN-5 | A queued operation survives a page refresh and a full browser close/reopen. It is written to IndexedDB **before** any network attempt. |
| SYN-6 | An entry leaves the automatic retry queue only on a server verdict. `APPLIED` and `DUPLICATE` remove it outright. `REJECTED` and `CONFLICT` move it into the durable `issues` store in the same local transaction, so nothing is lost by the removal. A network error, a timeout, or a 5xx is not a verdict and leaves the entry queued for normal retry. |
| SYN-7 | A collision between devices produces `CONFLICT` with the server's authoritative state. The server never merges, never picks a winner, and never overwrites silently. The client never automatically resubmits a conflicting operation unchanged; it is parked in `issues` awaiting explicit human resolution, so a conflict can neither retry-loop nor silently overwrite another device. |
| SYN-8 | Synchronised operations pass exactly the same validation and authorization as online ones. There is no privileged offline path. |
| SYN-9 | Offline reads serve only previously synchronised server data. The client never computes a balance, charge, or status of its own. |
| SYN-10 | The sync cursor is monotonic; a client that replays a cursor receives a superset, never a gap. |
| SYN-11 | Sync state is always visibly one of: Synced, Offline, Last synced *time*, *N* changes waiting, Syncing, Needs Attention. *N changes waiting* counts `outbox`; Needs Attention is driven by `issues` and stays raised until every entry there is resolved — across a browser restart, and across later successful syncs of unrelated operations. |
| SYN-12 | The `issues` store is durable: entries survive a page refresh and a browser close/reopen, are never silently discarded, expired, or auto-resolved, and leave only by explicit user resolution or dismissal. |
| SYN-13 | The `sync_operation` register is retained indefinitely in V1 and never pruned. An `operation_id` once accepted stays replay-safe for the life of the system; no retention horizon exists that could become a duplication horizon. |
| SYN-14 | An `operation_id` is bound to the request that created it. Replaying `(tenant_id, operation_id)` with a **different** request payload is refused with an explicit idempotency-key-reuse conflict: the earlier result is never returned as though the requests matched, and the new request is never applied. Fails closed. *(Added in P1 as a conservative implementation clarification — SYN-2 defines the identical-replay case but left differing-payload reuse unspecified.)* |
| SYN-15 | The register claims `(tenant_id, operation_id)` **before** the effect runs, so the register's unique index — not whichever business constraint the effect happens to touch — is the serialization point for concurrent replays. Otherwise identical concurrent envelopes surface as `CONFLICT` on a business constraint instead of `DUPLICATE`. *(Added in P1.)* |
| SYN-16 | Every record that appears in the client's authoritative offline snapshot (§7.1) carries its own `row_version` from the shared sequence — `tenant`, `customer`, `daily_service_record`, `ledger_entry`, `payment`, `statement` — and it advances on every permitted mutation of that record, including `RECORDED -> VOIDED` on a payment. A related row's version is never a substitute for the record's own. Tables that are not client sync entities do not carry the column. *(Added in P2: §6 defined `payment` and `statement` without `row_version` while §7.1 and §7.4 required them to be pageable — a genuine internal inconsistency, not a design change.)* |

### Acceptance — SYN

- **A-SYN-1/2** Post the same operation envelope five times concurrently. Exactly one row is
  created; four responses are `DUPLICATE`, each carrying the same authoritative entity as the first,
  compared **semantically** (parsed and field-compared) rather than by raw bytes.
- **A-SYN-3** Fault injection aborts the transaction after the effect but before commit; assert
  neither the row nor the register entry exists, and that a retry then succeeds as `APPLIED`.
- **A-SYN-4** Two different `operation_id`s for the same customer and date: the first is `APPLIED`,
  the second is `CONFLICT` (not a second row, not an overwrite).
- **A-SYN-5** Playwright: go offline, CONFIRM ten customers, reload the page, close and reopen the
  browser context — all ten are still queued and the badge reads "10 changes waiting".
- **A-SYN-6** Simulate a response lost after server commit (server 200 dropped in transit). The
  client retries; the response is `DUPLICATE`; exactly one record exists; the outbox drains.
- **A-SYN-7** Device A and device B both record the same customer/date offline; on sync one is
  `APPLIED` and the other becomes a durable `issues` entry carrying the server's state. Assert it is
  **not** re-sent on the next sync cycle, and that a subsequent sync of unrelated operations
  succeeds while Needs Attention stays raised.
- **A-SYN-8** A sync operation that would fail validation online (negative quantity, unknown
  customer, other tenant's customer) is `REJECTED` with the same error code as the online route,
  and lands in `issues` rather than disappearing with its outbox entry.
- **A-SYN-12** Playwright: force one `REJECTED` and one `CONFLICT`, then reload and fully restart
  the browser context. Both issues are still present, Needs Attention is still raised, the outbox is
  empty, and neither operation was re-sent.
- **A-SYN-13** Accept an operation, advance the clock far beyond any plausible retention window, and
  replay the same `operation_id`: the response is `DUPLICATE` and exactly one row exists. A
  code/schema test asserts no scheduled deletion, archival, or TTL of `sync_operation` rows exists.
- **A-SYN-14** Replay an accepted `operation_id` with a different payload: the response is a
  `IDEMPOTENCY_KEY_REUSE` conflict (409). Assert the original effect is unchanged **and** the new
  request was not applied — the failure is closed in both directions.
- **A-SYN-15** Fire five concurrent identical envelopes that all target the same business slot
  (same customer and service date). Exactly one is `APPLIED`, four are `DUPLICATE`, and none
  surfaces as `CONFLICT` — the register serializes them, not the daily-record index.
- **A-SYN-16** Assert every snapshot-bearing table has a `row_version` column defaulting from the
  shared sequence, and that `billing_cycle` does not. Record a payment, record a later one, then
  void the first: each draws a distinct value, the later payment's exceeds the earlier one's, the
  void advances that payment's own version, and the compensating ledger entry takes a later value
  still. Issue statements in three successive cycles and assert their versions strictly increase.

---

## 4. Payment invariants (PAY)

V1 payments are **manual only** — recorded by the owner, with no online gateway, no provider
reference, and no externally verified payment state. These invariants replace the former
gateway-verification family, which is out of scope.

| ID | Invariant |
| --- | --- |
| PAY-1 | A `payment` row is the only payment-bearing entity, and the only thing that posts a `PAYMENT` ledger entry. No attempt, intent, or provider record exists in V1. |
| PAY-2 | `method` is one of `CASH`, `BANK_TRANSFER`, `OTHER`. No online or provider state is representable. |
| PAY-3 | `amount_minor > 0`, enforced by a database check, not only by application validation. |
| PAY-4 | Every payment is tenant-scoped and reaches its customer through the composite foreign key `(tenant_id, customer_id)`, so it can never attach to another tenant's customer. |
| PAY-5 | Duplicate protection rests entirely on `operation_id` (SYN-2). Replaying a recorded payment creates nothing and returns the same logical result. |
| PAY-6 | There is deliberately **no** amount/date natural-key deduplication: two genuine equal payments from the same customer on the same day are legal and must not be blocked. The UI warns on such a repeat; it never forbids it. |
| PAY-7 | Voiding a payment appends a compensating payment-origin `ADJUSTMENT` ledger entry carrying reason, actor, and timestamp. The payment row is never mutated beyond `RECORDED → VOIDED` and never deleted (AUD-1, AUD-2). |
| PAY-8 | Manual payment recording works offline and syncs under the ordinary outbox rules, with no privileged path and no relaxed validation (SYN-8). |
| PAY-9 | Payments cannot be created, amended, or voided by voice in V1 (VOI-7). |

### Acceptance — PAY

- **A-PAY-1** A schema test asserts no `payment_attempt`-like table, no `provider` column, and no
  provider-callback route exists anywhere in the application.
- **A-PAY-2** Recording each of `CASH`, `BANK_TRANSFER`, `OTHER` succeeds; any other method value is
  rejected by the database, not merely by the API layer.
- **A-PAY-3** `amount_minor` of `0` and of `-100` are both rejected at the database level (direct SQL
  insert, bypassing the application).
- **A-PAY-4** Tenant A recording a payment against tenant B's customer id receives 404, and a direct
  SQL insert of that row is refused by the composite foreign key.
- **A-PAY-5** Post the same payment `operation_id` five times concurrently: one payment, one ledger
  entry, four `DUPLICATE` responses carrying the same logical result.
- **A-PAY-6** Two payments of 500 for the same customer on the same day, with **different**
  `operation_id`s, both succeed and both post — outstanding drops by 1000. This test must fail if
  anyone later adds natural-key deduplication.
- **A-PAY-7** Void a 500 payment: outstanding returns to its pre-payment figure, a payment-origin
  `ADJUSTMENT` of +500 exists with reason/actor/timestamp, the original row survives as `VOIDED`,
  business generated is unchanged (A-FIN-14), and no delete route or ORM delete targets `payment`.
- **A-PAY-8** Record a payment offline, restart the browser, sync: exactly one payment exists and
  the same validation errors surface as online.

---

## 5. Reminder invariants (REM)

| ID | Invariant |
| --- | --- |
| REM-1 | The default schedule is day 1 statement, days 4 / 8 / 12 reminders, day 15 final reminder plus owner alert — stored as tenant configuration, not as constants. |
| REM-2 | Every reminder uses the current authoritative outstanding (FIN-4), recomputed at send time. It never reads an amount from a statement, a cache, or an earlier reminder. |
| REM-3 | A partial payment between generation and send lowers the amount actually sent. |
| REM-4 | A customer whose outstanding is ≤ 0 receives no further outstanding reminders in that cycle, including immediately after a mid-cycle payment. |
| REM-5 | `(tenant, customer, cycle, schedule_day)` is unique. Re-running the daily job, or a duplicated cron trigger, sends nothing twice. |
| REM-6 | A communication failure, retry, or total provider outage writes only to `communication_log`. It can never change a balance, statement, payment, reminder eligibility, or commission record. |
| REM-7 | All eligibility and amount decisions are made inside our application. The delivery provider receives a rendered value and never computes one. |
| REM-8 | After an outage, catch-up is by **stage**, never by replaying missed dates. Each run computes `due_stage` = the highest configured schedule day ≤ today's tenant-local day, and `sent_stage` = the highest stage already successfully sent for that customer and cycle. If `due_stage > sent_stage` and the stage's own eligibility rule is met (outstanding > 0 for `REMINDER` and `FINAL`; an issued statement for `STATEMENT`), **exactly one** reminder is sent — the `due_stage` one. Skipped intermediate stages are never replayed, so an outage can never cause a burst of messages. |

### Acceptance — REM

- **A-REM-2/3** Generate a day-4 reminder for 5000, record a 2000 payment, then send: the delivered
  amount is 3000.
- **A-REM-4** Pay in full on day 6; the day-8, day-12, and day-15 runs produce no reminder for that
  customer, and no owner alert.
- **A-REM-5** Run the daily job three times for the same business date: one reminder row, one
  message, `job_run` guard hit twice.
- **A-REM-6** With the communication provider hard-failing on every send, assert every balance,
  statement, payment, and commission row is bit-identical before and after the run, and that the
  reminder is marked `FAILED`.
- **A-REM-7** Inspect the outbound message: it contains a pre-rendered amount string and no
  instruction, formula, or raw balance for the provider to interpret.
- **A-REM-8a** Outage covering day 4 only; the job runs on day 5 with outstanding > 0. Exactly one
  message is sent, and it is the day-4 stage.
- **A-REM-8b** Outage covering days 4 through 8; the job runs on day 9 with outstanding > 0. Exactly
  one message is sent, and it is the day-8 stage — assert the day-4 stage was **not** also sent.
- **A-REM-8c** Same outage as A-REM-8b, but the customer paid in full on day 6: no catch-up reminder
  is sent at all, and no owner alert is raised.
- **A-REM-8d** Outage through day 15; the job runs on day 16 with outstanding > 0. The `FINAL` stage
  **and** the owner alert are sent, and no earlier stage is replayed.

---

## 6. Commission invariants (COM)

| ID | Invariant |
| --- | --- |
| COM-1 | No commission rate, basis, or currency is hard-coded. All come from a `commission_plan` row. |
| COM-2 | A `commission_event` is created only after its source business event has been accepted and committed centrally. An offline device never creates commission. |
| COM-3 | Every `commission_event` snapshots the basis, rate, and fixed amount in force at creation. A later plan change never alters an existing event. |
| COM-4 | A correction, void, or reversal of a commissionable source event produces exactly one `commission_adjustment`, computed with the **original snapshotted terms**, and traceable to both the source and the original event. |
| COM-5 | `(tenant_id, source_type, source_id)` is unique on both events and adjustments: one source fact yields at most one commission event and at most one adjustment. |
| COM-6 | `commission_settlement` is an independent, append-only record of money settled. It stamps nothing on earning rows, references no event, and deletes or rewrites nothing. `earned + adjustments − settled = outstanding` always holds, including across partial and repeated settlements. |
| COM-7 | Tenant principals have no read and no write access to commission plans, events, adjustments, or settlements. |
| COM-8 | Only a platform principal may create a plan, create an adjustment, or record a settlement. |
| COM-9 | Commission uses the same integer rounding rule as billing; `rate_bp` is an integer 0–10000. |
| COM-10 | Changing the plan basis changes only which future events are generated. Existing events, adjustments, and settlements are untouched. |
| COM-11 | Neither `commission_event` nor `commission_adjustment` carries a settlement reference. V1 settles in aggregate and does not allocate settlements to individual earning events; a settlement-allocation table is a later decision if per-event allocation is ever required, never a column retrofitted onto immutable earning history. |

### Acceptance — COM

- **A-COM-2** Record a service offline, then sync. Before sync, zero commission events exist. After
  the accepting transaction commits, exactly one exists.
- **A-COM-3** Generate an event at 250 bp, change the plan to 500 bp, re-read: the event still
  shows 250 bp and its original amount.
- **A-COM-4** Correct a 3-unit record to 2 units under a 250 bp plan after the plan moved to 500 bp:
  the adjustment is computed at 250 bp and links to both the correction and the original event.
- **A-COM-6** *(partial settlement — the exact case the old model could not represent)* Earn 1000
  across events. Settle 400 → outstanding 600. Settle 600 → outstanding 0. At every step assert that
  no earning event or adjustment was modified, deleted, or annotated; that each earning row is
  unchanged in every field from its creation; and that `earned + adjustments − settled ==
  outstanding` holds throughout. A schema test asserts no `settlement_id` column exists on either
  earning table.
- **A-COM-6b** Settlement remains additive at the edges: settling 1200 against 1000 earned yields
  outstanding −200 without error, and a later adjustment still applies cleanly on top.
- **A-COM-7/8** An owner-admin token receives 403 or 404 on every commission route, including read.
  A route-enumeration test asserts no commission field is reachable through any tenant endpoint,
  including dashboards and search.
- **A-COM-10** Switch the basis from `RECORDED_VALUE` to `COLLECTED_VALUE`; assert prior events are
  unchanged and only subsequent triggers differ.

---

## 7. Audit and history invariants (AUD)

| ID | Invariant |
| --- | --- |
| AUD-1 | No hard-delete path exists for any accepted financially meaningful record: daily service records, payments, ledger entries, statements, commission events, adjustments, or settlements. |
| AUD-2 | The only permitted mutation of an accepted source document is its single lifecycle transition (`ACTIVE → SUPERSEDED` or `ACTIVE → VOIDED`; `RECORDED → VOIDED`), performed in the same transaction as its replacement or compensation. |
| AUD-3 | Every correction, void, and reversal preserves the original value, the resulting adjustment, the reason, the actor, the timestamp, and the source reference. |
| AUD-4 | A correction links to what it replaces (`corrects_id` / `superseded_by_id`), so the full chain is reconstructable from any point. |
| AUD-5 | `adjustment_minor` on a correcting record equals `new charge − superseded charge`. |
| AUD-6 | A reason is mandatory on every correction, void, and reversal. |
| AUD-7 | `audit_event` and `ledger_entry` are append-only. |
| AUD-8 | A customer's history displays superseded and voided records alongside current ones — they remain visible, not hidden. |
| AUD-9 | Every audit and financial row records whether it originated `ONLINE`, via `SYNC`, from a `JOB`, or from the `PLATFORM` scope. |

### Acceptance — AUD

- **A-AUD-1** A schema/code test asserts no `DELETE` statement and no ORM `delete()` targets any of
  the protected tables; the API exposes no delete route for them.
- **A-AUD-3/5** Correct 3 units at Rs. 250 (Rs. 750) down to 2 units: the original row survives as
  `SUPERSEDED` at Rs. 750, the new row shows Rs. 500 with `adjustment_minor = −25000`, a ledger
  `ADJUSTMENT` of −25000 exists, and reason/actor/timestamp are all populated.
- **A-AUD-4** Chain three successive corrections and assert the full chain is walkable in both
  directions and that exactly one row is `ACTIVE`.
- **A-AUD-6** A correction request with no reason is rejected with a validation error.
- **A-AUD-8** The customer history endpoint returns superseded and voided rows with their status,
  and the sum of only `ACTIVE` rows reconciles with outstanding.

---

## 8. Voice invariants (VOI)

Voice is an input method. It creates no new authority, no new domain path, and no new financial
rule.

| ID | Invariant |
| --- | --- |
| VOI-1 | A confirmed voice operation is executed by the **same** validated service command as button input, through the same domain module. There is no separate voice accounting path. |
| VOI-2 | No speech or model output ever writes. `/voice/*` routes are read-only; interpretation yields a *candidate* intent, and only an explicit human CONFIRM triggers the ordinary domain command. |
| VOI-3 | The operational interpreter's output is a closed union — `RECORD_SERVICE`, `SKIP_SERVICE`, `UNRESOLVED` — with fixed fields. SQL, table or column names, arbitrary code, a free-form action string, and any payment, price, commission, settlement, correction, void, or configuration command are **not representable** in the schema. |
| VOI-4 | Ambiguous customer resolution never auto-selects. Resolution is deterministic server-side matching against the tenant's own customers; the interpreter is never given the customer list. |
| VOI-5 | A missing or unparseable quantity is never invented. Nor is a materially ambiguous date. |
| VOI-6 | An utterance conflicting with authoritative tenant configuration (for example a spoken unit that is not the configured `unit_label`) fails closed into clarification. Configuration is never silently overridden. |
| VOI-7 | Voice may perform exactly: search/query, `RECORD_SERVICE`, and `SKIP_SERVICE` for today. Payments, prices, commission, settlements, corrections, voids, tenant configuration, and user management are unreachable by voice. |
| VOI-8 | Accepted records carry `input_method = VOICE`. Provenance is metadata only: after acceptance a voice record behaves identically to a button record in validation, pricing, uniqueness, history, reporting, and commission. |
| VOI-9 | Raw audio is never persisted by our application — no recording archive, and no audio in audit history. Transcripts are ephemeral, held only for the life of the confirmation, and never written to a table. |
| VOI-10 | Voice weakens no FIN, SEC, SYN, or AUD invariant: same tenant scoping, same `operation_id` idempotency, same active-record uniqueness, same immutable history, same offline conflict behaviour. |
| VOI-11 | Voice is optional and degrades cleanly. With speech or AI unavailable, disabled, or failing, button entry and text search remain fully functional, and offline button recording is unaffected. |
| VOI-12 | Speech-to-text and both interpreters sit behind ports. The initial speech implementation is the Groq adapter running `whisper-large-v3`, but the model identifier and all vendor API details live only in the adapter and configuration — no domain or service module references them. No vendor identifier appears outside `app/adapters/`, and mocks run the full test suite with no network access. |
| VOI-13 | The money shown on a confirmation card is computed server-side by the ordinary pricing rule (FIN-3). The model never supplies, sees, or influences a price or an amount. |

### Acceptance — VOI

*Voice search*

- **A-VOI-1** A known transcript produces exactly the same deterministic result as typing the same
  text: same filter object, same SQL, same rows.
- **A-VOI-2** An interpreter returning unknown fields, unknown operators, an inflated `limit`, or an
  SQL fragment is rejected by validation before any query is built.
- **A-VOI-3** With the AI unavailable, ordinary structured search and filtering still work.

*Voice service entry*

- **A-VOI-4** "Essa bought 2 bottles" yields a candidate intent, **not** a database write.
- **A-VOI-5** After interpretation and before confirmation, no `daily_service_record`, ledger entry,
  audit event, or commission event exists.
- **A-VOI-6** Confirming produces exactly the same record, ledger entry, audit event, and commission
  event as the equivalent button entry — asserted field by field, `input_method` aside.
- **A-VOI-7** With two active customers plausibly matching the spoken name, no customer is selected;
  a candidate list is returned instead.
- **A-VOI-8** "Essa bought some bottles" (no quantity) never invents one; the response asks for
  quantity or falls back to the normal control.
- **A-VOI-9** "Change Essa's price to 900" is `UNRESOLVED` or rejected — never a price change. A
  schema test asserts no price, payment, commission, or configuration intent can even be constructed.
- **A-VOI-10** "Essa paid 500 rupees" creates no payment; payment-by-voice is unavailable in V1.
- **A-VOI-11** Confirming, losing the response, and retrying replays the same `operation_id`: one
  record, `DUPLICATE` on the retry.
- **A-VOI-12** An accepted voice record has `input_method = VOICE`, and an otherwise identical
  button record differs in that column and nothing else.
- **A-VOI-13** No audio is written anywhere: a filesystem, object-store, and schema assertion after a
  full voice flow finds no audio artifact and no transcript row.
- **A-VOI-14** With `VOICE_ENABLED=false`, the speech provider failing, and the interpreter failing,
  button entry and the daily register work normally — including offline.
- **A-VOI-15** The whole voice suite runs with `MockSpeechToTextProvider` and a mock interpreter,
  with no network access available. No automated test ever makes a live provider call.

*Speech provider selection (the frozen initial implementation)*

- **A-VOI-16** With `SPEECH_PROVIDER=groq`, the resolved provider is the Groq adapter and the
  default `SPEECH_MODEL` is `whisper-large-v3` — asserted from configuration resolution, with the
  network stubbed; no live call is made.
- **A-VOI-17** Changing `SPEECH_MODEL`, or swapping `SPEECH_PROVIDER` back to `mock`, requires no
  change to any domain or service module. A grep asserts no model string and no vendor API detail
  appears outside `app/adapters/speech/` and configuration, and the import-linter test (A-SLOT-5)
  still passes.
- **A-VOI-18** A transcription failure, timeout, or low-confidence result from the configured
  provider degrades to the ordinary controls — no candidate intent, no partial record, no error
  that blocks button entry.
- **A-VOI-19** Running the full voice flow against the Groq adapter with a stubbed transport
  introduces no audio or transcript persistence: the A-VOI-13 assertions still hold with a real
  adapter configured.

---

## 9. Cross-cutting acceptance for the pluggable integration points

Three external areas remain genuinely pluggable: **GHL/WhatsApp delivery**, **speech-to-text**, and
**the AI interpreters**. These prove they stayed that way, and must be re-run whenever a real
provider lands. (Commission terms are business configuration, not a runtime integration.)

- **A-SLOT-1** The entire backend test suite passes with `COMMS_PROVIDER=mock`,
  `SPEECH_PROVIDER=mock`, and `GROQ_ENABLED=false`, with no network access available. Selecting Groq
  as the deployment speech provider changes nothing about this: tests never call it.
- **A-SLOT-2** A second mock communication provider is registered; no file outside
  `app/adapters/comms/` changes.
- **A-SLOT-3** A second mock speech-to-text provider, with different capabilities and a failure
  mode, is registered; no file outside `app/adapters/speech/` changes. This must keep passing now
  that a real provider is selected — choosing Groq froze an *implementation*, not the port.
- **A-SLOT-4** With the AI disabled or failing, structured search, filtering, dashboards, reminders,
  billing, payments, and button-based daily entry all behave identically, and voice degrades to the
  ordinary controls.
- **A-SLOT-5** An import-linter test fails the build if any domain module imports from
  `app/adapters/`.
- **A-SLOT-6** A vendor-name grep (`ghl`, `groq`, and any selected STT vendor) outside
  `app/adapters/` and configuration returns nothing; this runs in CI.

---

## 10. Definition of done for any implementation package

A package is complete only when:

1. Every invariant it touches has at least one test that **fails** if the invariant is removed.
2. The tenant isolation suite (A-SEC-3/4) passes over all newly added routes.
3. Migrations apply cleanly forward, and every constraint named in the architecture freeze exists in
   the database — verified by a schema assertion test, not by reading the migration file.
4. No new dependency, table, or background service was added without a stated requirement.
5. No secret entered source control.
