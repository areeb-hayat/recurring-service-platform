# P6 — Handover

Package: **P6 — Owner Financial Dashboard & Operating Costs**. The owner can now see the
business's money and record what it costs to run. Nothing committed, nothing pushed.

**Base commit:** `4e6e4f8` — "Implement P5 offline-first sync and durable conflict handling".
**Worktree:** `E:\Recurring-Service-Platform-yahya` · **branch:** `yahya` · **upstream:**
`origin/yahya`. Working tree was clean at start.

---

## 1. Scope implemented

Two halves that share nothing but a navigation bar.

**The owner's own money** — screens over reads P2 and P3 already computed but nothing rendered:

```
/overview          dashboard: outstanding, sold, collected, who owes, recent payments
/statements        issued statements, list and detail
/customers/:id     + payments, statements, delivery history, reverse a payment
/customers/:id/pay record a manual payment (online only)
```

**The owner's costs** — a post-P0 product addition (P0 §15a, dated 2026-09-03):

```
/operating-costs   cost items, versioned rates, monthly usage, real invoices,
                   estimated vs actual variance, month-by-month history,
                   and a three-level planning calculator
```

Plus one sync change: `payment` and `statement` joined the read-only change feed, which P5
deliberately deferred until a screen existed for them.

**Deliberately not built:** reminders, WhatsApp/n8n/Evolution/SMS (P10), search or aliases, voice
or any speech/intent provider call (P9), a platform-owner frontend, deployment, domain
provisioning, live vendor billing APIs, automatic invoice ingestion. No placeholder module exists
for any of them, and no outbound HTTP client is imported anywhere in `app/`.

---

## 2. The three money systems, and why they stay apart

This is the load-bearing decision of the package.

| Concept | Tables | Scope | Answers |
| --- | --- | --- | --- |
| Customer ledger | `ledger_entry`, `statement`, `payment` | tenant | what a customer owes the business |
| Platform commission | `commission_*` | **platform** | what the business owes the platform |
| **Operating costs** | `operating_cost_*` | tenant | what the business owes its providers |

They are shown near each other for the owner's convenience — the dashboard and Running costs are
two taps apart — and they are otherwise disjoint: separate tables, separate API prefixes, separate
capabilities, separate totals. `app/costs/` imports neither `app.billing.ledger` nor
`app.commission.*`, and a test asserts it by walking the imports rather than trusting the
docstring. Recording a month of provider usage creates no ledger entry, no commission event, and
moves no customer's outstanding balance by a single minor unit; a test asserts that too.

The capability question mattered more than it looks. Reusing `commission:*` would have handed the
platform's authority to the tenant, and reusing `billing:read` would have put provider expenses
behind the same key as customer statements. `cost:read` / `cost:write` were added to `OWNER_ADMIN`
instead — the frozen §3.2 map grows by two, its disjointness from the platform set is unchanged,
and A-SEC-5 still passes untouched.

---

## 3. Owner dashboard

`GET /api/v1/dashboard/summary` and `GET /api/v1/dashboard/outstanding` — both in the P0 §15
frozen surface, neither previously implemented. `app/billing/dashboard.py`.

Every figure is derived server-side by the **same functions** statements and the customer page use
(`app/billing/reporting.py`), so the dashboard cannot disagree with the rest of the system:

- **outstanding** — `SUM(ledger_entry.amount_minor)` over the tenant (FIN-4);
- **business generated / billed value / collected** — the four §11.1 derivations, for the open
  cycle *and* for all time, never one derived from another;
- **customer counts** — total, active, with a balance due, in credit. The last two come from one
  grouped query over the ledger, not one balance read per customer;
- **recent payments** — the latest activity, **voided rows included**, because a reversal is
  exactly the movement an owner opens a dashboard to find (AUD-8).

`current_cycle` is `null` rather than a row of zeros when no cycle is open: zeros would read as "no
business this month", which is a different statement.

`GET /dashboard/outstanding` returns who owes, most owed first, **including credits** (a negative
balance from an overpayment). Filtering credits out would make the page's total disagree with the
summary's — the kind of small inconsistency that destroys trust in a financial screen.

**The client renders and does not reconcile.** A test asserts the payment-void case through the
dashboard: business generated stays put while collections and outstanding move (A-FIN-14/16).

**Offline.** The summary is a server-computed document, so the client stores it in the P5 snapshot
when the screen is opened online (P0 §7.1 lists "dashboard" among the snapshot's authoritative
reads) and, offline, renders it with *"Offline — showing the figures from 20 minutes ago"*. It is
never recomputed locally to freshen it. A device that has never received one says "Unavailable
offline" and makes no request.

It is refreshed **when the dashboard is opened**, not on every sync. A round of two hundred taps
triggers a sync after each one, and the summary is several aggregate queries.

---

## 4. Statements

`GET /api/v1/statements` (new, tenant-wide) alongside the existing `/statements/{id}` and
`/customers/{id}/statements`. Read on screen from the P5 snapshot, so a synced statement is
readable offline.

The detail shows the FIN-8 identity line by line, in the order a person reads a bill:

```
brought forward + this period's deliveries + corrections − money received + payments reversed
= balance at close
```

All six numbers are printed as the server sent them, already split by origin — a service
correction and a payment reversal are different lines even though both are `ADJUSTMENT` rows in
the ledger. The screen's only arithmetic-looking act is *showing* the identity.

**No edit, no delete, no reissue, no recalculate**, and no "issue statement" button — a statement is
only sound once its cycle can receive no further entries, so issuing remains part of closing a
cycle, and P0 §15 exposes no route that would let it happen sooner. **No automatic month close was
invented.** Tests assert the absence of every one of those controls, which is the only way to test
for something that must not exist.

Ordering note: `list_all_statements` pages by `(period_start DESC, row_version DESC)`. A close
issues one statement per customer inside a single period, so period alone is not a total order and
offset paging over it could drop or repeat a row at a page boundary — the same defect the customer
list fixed with an id tiebreaker in P4.

---

## 5. Manual payments

Record and reverse, on the existing P2 contract. `CASH`, `BANK_TRANSFER`, `OTHER` — the complete
V1 vocabulary, and no provider state is representable.

- **Any positive amount**, including more than is owed. The form warns *"That is more than the
  balance. It will be kept as credit"* and does not stop you (FIN-10, PAY-6: the UI warns, it never
  forbids). Partial, full and over-payment are all tested.
- **The amount is parsed, not multiplied.** `majorToMinor` concatenates digits and pads the
  fraction; `250.50 → 25050` exactly, where `Number("250.50") * 100` is not exact for every input.
  Moved to `lib/money.ts` in this package because the payment form is now a second caller.
- **`operation_id` generated once**, at the tap. A transport failure keeps the envelope and locks
  the amount field: editing it and pressing Retry would send a different request under the same id,
  which SYN-14 correctly refuses. A test asserts the two request bodies carry the same id.
- **The resulting balance is not computed here.** A test asserts the screen does *not* show
  `700 − 250 = 450` after a successful payment: it shows the server's figure until the server
  re-states it.
- **Reversal requires a reason** (AUD-6). The button is disabled until one is typed. The payment row
  survives as `VOIDED` carrying reason, actor and timestamp; a compensating payment-origin
  `ADJUSTMENT` is what returns the balance. There is no delete control anywhere, and a payment
  already reversed offers no Reverse button.
- **Voided payments are shown, not hidden** (AUD-8) — struck through, with the reason. Hiding them
  would leave a balance nothing on screen accounts for.

**Online only, and it says so** (PAY-8). Offline the form is disabled with a plain sentence rather
than a silently failing button, and a test asserts zero requests leave the app.

New reads: `GET /customers/{id}/payments` and `GET /customers/{id}/history` (the latter is in the
frozen P0 §15 surface and closes A-AUD-8, which had no implementation). Both return superseded and
voided rows.

---

## 6. Operating costs

### 6.1 Model — four tables

```
operating_cost_item     a provider / cost line, configured by the owner
operating_cost_rate     a versioned price with an effective range
operating_cost_usage    a month's measured usage + the estimate it produced
operating_cost_actual   what the provider actually invoiced for that month
```

**`operating_cost_item` is a table, not an enum.** The current list — hosting and database,
speech-to-text, intent interpretation, backup storage, messaging automation hosting, messaging
charges, domain — is what the business happens to pay for today. Freezing it into application logic
would mean a code change to record a new supplier.

**Rates are versioned and never rewritten.** Exactly one of two shapes, enforced by a CHECK:

- usage priced — `unit_price_minor` + a `unit` label (per audio hour, per GB-month, per million
  tokens);
- fixed — `fixed_amount_minor` + `MONTHLY` or `ANNUAL`.

Ranges may not overlap for one item — a GiST `EXCLUDE` constraint over
`(tenant_id, cost_item_id, daterange(effective_from, effective_to, '[]'))`, the same mechanism and
the same reasoning as `commission_plan`: those terms get snapshotted onto usage rows, where an
ambiguity could never be corrected afterwards. A new rate **closes** its open-ended predecessor at
`effective_from − 1 day`; there is no rate-edit route and requests carry no `effective_to`, so no
caller can leave a gap with no rate in force.

**Which rate applies to a month:** the one in force on the month's **first day**. Deterministic, and
a rate introduced part-way through a month cannot silently restate a month already reviewed.

**Corrections, not edits.** A usage figure or an invoice that turns out to be wrong is replaced by
appending a new `ACTIVE` row and marking the old one `SUPERSEDED` with a mandatory reason, actor and
timestamp — the same shape a corrected daily service record has. Partial unique indexes keep exactly
one `ACTIVE` row per (item, month). No delete path exists (AUD-1).

**No `row_version` on any of them.** The Operating Costs screen is online-only, so versioning them
"for symmetry" would quietly make them syncable. A test asserts the columns are absent.

### 6.2 Estimate, actual, variance

```
usage priced   estimate = round_half_up(usage_quantity × unit_price_minor)
fixed monthly  estimate = fixed_amount_minor
fixed annual   estimate = round_half_up(fixed_amount_minor / 12)
variance       = actual − estimated
```

The owner's monthly formula — hosting + STT + intent + backup + messaging hosting + messaging +
(annual domain / 12) + other approved costs — is exactly the sum of those over the configured items,
expressed without naming any of them. The annual/12 term is normalised once, on the server, so no
screen divides money.

Rounding goes through `app.core.money.round_half_up`, the *same single implementation* the charge
rule and the commission rule use. `multiply_minor` was added beside `compute_charge_minor` because
usage quantities are not `NUMERIC(12,3)` service quantities — audio hours and GB-months are measured
at six decimal places — and relaxing `quantize_quantity` to accommodate them would have weakened a
rule that exists to keep *customer billing* exact. FIN-3's "one rounding point" is untouched:
nothing here posts to the customer ledger.

**Nothing is invented** (P6 §14):

- no measured usage on a usage-priced item → **no estimate**, not zero. Zero says the provider was
  free.
- no invoice entered → **no actual**, and therefore **no variance**. An invoice that has not arrived
  is not an invoice for nothing.

Both render as `—` on screen, and tests assert the dash rather than a `0.00`.

**Currency travels with the money.** Rates and invoices each carry their own `currency` and
`currency_exponent`. Providers bill in USD while the tenant bills in PKR, and V1 has no FX source —
so totals are reported **per currency** and nothing is converted. The screen says so when more than
one appears. No FX feature was added.

### 6.3 Scenario calculator

`POST /api/v1/operating-costs/scenarios` — a read that writes nothing and therefore carries no
`operation_id`. Three defaults (100 / 500 / 1,000 uses a day), with the uses-per-day and the average
seconds both editable.

Each case supplies either a usage quantity directly or the events/seconds/days triple, and the
**server** does the conversion — `events_per_day × days × seconds_per_event / 3600` — then applies
the configured rate. The client would have to divide to produce hours, and dividing on the client is
how a planning figure quietly stops matching the recorded one.

Against a rate row of 22 minor units per audio hour, that reproduces the planning document exactly:

| Uses/day at 5s | Audio hours | Estimate |
| --- | --- | --- |
| 100 | 4.166667 | 0.92 |
| 500 | 20.833333 | 4.58 |
| 1,000 | 41.666667 | 9.17 |

A test asserts those three numbers, and another asserts that changing the *rate row* changes the
answer — which is the whole point: the price is data, not a constant. **No vendor name and no vendor
price appears anywhere in `app/`**; the existing A-SLOT-6 guard covers `app/costs/` automatically
because `costs` was added to `DOMAIN_PACKAGES`.

### 6.4 Screen

`/operating-costs`: current-month summary (estimated / invoiced / difference, per currency), a line
per provider with its rate and measured usage, forms for usage, invoices, rates and new items, the
scenario calculator, and twelve months of history. Plain language throughout — "Running costs",
"Invoiced", "Difference", "What if we used more?" — and no accounting vocabulary.

Online only. Offline it says so and issues no request.

---

## 7. Sync feed: payment and statement

`SYNC_ENTITIES` is now `tenant, customer, daily_service_record, payment, statement`.
**`SYNC_FEED_VERSION` 1 → 2.**

P5 withheld these two on the grounds that streaming financial rows to a device with nothing to
render them invites a client-side total (SYN-9). P6 built that screen, and every figure on it is one
the server computed.

**`ledger_entry` is still absent, and not by oversight.** Nothing renders a raw ledger row: a
statement *is* the presentation of a cycle's entries, and a balance is derived server-side. Shipping
the entries would put the one dataset a client could plausibly re-total onto the device for no
screen at all.

**The version bump is what makes admission safe.** The feed only ever hands over rows *above* the
cursor, so a device already past a payment's `row_version` could never receive it. A different
`feed_version` tells the client to discard its cursor and resynchronise from zero. That resync
clears the **snapshot only** — `outbox` and `issues` are not caches and are untouched (the existing
P5 test that asserts this now runs across the version change).

**The commit-order boundary was extended, which is the part that would have been easy to miss.**
`FEED_WRITING_OP_TYPES` gained `payment.record`, `payment.void` and `billing.close_cycle`. Audited
against the real paths: `record_payment` draws a `row_version` on insert, `void_payment` advances it
on the `RECORDED → VOIDED` transition, and `issue_statements_for_cycle` draws one per statement
inside the close transaction — those are the only `next_row_version` calls for the two tables.
Without them the D4 gap reopens on financial history: a payment allocating early and committing late
behind a service record that commits first is the same race P5 closed.

A test asserts the lock is genuinely held during a real `payment.record` by querying `pg_locks`
inside `perform` — the registry is a list, this is the behaviour, and it fails if the lock is
removed.

The P5 guard that pinned the old correspondence was rewritten rather than deleted: it now drives off
an explicit `ENTITY_WRITERS` map (entity → writing module → op types), checks each named module
really contains the allocation, and fails if `SYNC_ENTITIES` and `FEED_WRITING_OP_TYPES` drift apart
in either direction.

**Client side.** The first sync seeds `payment` and `statement` from `GET /payments` and
`GET /statements`, paged to the end like customers — a silent truncation would be history that
simply never appears offline. They are **not** pruned: the service-record retention rule (the
server-stated business date) is about a rolling day view, and trimming financial history by age
would invent the retention horizon D6 removed in P5.

**Payments stay online-only.** A test asserts `payment.record` is still `REJECTED` by
`POST /sync/operations` and leaves no payment, ledger entry, commission row or register entry.
Payment history becoming *visible* offline is not payment recording becoming *possible* offline.

---

## 8. Authorization and audit

- Two new tenant capabilities, `cost:read` / `cost:write`, on `OWNER_ADMIN` only. Disjoint from
  `commission:*` and from the platform set; asserted directly.
- Every P6 route is tenant-scoped from the authenticated principal. No route reads a `tenant_id`
  from a body or query, and the client never sends one.
- All fourteen new routes are in the A-SEC-3/4 `EXERCISED` inventory, so the enumeration guard
  covers them and a future route cannot escape it.
- Cross-tenant identifiers return **404**, never 403 (SEC-4): asserted for customer payments,
  customer history, cost rates, usage, invoices and scenarios. Aggregate routes name nothing, so
  those are asserted the only way that means anything — tenant B's dashboard, outstanding list,
  payment list, statement list and cost summary contain none of tenant A's business.
- A platform principal is refused **403** on every P6 route (SEC-6).
- Audited with before/after and actor: cost item created, rate created, rate closed, usage recorded,
  usage corrected, invoice recorded, invoice corrected. Corrections carry the reason. New allow-list
  entries were added for the four entities — an allow-list, so a field is only audited if somebody
  chose to audit it.

---

## 9. Migration

One migration, `0004_p6_operating_costs`, adding exactly the four `operating_cost_*` tables and
nothing else. No column is added to `ledger_entry`, `payment`, `statement` or any `commission_*`
table — P6's other half is read-only over what P1–P3 already built.

Constraints, all explicitly named so the schema-assertion test can look them up: composite FKs
`(tenant_id, cost_item_id)` and `(tenant_id, rate_id)`; the rate EXCLUDE constraint; CHECKs for the
month-start key, one pricing shape, complete usage/fixed rates, non-negative amounts, valid statuses
and currency exponents, and *superseded-requires-a-reason-and-a-successor*; and two partial unique
indexes for the single `ACTIVE` row per (item, month).

`btree_gist` is required by the EXCLUDE constraint and is already installed by `0003`; the
`IF NOT EXISTS` keeps this migration runnable on its own. The downgrade drops the four tables and
leaves the extension, which `0003` still needs.

**Verified:** `upgrade head` → 19 tables with all four present; `downgrade 0003` → 15 tables with all
four gone; `upgrade head` again → identical to the first upgrade; and the exclusion constraint and
both partial unique indexes present by name.

---

## 10. Verification

Run once each, after the last code change.

| Check | Result |
| --- | --- |
| Frontend tests (`npx vitest run`) | **121 passed**, 0 failed, 9 files (90 from P1–P5, 31 new) |
| TypeScript (`tsc --noEmit`) | **clean** — `strict`, `noUncheckedIndexedAccess`, `verbatimModuleSyntax` |
| Production build (`npm run build`) | **succeeded** — `index.js` 294.74 kB (90.32 kB gzip), `index.css` 9.84 kB; PWA precache 12 entries / 306.92 KiB |
| Backend suite (PostgreSQL 16) | **842 passed**, 0 failed, 7m01s — 760 from P1–P5 unchanged, plus 82 new |
| Migration upgrade / downgrade / upgrade | **verified**, §9 |
| `git diff --check` | clean |

Playwright was **not** re-run. P6 introduces no browser-level guarantee that unit and integration
tests cannot prove honestly: the payment and cost writes are online-only, so there is no durability
claim to test across a browser restart, and the offline reads are covered against a real IndexedDB
under Vitest. Re-running P5's five cases would have been ceremony.

### 10.1 Backend tests added

| File | Tests | Covers |
| --- | --- | --- |
| `test_operating_costs.py` | 38 | versioned rates (successor closes predecessor, overlap refused by app **and** by direct SQL, two items may share dates, the month resolves on its first day); **a later rate never restates a recorded month**; one pricing shape enforced; provider currency carried not converted; **the owner's three worked examples reproduced from a rate row**; annual/12; fixed items take no usage; usage must be exact and never a float; no usage → no estimate; no rate → refused; future months refused; **variance = actual − estimated**; no invoice → no actual and no variance; a zero invoice is recordable and is not the same as none; corrections supersede with a mandatory reason, on both usage and invoices; no hard-delete path; month history and per-currency totals; the scenario calculator (three levels, rate-driven, writes nothing, unknown item 404); **no ledger entry, no commission row, no movement of any customer's outstanding**; the module imports neither ledger nor commission; no `row_version`; audit coverage and before/after/reason; idempotent replay over HTTP |
| `test_dashboard.py` | 12 | empty business reports zeros and a null cycle; headline figures come from the ledger; **a voided payment moves collections and outstanding, not business generated**; customer counts split active / owing / in credit; recent activity shows voids; no commission and no operating-cost figure in the summary; the outstanding list is ordered, includes credits, and agrees with the summary total; both routes over HTTP; customer payments / history / tenant-wide payment list |
| `test_p6_sync_feed.py` | 12 | a payment arrives as a change; **a void arrives as an update, not a deletion**; `payment.record` is still refused by the sync route and leaves nothing; statements arrive when a cycle closes, split by origin; feed version 2 and the entity list; `ledger_entry` still absent; no commission row reachable and none carries `row_version`; `head` accounts for the new tables; the cursor walks every row exactly once; replay is a superset; **the new op types take the SYN-10 boundary, asserted against `pg_locks` inside a real `payment.record`** |
| `test_tenant_isolation.py` (+13) | 13 | cross-tenant 404 on customer payments and history; cost rate / usage / invoice / scenario against another tenant's item; tenant-scoped payment list, statement list, dashboard, outstanding list and cost reads; platform principal 403 on every P6 route; cost capabilities disjoint from commission; all fourteen routes added to `EXERCISED` |

The 82 new backend tests are 38 + 12 + 12 + 13 above, plus **4** from the parameterized SEC-3
scoping guard (which now covers `app/billing/dashboard.py` and the three `app/costs/` modules
automatically), **2** net in `test_sync_changes.py` and **1** in `test_sync_serialization.py`.

`test_sync_changes.py` and `test_sync_serialization.py` had four guards **rewritten** rather than
deleted — they pinned P5's feed scope by construction, which is exactly what they were for. They now
pin P6's, and still fail if an entity is added without its op types or vice versa.

### 10.2 Frontend tests added

| File | Tests | Covers |
| --- | --- | --- |
| `dashboard/dashboard.test.tsx` | 18 | the dashboard renders the server's figures and derives none; **the payment-void case as the owner sees it**; unavailable offline with no request made; the cached summary shown with an "as of" stamp offline; no commission or cost figure; the statement list and detail from the snapshot; **no edit / delete / recalculate / reissue control on an issued statement, and no invented close-period or issue button**; statements unavailable offline when nothing synced; payment recording — partial, full, all three methods, overpayment allowed with a warning, minor units on the wire, no `tenant_id`, **the balance is never computed locally**; **offline blocks the form and sends nothing**; a transport failure retries the same `operation_id` with the amount locked; voided payments shown not hidden with no delete; reversal requires a reason; synchronised payments and statements readable offline |
| `costs/costs.test.tsx` | 13 | estimated / invoiced / difference from the server; the usage and rate behind an estimate; **a dash, never a zero**, for a missing invoice and for missing usage; per-currency totals with "nothing is converted"; month-by-month history; no mention of commission; unavailable offline with no request; usage sent as a decimal string; a correction demands a reason before the button enables; a new rate saved as data with no `effective_to`; **the three scenarios priced on the server, writing nothing**; the levels and seconds are editable |

Three existing tests needed updating, all for real reasons: the feed-version fixture moved to 2 (the
assertion is the *transition*, not the value), and `completeness.test.tsx` now stubs the two seed
reads P6 added.

---

## 11. Defects and clarifications found

**D1 — the supersede path violated its own CHECK constraint (found by the constraint, fixed).**
`record_usage` and `record_actual` originally marked the outgoing row `SUPERSEDED` and set
`superseded_by_id` in two statements, mirroring how `correct_service` does it. But the P6 tables
carry a CHECK that a `SUPERSEDED` row has both a reason *and* a successor, and it is evaluated on
each `UPDATE` — so the first statement wrote a momentarily illegal row and PostgreSQL refused it.
The constraint was right and the write order was wrong.

The fix draws the successor's id up front (`new_id()`), so the outgoing row is closed in **one**
statement carrying status, reason and successor together. That ordering is load-bearing for a second
reason as well: the partial unique index allows only one `ACTIVE` row per (item, month), so the slot
has to be freed *before* the replacement is inserted — the two-phase version could not have worked
even without the CHECK.

**D2 — a lost race for a month's ACTIVE row surfaced as a 500.** Two different `operation_id`s
recording the same month at the same instant are two different requests, so idempotency does not
merge them and the partial unique index decides. The loser was getting an unhandled `IntegrityError`.
It now gets `COST_PERIOD_CONFLICT` and a sentence telling it to reload — the same shape every other
constraint race in the codebase has.

**D3 — P3's schema test pinned the migration head, so any new migration failed it (found by the
final suite, fixed).** `test_schema_p3.py` asserted `alembic_version == "0003_p3_commission_engine"`,
which P6's `0004` legitimately breaks. P2's equivalent test had already got this right and says why
in its own docstring — *"deliberately not 'the head is P2': later packages move the head, and a test
that pins it would fail every time one legitimately does"* — and P3's had regressed to pinning.

The fix brings P3's into line with P2's: assert the applied chain still runs **through** P3, by
walking the revision ancestry. That keeps the property worth protecting — a future migration cannot
quietly drop P3 and leave the commission tables unexplained — without failing on every subsequent
package. This is a defect in a P3 test, not in P3 or P6 behaviour; no schema and no migration
changed to accommodate it.

*(Three `test_billing_cycles.py` errors also appeared in an earlier full run —
`relation "app_user" does not exist` inside the truncate fixture. They were leftover damage from two
pytest processes that had been killed mid-`DROP SCHEMA`, not a defect: the same tests pass cleanly on
a re-run, and the final suite is green.)*

**C1 — `majorToMinor` / `minorToMajor` lived in `CustomerForm`.** The payment form parses a typed
amount too, and "what a person typed, as minor units" wants one definition rather than two that
agree today. Both moved to `lib/money.ts` and are re-exported from `CustomerForm`, so no caller
changed. Nothing about the parsing changed: it is still string concatenation, never
`Number(text) * 100`.

**C2 — the P0 §15 surface was missing two routes it froze.** `GET /customers/{id}/history` is named
in §15 and is what A-AUD-8 asserts against, and neither existed. Both now do. The tenant-wide
`GET /statements` and `GET /payments` are genuinely new: §15 froze the per-customer and
by-id forms only, and the owner's statement list and the first-sync seed both need the tenant-wide
one. Recorded here as an addition to the surface rather than slipped in.

**C3 — operating costs are a post-P0 addition, and are documented as one.** A dated §15a was added
to `docs/P0_ARCHITECTURE_FREEZE.md` and a `COST-1..11` block with acceptance criteria to
`docs/P0_INVARIANTS_AND_ACCEPTANCE.md`. Nothing else in either document was touched, and **no
client-facing document was modified**.

**C4 — no defect was found in P1–P5 behaviour.** Every route P6 consumes behaved as its handover
describes.

---

## 12. Risks

**R1 — the feed-version bump costs every existing device a full resync.** By design, and the reason
P5's R1 asked P6 to add these entities *before* the data grows. For a tenant with a year of payments
it is a real download, done once. The seed pages to the end rather than truncating, which is the
right trade but is the slow half.

**R2 — payments and statements are never pruned from the device.** Deliberate: they are the history
the new screens render, and trimming by age would invent a horizon nobody chose. Storage therefore
grows with financial history rather than staying flat. If it becomes a problem the answer is a
product decision about how much history a phone should hold, not a constant chosen here.

**R3 — the commit-order lock now covers payments and cycle closes.** Feed-visible writes for a tenant
were already effectively serial, and a cycle close is rare, so this is uncontended in practice. It
does mean a close — which issues one statement per customer — holds the tenant's boundary for its
duration. For a large tenant that is the longest anyone will wait on it.

**R4 — no FX.** Provider costs in USD and customer billing in PKR are reported side by side and never
summed. That is correct and it is also less convenient than a single number. A combined total needs
an explicit FX source and a decision about which rate and when; both are out of scope.

**R5 — the monthly total counts a fixed rate whether or not the service was used.** A configured
fixed charge is an estimate for every month its rate covers. That is what a flat charge means, but it
does mean archiving an item the business has stopped paying for is the owner's job, not the system's.

**R6 — `localStorage` holds the refresh token** (P4 R2, P5 R4, unchanged). Mitigation is a CSP at
deployment. Worth a decision before P12.

**R7 — `react-router-dom@6` carries two moderate advisories** (P5 R6, unchanged). The fix is a major
upgrade to v7, unrelated to P6.

**R8 — A-SEC-9 (CI secret scanning) is still open**, from P1 through P6.

---

## 13. Recommended next package

**P7 — reminders and the scheduled job runner.** It is the last piece of the daily loop that is still
missing, everything it needs now exists (the derived payment status, the authoritative outstanding,
the tenant's reminder schedule), and it is the first package to need `app/jobs/` and the
authenticated internal job endpoint — which the deployment package will then have something concrete
to schedule.

Two things P7 should not lose: REM-2 requires the *current* authoritative outstanding recomputed at
send time, never an amount read from a statement or an earlier reminder; and `communication_log` is
its own table, not a column on the customer.

One thing P7 will want that P6 leaves ready: `GET /dashboard/outstanding` is already the "who owes
what, right now" query, computed once in the database.

---

## 14. Files

**Created**

```
backend/app/costs/__init__.py  models.py  estimates.py  commands.py  reporting.py
backend/app/billing/dashboard.py
backend/alembic/versions/0004_p6_operating_costs.py
backend/tests/test_operating_costs.py  test_dashboard.py  test_p6_sync_feed.py
frontend/src/api/finance.ts  costs.ts
frontend/src/dashboard/DashboardPage.tsx  dashboard.test.tsx
frontend/src/statements/StatementsPage.tsx
frontend/src/payments/RecordPaymentPage.tsx
frontend/src/customers/CustomerFinancials.tsx
frontend/src/costs/OperatingCostsPage.tsx  costs.test.tsx
docs/P6_HANDOVER.md
```

**Modified**

```
backend/app/core/money.py            round_half_up made public; multiply_minor added
backend/app/identity/capabilities.py cost:read, cost:write on OWNER_ADMIN
backend/app/audit/models.py          7 operating-cost actions
backend/app/audit/service.py         allow-lists for the 4 new entities
backend/app/sync/changes.py          payment + statement readers, head, FEED_VERSION 2
backend/app/sync/serialization.py    payment.record, payment.void, billing.close_cycle
backend/app/billing/statements.py    list_all_statements
backend/app/payments/commands.py     list_all_payments
backend/app/service/commands.py      list_customer_history
backend/app/api/schemas.py           5 operating-cost request models
backend/app/api/routes.py            dashboard_router, cost_router, +14 routes
backend/app/main.py                  registers both new routers
backend/app/db_models.py             P6_TABLES
backend/tests/test_architecture.py   costs in DOMAIN_PACKAGES + SEC-3 module list
backend/tests/test_tenant_isolation.py  14 routes in EXERCISED + 13 new cases
backend/tests/test_sync_changes.py      feed-scope guards rewritten for P6
backend/tests/test_sync_serialization.py  the pinned correspondence, now a map
backend/tests/test_schema_p3.py      pinned migration head -> ancestry check (D3)
frontend/src/api/types.ts            payment, statement, dashboard, cost shapes
frontend/src/api/operation.ts        online-only op types
frontend/src/lib/money.ts            majorToMinor / minorToMajor moved here
frontend/src/customers/CustomerForm.tsx        re-exports them
frontend/src/customers/CustomerDetailPage.tsx  the financial view
frontend/src/sync/types.ts  stores.ts  engine.ts  SyncProvider.tsx  useLocalData.ts
frontend/src/App.tsx                 4 routes
frontend/src/components/AppShell.tsx 3 destinations
frontend/src/styles.css              stats, money lists, statement, payment form, costs
frontend/src/test/fixtures.tsx       payment/statement/dashboard fixtures, 2 seed stubs
frontend/src/sync/sync.test.tsx      feed-version numbers
frontend/src/daily/completeness.test.tsx  the two new seed reads
CLAUDE.md                            phase, boundaries, the three money systems
docs/P0_ARCHITECTURE_FREEZE.md       §15a, dated 2026-09-03
docs/P0_INVARIANTS_AND_ACCEPTANCE.md §7a, COST-1..11 + acceptance
```

**Dependencies added:** none, on either side. P6 needed no new library.

---

## 15. Git state

Branch `yahya`, base `4e6e4f8`, upstream `origin/yahya`. **Nothing was committed and nothing was
pushed**, as instructed. `git diff --check` reports no whitespace errors. No secret and no `.env`
value is in the diff. All changes are in the working tree.
