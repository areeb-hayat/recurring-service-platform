# P0 — Architecture Freeze

Status: **FROZEN**. This document is design only; no implementation exists yet.
Requirements source: `README.md` and the P0 package brief. Nothing here adds product scope.

---

## 1. Stack decision

### 1.1 Options evaluated

**A. Python / FastAPI / PostgreSQL + React TS PWA** (the candidate in the brief).

**B. Cloudflare-native full TypeScript** — Workers + Hono + D1 + Drizzle + React, Cron Triggers,
one language end to end, generous free tier.

**C. Simpler server-rendered monolith** — Django or Rails with HTMX, no separate frontend build.

### 1.2 Verdict — Option A is frozen

Option C is eliminated by one hard requirement: **offline-first daily recording**. A persistent
IndexedDB outbox, a Service Worker shell, and visible sync states are a real client-side
application. HTMX cannot queue writes across a browser restart. Bolting a PWA layer onto a
server-rendered app reintroduces the SPA anyway, with two rendering models instead of one.

Option B is genuinely attractive — one language, shared types, cheap hosting, cron included — and
was not dismissed lightly. It loses on the things this product is actually made of:

| Requirement | Postgres + FastAPI | Cloudflare + D1 |
| --- | --- | --- |
| Partial unique index (duplicate-service prevention on *active* rows only) | native, routine | SQLite has partial indexes, but D1's migration/verification story is weaker |
| Composite FKs `(tenant_id, id)` for tenant isolation | enforced, standard | SQLite FK enforcement is per-connection and historically fragile |
| One transaction across ledger + source document + commission | ordinary | Worker CPU/time limits push logic toward compensation instead of transactions |
| Backup / point-in-time recovery for a money ledger | commodity on every host | thinner, provider-specific |
| Financial test maturity | pytest + `Decimal` + property tests | workable, less established |
| Deployment portability | any container host or VPS | effectively locked to Cloudflare |

The deciding factor: this is a **money ledger for a paying client that may become a licensed
product**. Boring relational guarantees and portability outweigh the single-language benefit.
Option B's real advantage is kept anyway — the backend publishes OpenAPI and the frontend generates
its TypeScript types from it, so there is one schema contract without a TypeScript backend.

### 1.3 Frozen stack

| Layer | Choice |
| --- | --- |
| Language | Python 3.12 |
| API | FastAPI + Pydantic v2 |
| ORM / migrations | SQLAlchemy 2.x (typed) + Alembic |
| Database | PostgreSQL 16 (authoritative) |
| Auth | short-lived JWT access token + opaque DB-stored refresh token |
| Frontend | React 18 + TypeScript + Vite |
| Client store | IndexedDB via a thin typed wrapper (`idb`) |
| PWA | Vite PWA plugin / Workbox for the shell; custom sync logic (not Background Sync alone) |
| Server state | TanStack Query, hydrated from and written through IndexedDB |
| Backend tests | pytest, `pytest-asyncio`, `hypothesis` for money/rounding properties |
| Frontend tests | Vitest + Testing Library |
| E2E | Playwright (offline/online toggling is first-class there) |
| Scheduling | host cron → authenticated internal job endpoint (no Celery, no Redis) |

Explicitly **not** adopted: message brokers, event-sourcing frameworks, microservices, Kubernetes,
a background worker daemon, a cache server. No requirement forces any of them.

---

## 2. Overall architecture

```
 ┌──────────────────── Browser (PWA) ──────────────────────────────┐
 │  React UI                                                       │
 │  Service Worker  →  app shell + read cache                      │
 │  IndexedDB       →  outbox + issues + snapshot + meta           │
 │  Sync engine     →  batch POST, per-operation results           │
 └──────────────────┬──────────────────────────────────────────────┘
                    │ HTTPS / REST / JSON
 ┌──────────────────▼──────────────────────────────────────────────┐
 │  FastAPI application (single deployable process)                │
 │  api/      thin routers: authn, authz, validate                 │
 │  domain/   tenancy identity customers service billing           │
 │            payments reminders commission sync search voice      │
 │  ports/    CommunicationProvider   SpeechToTextProvider         │
 │            SearchInterpreter       OperationalIntentInterpreter │
 │  adapters/ comms/  speech/  ai/   (mock + real)                 │
 │  jobs/     daily reminder / statement / cycle jobs              │
 └──────────────────┬──────────────────────────────────────────────┘
                    │
            ┌───────▼────────┐   ┌──────────────────────┐
            │  PostgreSQL    │   │ external (pluggable) │
            │  authoritative │   │  GHL / WhatsApp      │
            └────────────────┘   │  speech-to-text      │
                                 │  AI interpreters     │
                                 └──────────────────────┘
```

One process, one database. The frontend is static assets and may be served by the same process or
a CDN; that choice does not change the architecture.

### 2.1 Module boundaries

```
app/
  core/        money.py ids.py clock.py config.py security.py errors.py
  tenancy/     tenant, settings resolution
  identity/    users, sessions, capabilities
  customers/
  service/     daily service records, corrections
  billing/     billing cycles, ledger, statements
  payments/    manual payment ledger
  reminders/   schedule evaluation, communication log
  commission/  plans, events, adjustments, settlements
  sync/        operation envelope, dispatch, change feed
  search/      structured filter schema + validator
  voice/       transcript -> candidate intent -> resolution -> confirmation preview
  ports/       Protocol definitions only:
                 CommunicationProvider
                 SpeechToTextProvider
                 SearchInterpreter
                 OperationalIntentInterpreter
  adapters/
    comms/     mock.py  (later: ghl.py)
    speech/    mock.py  groq.py   (whisper-large-v3 via SPEECH_MODEL)
    ai/        groq.py  null.py
  api/         routers
  jobs/        run_daily.py
```

Rules, enforced by review and an import-linter test:

- `domain → ports` allowed. `domain → adapters` **forbidden**.
- `adapters → core` and `adapters → ports` allowed. `adapters → domain` forbidden.
- `api → domain` allowed. `domain → api` forbidden.
- No vendor name (`ghl`, `groq`, or any STT vendor) may appear outside `app/adapters/`,
  `app/core/config.py`, and configuration files.
- `voice/` orchestrates; it owns no accounting logic and no write path of its own. It resolves and
  validates, then calls the same `service/` command the buttons call (§8).

---

## 3. Roles and authorization

### 3.1 Principals

| Principal | Scope | Exists in V1 | Logs in |
| --- | --- | --- | --- |
| Business Owner / Admin | tenant | yes | yes |
| Operator / Employee | tenant | **no** (role value reserved, unused) | — |
| Platform Owner | platform | yes | yes |
| Customer | tenant data subject | yes, as a record | **no** |

A principal is either tenant-scoped (`tenant_id` set) or platform-scoped (`tenant_id` NULL). No
principal is both. The platform owner does **not** get implicit access to tenant business data; if
platform support access is ever needed it becomes an explicit, audited, separately designed
feature — deferred, not assumed.

### 3.2 Authorization model

No RBAC framework. A static capability map plus one explicit check:

```python
CAPABILITIES = {
    Role.OWNER_ADMIN: {
        "customer:read", "customer:write",
        "service:record", "service:correct",
        "billing:read", "billing:close_cycle",
        "payment:record", "payment:void",
        "reminder:read", "reminder:trigger",
        "dashboard:read", "search:use",
    },
    Role.PLATFORM_OWNER: {
        "commission:read", "commission:adjust", "commission:settle",
        "tenant:provision", "platform_dashboard:read",
    },
    Role.OPERATOR: set(),   # reserved for a future package, unused in V1
}
```

Every router dependency resolves a `Principal`, then calls
`require(principal, "payment:record")`. The set is deliberately flat and small; if it grows past
roughly thirty entries, revisit the model rather than adding hierarchy.

The two capability sets are **disjoint by construction**: no tenant role holds any `commission:*`
capability, and no platform role holds any tenant business capability. That is what makes
"business owner cannot reach platform commission authority" true without runtime cleverness.

### 3.3 Sessions

- Access token: JWT, 60 minutes, carrying `user_id`, `scope`, `tenant_id`, `role`.
- Refresh token: opaque random value, hashed at rest in `user_session`, 30 days, revocable.
- Offline behaviour: an expired access token does **not** block local recording. The PWA keeps
  queueing into the outbox and refreshes on reconnect. If refresh fails, the outbox is preserved
  and the user is asked to re-authenticate; queued operations are never discarded.

### 3.4 Tenant isolation — defence in depth

1. **Schema.** Every business table has `tenant_id NOT NULL`. Every parent table has
   `UNIQUE (tenant_id, id)`, and every child references it with a composite foreign key
   `(tenant_id, parent_id)`. A row physically cannot point at another tenant's row.
2. **Data access.** Every repository entry point takes a `TenantContext` derived from the principal
   and adds `tenant_id = :tenant_id` to every statement. There is no repository call that omits it.
3. **Tests.** A dedicated isolation suite creates two tenants and asserts every read and write
   endpoint returns 404 (not 403) for the other tenant's identifiers.

Postgres row-level security is **not** used in V1: with composite FKs and a mandatory scoping layer
it adds operational complexity without closing a remaining gap. It stays available later without a
data-model change.

---

## 4. Tenant model

Single database, single schema, `tenant_id` column on every business row. Not schema-per-tenant,
not database-per-tenant — both cost migration complexity that a handful of licensed businesses do
not justify, and both remain reachable later by export/import.

Tenant provisioning in V1 is a platform-owner action (or a seed script), not self-service signup.
No public registration endpoint exists.

Per-tenant settings live on the `tenant` row: business name, currency code, currency minor-unit
exponent, unit label, timezone, billing-cycle configuration, reminder schedule, default price and
quantity. These are business configuration, never code constants.

---

## 5. Financial authority

### 5.1 Representation

| Concept | Type | Notes |
| --- | --- | --- |
| Money | `BIGINT` minor units, Python `int` | e.g. paisa. Never float; never `Decimal` in storage |
| Currency | ISO-4217 code + integer exponent on `tenant` | PKR/2 by default; exponent drives display |
| Quantity | `NUMERIC(12,3)` / `Decimal` | litres, bottles, service units — **not** assumed integer |
| Unit price | `BIGINT` minor units **per one unit** | snapshotted onto every accepted record |
| Commission rate | `INTEGER` basis points (0–10000) | 250 bp = 2.50%; never a float |

JSON contract: money is an integer count of minor units in a field suffixed `_minor`, alongside
`currency` and `currency_exponent` on the response envelope. The client formats; it never
arithmetises money.

Rationale for integer minor units: exact, closed under addition, trivially comparable, survives
JSON and JavaScript intact, and removes every float-drift class of bug. No concrete reason to
deviate was found.

### 5.2 The one rounding rule

```
charge_minor = round_half_up(quantity * unit_price_minor)
```

Rounding happens **exactly once**, at the daily service record, using `Decimal` with
`ROUND_HALF_UP`. Statements, dashboards, and outstanding balances are integer sums of
already-rounded values and never re-round. This makes every total reproducible and makes
"statement total ≠ sum of its lines" impossible by construction.

Commission uses the same rule at the commission-event level:
`commission_minor = round_half_up(base_amount_minor * rate_bp / 10000)`.

### 5.3 The ledger

`ledger_entry` is the **single derivation source for every balance in the product**. It is strictly
append-only: no `UPDATE`, no `DELETE`, no status column.

```
outstanding(customer) = SUM(ledger_entry.amount_minor WHERE tenant, customer)
```

Sign convention: positive increases what the customer owes.

| `entry_kind` | Sign | Created by |
| --- | --- | --- |
| `OPENING` | + or − | customer onboarding opening balance |
| `CHARGE` | + | accepted daily service record |
| `PAYMENT` | − | accepted manual payment |
| `ADJUSTMENT` | + or − | correction, void, or reversal of a source document |

Source documents (`daily_service_record`, `payment`) carry lifecycle status. The ledger carries
only facts. A void appends an equal-and-opposite `ADJUSTMENT`; a correction appends the difference.
The original entry is never touched — which is exactly the README's "the historical record remains
visible".

**Adjustment source attribution (clarified in P1).** An `ADJUSTMENT` is attached to the record
whose *active life is ending*, never to its replacement. One rule covers both operations: when a
record stops being active, post `(replacement_charge - its own charge)` against **that** record —
a void is simply `replacement_charge = 0`. This is what keeps the
`(tenant_id, source_type, source_id, entry_kind)` uniqueness satisfiable for a correction chain of
any length: a record is either superseded or voided, never both, so it can carry at most one
`ADJUSTMENT`. Attaching the delta to the replacement instead collides the moment that replacement
is later voided.

**Adjustment origin (frozen).** Every `ADJUSTMENT` inherits the `source_type` of the document it
compensates: `daily_service_record` ⇒ **service-origin**, `payment` ⇒ **payment-origin**. Origin,
not sign, decides which report an adjustment belongs to. §11.1 depends on this distinction, and
reporting code must filter on `source_type` — summing `ADJUSTMENT` entries by `entry_kind` alone is
a defect, not a shortcut.

This single-table choice is also the *simplest* correct option: one place to sum, one invariant to
protect, no reconciliation between three tables.

### 5.4 The core invariant

```
Previous Outstanding + Current Cycle Charges − Payments = Current Outstanding
```

is not enforced by a stored field — it is **true by construction**, because outstanding is a
running sum over one append-only table. A statement is a frozen presentation of a slice of that
sum:

```
closing = opening
        + charges              (Σ CHARGE)
        + service_adjustments  (Σ ADJUSTMENT where source_type = daily_service_record, signed)
        − payments             (Σ PAYMENT, expressed positive)
        + payment_reversals    (Σ ADJUSTMENT where source_type = payment)
```

over entries whose `posting_cycle_id` is that cycle. Service adjustments and payment reversals are
carried as **separate statement columns**, never as one mixed `adjustments` figure: a customer
reading a bill must be able to tell "we corrected what you were charged" from "a payment was
reversed", and §11.1 needs the two separated to define billed value unambiguously.

### 5.5 Billing cycles and carry-forward

- `billing_cycle` is per tenant, default calendar-monthly in the tenant timezone. Exactly one
  `OPEN` cycle per tenant, enforced by a partial unique index.
- **`period_end` is inclusive, and a cycle may not be closed until it has passed (clarified in
  P2).** The earliest valid close is `business_date > period_end`: the period is still running
  throughout `period_end` itself, so business events dated that day stay eligible to post to the
  cycle no matter what time somebody attempts to close it. Closing sooner would end the period
  somewhere other than where the tenant's configuration says it ends, and the days between the
  close and the real boundary would be billed in the *following* cycle. Neither the shortened
  period nor that carry-over was ever a client decision, so V1 refuses the close rather than
  inventing one, and there is no override flag. Cycles are therefore always full configured
  periods. An explicit early-close feature, if it is ever wanted, is a separate design with its own
  product decision.
- **An `OPEN` cycle whose period has ended accepts no new entry (clarified in P2).** Rollover is
  not automatic, so a cycle can still be `OPEN` after its `period_end` — an August cycle nobody
  closed, seen on 1 September. Posting into it would file September's business under a period that
  is over: a mis-stated bill, not a late one. Such a write **fails closed**, asking for the close
  operation, and is never resolved by auto-closing the stale cycle from inside a service or payment
  command — closing issues statements, and issuing a customer's bill as a side effect of recording
  a delivery is not a decision a write command may take. A scheduled rollover calls the real close.
  This is one-directional and does not touch §5.5's late-correction or backdating rules: an entry
  may post to a cycle that *began* before it, never to one that *ended* before it.
- Carry-forward needs no transfer entry. The ledger is continuous per customer, so the next
  statement's opening balance *is* the previous statement's closing balance.
- Statements are **immutable documents** once issued.
- **Late-correction rule (frozen):** a correction or void affecting a closed cycle keeps its
  original `occurred_on` (the true service date) but is posted to the currently `OPEN` cycle via
  `posting_cycle_id`. Issued statements never change; the adjustment appears as a line on the next
  statement. This keeps the invariant chain unbroken across statement boundaries without ever
  rewriting a delivered bill.

### 5.6 Payment status

Customer status is derived, never stored:

- `PAID` — outstanding ≤ 0
- `UNPAID` — outstanding > 0 and no payment recorded against the current cycle
- `PARTIALLY_PAID` — otherwise

Computed in one function used identically by the dashboard, statements, and the reminder engine.

---

## 6. Data model

`id` is UUIDv7 — time-ordered, generatable on the device while offline, and not enumerable across
tenants. `row_version` is a `BIGINT` drawn from one shared Postgres sequence, used for both
optimistic concurrency and the sync change feed.

**Which tables carry `row_version` (clarified in P2).** Every table whose rows appear in the
client's authoritative `snapshot` store (§7.1) carries it, because §7.4 pages that snapshot on
`row_version > since`: `tenant`, `customer`, `daily_service_record`, `ledger_entry`, `payment` and
`statement`. A related row's version is never a substitute for the record's own — a client pulling
payment history cannot page on the ledger entry a payment happens to post. Server-side tables that
are not client sync entities — `billing_cycle`, `audit_event`, `sync_operation`, `job_run`, the
`commission_*` family — deliberately do not carry it; adding one for symmetry would make a table a
sync entity by accident.

Eighteen tables. Each entry gives: tenant ownership → key uniqueness → immutability → correction link.

**`tenant`** — root. `id`, `slug` (unique), `name`, `currency`, `currency_exponent`, `unit_label`,
`timezone`, `cycle_type`, `cycle_start_day`, `reminder_schedule` (JSONB),
`default_unit_price_minor`, `default_quantity`, `status`. Mutable configuration; changes audited.

**`app_user`** — `tenant_id` (NULL ⇒ platform scope), `email`, `password_hash`, `role`, `status`.
Unique `(tenant_id, lower(email))`; separate unique on `lower(email)` where `tenant_id IS NULL`.

**`user_session`** — `tenant_id`, `user_id`, `refresh_token_hash` (unique), `expires_at`,
`revoked_at`, `device_label`.

**`customer`** — `tenant_id`, `code` (human reference), `name`, `phone_e164`, `whatsapp_e164`,
`address`, `area` (indexed — powers "unpaid customers in G-10"), `default_quantity NUMERIC(12,3)`,
`unit_price_minor`, `status`, `row_version`. Unique `(tenant_id, id)` and `(tenant_id, code)`.
A price change mutates the row and writes an `audit_event`; **historical prices are protected by
snapshots on records, not by a price-history table.**

**`daily_service_record`** — the `[-] qty [+] CONFIRM / SKIP` fact.
`tenant_id`, `customer_id`, `service_date DATE`, `quantity NUMERIC(12,3)`,
`unit_price_minor` (snapshot), `unit_label` (snapshot), `charge_minor`, `kind`
(`SERVICE` | `SKIP`), `status` (`ACTIVE` | `SUPERSEDED` | `VOIDED`), `corrects_id`,
`superseded_by_id`, `adjustment_minor`, `reason`, `recorded_by_user_id`, `operation_id`,
`source` (`ONLINE` | `SYNC` | `IMPORT`), `input_method` (`BUTTON` | `VOICE`, default `BUTTON`),
`recorded_at`, `row_version`.

- Unique `(tenant_id, customer_id, service_date) WHERE status = 'ACTIVE'` — **this partial index is
  the duplicate-service guarantee**, covering repeated CONFIRM taps and multi-device races alike.
- `SKIP` stores `quantity = 0`, `charge_minor = 0`, and still creates a row (a skip is a recorded
  business fact) but creates **no** ledger entry.
- Immutable except the single `ACTIVE → SUPERSEDED | VOIDED` transition plus `superseded_by_id`,
  performed in the same transaction as the replacement.
- Check: `quantity >= 0`, `unit_price_minor >= 0`, `charge_minor >= 0`.
- `source` is *transport* (how the write arrived); `input_method` is *provenance* (how the human
  expressed it). They are orthogonal — a voice-entered record synced from the outbox is
  `source = SYNC`, `input_method = VOICE`. Provenance is metadata only: it never changes
  validation, pricing, uniqueness, or any downstream behaviour (§8).

**`billing_cycle`** — `tenant_id`, `period_start`, `period_end`, `status` (`OPEN` | `CLOSED`),
`closed_at`. Unique `(tenant_id, period_start)`; partial unique `(tenant_id) WHERE status = 'OPEN'`.

**`statement`** — the issued document. `tenant_id`, `customer_id`, `cycle_id`, `issued_at`,
`opening_balance_minor`, `charges_minor`, `service_adjustments_minor` (signed),
`payments_minor` (positive = received), `payment_reversals_minor` (positive = reversed),
`closing_balance_minor`, `service_days`, `total_quantity`, `unit_label`, `currency`, `row_version`.
Unique `(tenant_id, customer_id, cycle_id)`. **Fully immutable after issue** — `row_version` is
drawn once, at issue, and like every other column never changes again. The movement columns
satisfy the §5.4 identity exactly, and adjustments are split by origin rather than merged — that
split is what makes billed value (FIN-15) computable from statements without contamination.

**`ledger_entry`** — `tenant_id`, `customer_id`, `entry_kind`, `amount_minor`, `occurred_on DATE`,
`posting_cycle_id`, `source_type`, `source_id`, `created_at`, `created_by_user_id`, `row_version`.
Unique `(tenant_id, source_type, source_id, entry_kind)` — the guarantee that one source document
can never post the same kind of entry twice. Indexes `(tenant_id, customer_id, id)` for balance
sums and `(tenant_id, posting_cycle_id)` for statements. Check `amount_minor <> 0`.
**Append-only. No update, no delete, ever.**

**`payment`** — an accepted money-in fact, recorded by the owner. `tenant_id`, `customer_id`,
`amount_minor`, `method` (`CASH` | `BANK_TRANSFER` | `OTHER`), `received_on DATE`, `reference`,
`note`, `status` (`RECORDED` | `VOIDED`), `voided_reason`, `voided_by_user_id`, `voided_at`,
`operation_id`, `recorded_by_user_id`, `source`, `recorded_at`, `row_version`.
Check `amount_minor > 0`. Voiding appends a compensating ledger entry; the row is never deleted.
`row_version` advances on the `RECORDED -> VOIDED` transition, so a client holding payment history
offline sees the void on its next delta.

V1 payments are **manual only** — there is no online gateway, no provider reference, and no
externally verified payment state (see §8 and the scope note in §16). Every payment in the system
is one the owner entered, which makes `operation_id` the whole of its duplicate protection.

> **Honest limitation, recorded deliberately:** two genuinely separate cash payments of the same
> amount on the same day are legal and must not be blocked. Duplicate protection for manual
> payments therefore rests on `operation_id`, not on an amount/date natural key. The UI warns on a
> same-amount same-day repeat; it does not forbid it.

**`reminder`** — `tenant_id`, `customer_id`, `cycle_id`, `schedule_day`, `kind`
(`STATEMENT` | `REMINDER` | `FINAL` | `OWNER_ALERT`), `amount_minor_at_generation`,
`state` (`PENDING` | `SENT` | `FAILED` | `CANCELLED`), `generated_at`, `sent_at`.
Unique `(tenant_id, customer_id, cycle_id, schedule_day)` — a re-run of the daily job cannot
duplicate a reminder.

**`communication_log`** — `tenant_id`, `customer_id`, `reminder_id`, `channel`, `provider`,
`template_key`, `payload` (JSONB — the rendered values we sent), `provider_message_id`,
`state` (`QUEUED` | `ACCEPTED` | `DELIVERED` | `FAILED`), `error`, `attempt_no`, `created_at`.
Never referenced by billing logic.

**`audit_event`** — `tenant_id` (nullable for platform actions), `actor_user_id`, `actor_scope`,
`action`, `entity_type`, `entity_id`, `before` (JSONB), `after` (JSONB), `reason`, `request_id`,
`source`, `occurred_at`. Written for corrections, voids, reversals, price changes, customer
create/update, settlements, configuration changes, and authentication events — **not** for reads.
Append-only.

**`sync_operation`** — the idempotency register. `tenant_id`, `operation_id` (client-generated),
`user_id`, `op_type`, `request_hash`, `status` (`APPLIED` | `REJECTED` | `CONFLICT`),
`result` (JSONB — the exact response body that was returned), `entity_type`, `entity_id`,
`received_at`. Unique `(tenant_id, operation_id)`. **Retained indefinitely in V1 — never pruned.**
Any retention horizon silently becomes a *duplication* horizon: an operation retried after the
cut-off would be accepted a second time. That is unacceptable for manual payments in particular,
where two genuine equal payments on the same day are legal and there is deliberately no amount/date
natural key to fall back on (see `payment` above). At the expected scale — one small row per
business write — indefinite retention is far cheaper than a tombstone or archival subsystem, which
V1 must not build.

**`commission_plan`** — `tenant_id`, `basis` (`RECORDED_VALUE` | `BILLED_VALUE` |
`COLLECTED_VALUE` | `PER_EVENT`), `rate_bp` (nullable), `fixed_amount_minor` (nullable),
`currency`, `effective_from`, `effective_to`, `created_by_user_id`.
Check: exactly one of `rate_bp` / `fixed_amount_minor` is set; `rate_bp BETWEEN 0 AND 10000`.
Effective ranges must not overlap per tenant. Platform-scope write only.

**`commission_event`** — `tenant_id`, `plan_id`, `basis_snapshot`, `rate_bp_snapshot`,
`fixed_amount_minor_snapshot`, `source_type`, `source_id`, `base_amount_minor`, `commission_minor`,
`occurred_on`, `created_at`.
Unique `(tenant_id, source_type, source_id)`. **Carries no settlement reference** — V1 settles in
aggregate and does not allocate settlements to individual earning events (§11.1). **Immutable** — the plan terms in force are
snapshotted so a later plan change cannot rewrite earned history. Created **only after** the source
business event has been centrally accepted and committed.

**`commission_adjustment`** — `tenant_id`, `commission_event_id`, `amount_minor` (signed), `reason`,
`source_type`, `source_id`, `created_by_user_id`, `created_at`. Carries no settlement reference,
for the same reason as `commission_event`.
Unique `(tenant_id, source_type, source_id)`. Immutable. Every correction, void, or reversal of a
commissionable source event produces exactly one adjustment.

**`commission_settlement`** — `tenant_id`, `period_start`, `period_end`, `amount_minor`,
`settled_on`, `reference`, `note`, `created_by_user_id` (platform scope only), `created_at`.
An **independent, append-only record of money actually settled** between the platform and the
tenant. It references no earning event, stamps nothing on events or adjustments, and deletes or
rewrites nothing. This is precisely what makes partial settlement representable — see §11.1.

```
commission_outstanding = Σ commission_event.commission_minor
                       + Σ commission_adjustment.amount_minor
                       − Σ commission_settlement.amount_minor
```

**`job_run`** — `tenant_id`, `job_kind`, `business_date`, `started_at`, `finished_at`, `status`,
`stats` (JSONB), `error`. Unique `(tenant_id, job_kind, business_date)` — the daily job is safely
re-runnable and cannot double-fire on a retry or a duplicated cron trigger.

**Deliberately not created:** any voice, audio, or transcript table (§8.4 — raw audio is never
persisted and transcripts are ephemeral), a balance cache table (outstanding is an indexed integer sum; add
materialisation only when measurement demands it), a price-history table (snapshots cover it), a
generic event store, a settlement-allocation table (V1 settles in aggregate, not per event; §11.1),
and any `operator` or `customer_login` table.

---

## 7. Offline / sync contract

Contract only. Implementation is P5.

### 7.1 Client stores (IndexedDB)

| Store | Contents | Cleared when |
| --- | --- | --- |
| `outbox` | write operations awaiting or eligible for automatic retry, ordered, with attempt counts | on `APPLIED` / `DUPLICATE`, or on promotion to `issues` |
| `issues` | unresolved `REJECTED` / `CONFLICT` operations: `operation_id`, the original intent/payload, the error or `server_state`, and a resolution state | only on explicit user resolution or dismissal |
| `snapshot` | last server-authoritative reads: customers, customer detail, balance/status, daily history, payment history, statements, outstanding list, dashboard | on explicit refresh |
| `meta` | `sync_cursor`, `last_synced_at`, auth hints | on sign-out |

The Service Worker caches the app shell. It never fabricates a financial response: an offline read
with no snapshot shows "unavailable offline", not a computed guess. Local search and filtering run
against `snapshot` only.

### 7.2 Operation envelope

```json
{
  "operation_id": "uuidv7 generated on the device",
  "op_type": "service.record | service.skip | service.correct | payment.record | customer.update",
  "payload": { "...": "..." },
  "client_created_at": "2026-09-02T05:11:22Z",
  "base_row_version": 41822
}
```

`operation_id` is generated **once**, at the moment the user taps CONFIRM, and is never regenerated
on retry. That single rule is what makes a lost response safe.

> **Clarification — 2026-09-03 (P5).** The `op_type` list above is the envelope's *extensible
> vocabulary*, not the set of operations a device may queue. **V1's offline write guarantee is
> `service.record` and `service.skip` — CONFIRM and SKIP — and nothing else.** Payments, service
> corrections and voids, and customer create/edit are online-only operations in V1: they keep this
> same envelope shape, and admitting one later is a registry entry in `app/sync/envelope.py` plus a
> client screen, not a redesign. `POST /sync/operations` refuses any other `op_type` with a
> per-operation `REJECTED`. This narrows nothing that was ever built — §8.6 already made "the hard
> offline guarantee" the button daily entry — it states the settled V1 product scope where the
> enumeration above read as a promise.

### 7.3 Server protocol

`POST /api/v1/sync/operations` accepts a batch and returns one result per operation, each processed
independently in its own transaction:

```json
{ "results": [
  { "operation_id": "...", "status": "APPLIED",   "entity": { "...": "..." } },
  { "operation_id": "...", "status": "DUPLICATE", "entity": { "...": "..." } },
  { "operation_id": "...", "status": "REJECTED",  "error": { "code": "VALIDATION", "detail": "..." } },
  { "operation_id": "...", "status": "CONFLICT",  "error": { "code": "SERVICE_ALREADY_RECORDED" },
    "server_state": { "...": "..." } }
]}
```

- `APPLIED` — first acceptance; the row and its `sync_operation` register entry are written in the
  same transaction.
- `DUPLICATE` — `(tenant_id, operation_id)` already exists; the **stored original result** is
  returned. Nothing new is created and no side effect fires. This is the lost-response guarantee.
  The requirement is the *same logical result*: the same authoritative entity, semantically equal to
  what the first acceptance returned. It is deliberately **not** a promise of byte-identical
  serialization — `sync_operation.result` is JSONB, key order and whitespace are not preserved, raw
  response bytes are not stored, and correctness does not need them.
- `REJECTED` — validation or authorization failure. Terminal for retry: the client removes it from
  the outbox **and writes it into `issues` in the same local transaction**, so the problem outlives
  the removal instead of vanishing with it.
- `CONFLICT` — a different operation already occupies the same business slot (another device
  recorded that customer/date), or `base_row_version` no longer matches. **Never auto-merged.** The
  server returns its authoritative state; the client moves the operation out of the retry queue and
  into `issues` with that state attached. A conflicting operation is **never** automatically
  resubmitted unchanged — that would either loop forever or silently overwrite another device.

A network error, a timeout, or a 5xx is **not** a verdict. The operation stays in `outbox` and
retries normally with backoff; only the four verdicts above move an operation out of the queue.

The server validates every synchronised operation exactly as it validates an online one. There is
no privileged offline path.

### 7.4 Pull / delta

`GET /api/v1/sync/changes?since=<row_version>` returns rows with `row_version > since` across the
syncable tables, plus a new cursor. `row_version` comes from one shared Postgres sequence, so the
cursor is globally monotonic and gap-tolerant.

### 7.5 Visible sync states

`Synced` · `Offline` · `Last synced <time>` · `N changes waiting` · `Syncing` · `Needs Attention`.

`N changes waiting` counts `outbox`. `Needs Attention` is driven by `issues` and is non-dismissable
while any entry there is unresolved — across a refresh, across a browser restart, and across later
successful syncs of unrelated operations. The resolution UX itself is a P5 design task; P0 freezes
only this persistence and retry contract.

### 7.6 Guarantees

- Refresh, or close and reopen, loses nothing: the outbox is durable IndexedDB, written **before**
  the network call is attempted.
- A response lost after server acceptance cannot duplicate: the retry receives `DUPLICATE`, with
  the same logical result and no new side effect. Because the register is never pruned, this holds
  for the life of the system rather than for a retention window.
- Multi-device collisions are **detected**, never guessed.
- A `REJECTED` or `CONFLICT` outcome is never lost: it is persisted to `issues`, survives refresh
  and browser restart, and waits for a person.
- Commission is created only inside the server transaction that accepts the business event — never
  on the device, never optimistically.

---

## 8. Voice input architecture

Target users may have limited software literacy, so voice is a first-class V1 input method — an
easier way to express the *same* operations the buttons already perform, never a second way to
change money.

### 8.1 The two voice experiences

**B1 — Voice search (read-only).**

```
audio → SpeechToTextProvider → transcript → SearchInterpreter → CustomerSearchFilter
      → server validation → deterministic tenant-scoped query
```

Identical to typing the same sentence. The voice layer adds **no** search authority: it produces a
transcript and nothing else, then joins the existing §12 path.

**B2 — Voice daily service entry (write, confirmed).**

```
audio → SpeechToTextProvider → transcript → OperationalIntentInterpreter
      → candidate intent → server resolution + validation → confirmation card
      → [user taps CONFIRM] → the SAME service command the buttons call → ledger / audit / commission
```

"Essa bought 2 bottles", "Essa got three units today", "Essa ne 3 kitaabein khareedein" and
equivalent English / Roman Urdu / code-switched phrasing all resolve to the one candidate intent:

```json
{ "intent": "RECORD_SERVICE", "customer_reference": "Essa",
  "quantity": "2", "spoken_unit": "bottle", "date_reference": "today" }
```

That object is a **candidate only**. It is not a command, and nothing in the system acts on it
until a person confirms.

### 8.2 One domain path (the structural guarantee)

There is deliberately **no voice write endpoint**. `/voice/*` transcribes and interprets; it cannot
create anything. Confirmation posts to the ordinary `POST /service/records` with an
`operation_id`, exactly as the `[-] qty [+] CONFIRM` button does. That is the whole of "voice must
use the same domain path" — enforced by the route table (§15), not by convention.

Consequently a voice-origin record is, after acceptance, indistinguishable from a button-origin one
except for `input_method = VOICE`. It inherits every FIN, SEC, SYN, and AUD invariant unchanged:
same tenant scoping, same `operation_id` idempotency, same active-record uniqueness, same immutable
history, same commission trigger. There is no "voice accounting logic" to keep in step.

### 8.3 Confirmation and fail-closed resolution

Speech recognition and name matching are both fallible, so a voice mutation never posts silently.
The server resolves the candidate deterministically and returns a preview; the UI shows a large,
low-reading confirmation card:

```
I heard:
  Essa
  2 bottles
  Today
Calculated charge: Rs. 500
[ CONFIRM ]   [ TRY AGAIN ]
```

The charge shown is computed **server-side** by the ordinary pricing rule (§5.2) — the model never
sees or supplies a price or an amount.

Resolution rules, all failing closed into ordinary UI rather than guessing:

| Situation | Behaviour |
| --- | --- |
| Customer name matches exactly one active customer | resolved; shown for confirmation |
| Name matches several plausibly | **never auto-selects** — a short candidate list with large buttons |
| Name matches none | unresolved; fall back to the normal customer picker |
| Quantity missing or unparseable | **never invented** — ask, or fall back to the `[-] qty [+]` control |
| Spoken unit conflicts with the tenant's configured `unit_label` | fail closed to clarification; configuration is never silently overridden |
| Date materially ambiguous | **never invented** — default is nothing; ask or use the normal date control |
| Anything else | `UNRESOLVED` → ordinary UI, no candidate |

Customer resolution is deterministic server-side matching against the tenant's own customers. The
interpreter is never given the customer list (§8.5), so resolution authority stays in the database.

### 8.4 Allowed voice mutations in V1, and privacy

Voice may perform exactly three things: **search/query**, **`RECORD_SERVICE`**, and
**`SKIP_SERVICE` for today**. It may **not** record payments, change prices, touch commission or
settlements, make corrections, void or reverse anything, change tenant configuration, or manage
users. Those stay in explicit UI flows. This is a safety boundary for V1, not a permanent product
limit — and because the closed intent schema (§8.5) cannot *represent* those commands, the boundary
is structural rather than a filter someone can forget to apply.

Privacy rules, frozen conservatively:

- Raw audio is **never persisted** by our application. There is no recording archive, and no audio
  reaches audit history.
- Transcripts are ephemeral — held only for the life of the confirmation, never written to a table.
- Accepted records keep ordinary structured business data plus `input_method = VOICE`. Nothing else
  about the utterance survives.
- Persistent transcript storage would need an explicit future product and privacy decision (D13).
- No voice analytics, and no text-to-speech output, in V1.

### 8.5 Ports — speech and operational intent

Both are behind ports; no vendor is frozen in P0.

```python
class SpeechToTextProvider(Protocol):
    name: str
    capabilities: SttCapabilities        # languages, max_audio_seconds, supports_language_hint

    def transcribe(self, audio: bytes, language_hint: str | None = None) -> TranscriptResult: ...
```

`TranscriptResult` expresses `text`, `confidence` (when the provider reports one), `detected_language`
(when available), and an explicit `failed` / `unsupported` outcome — failure is a first-class result,
not an exception to guess around.

```python
class OperationalIntentInterpreter(Protocol):
    def interpret(self, transcript: str, ctx: IntentContext) -> OperationalIntent: ...
```

`OperationalIntent` is a **closed** discriminated union — `RECORD_SERVICE`, `SKIP_SERVICE`, or
`UNRESOLVED`, each with a fixed field set. It deliberately cannot express SQL, a table or column
name, arbitrary code, a free-form `action` string, or any payment, price, commission, or
configuration command. An unparseable utterance is `UNRESOLVED`, never a best guess.

`IntentContext` carries only what interpretation needs and nothing sensitive: the tenant's
`unit_label`, its timezone and today's business date, and the allowed-intent schema. It does **not**
carry the customer list, balances, prices, or any other customer data.

This is kept strictly separate from `SearchInterpreter` / `CustomerSearchFilter`: a read schema and
a write-candidate schema with different authority must not be one overloaded object. One vendor
(Groq) may implement both ports, but the two contracts, prompts, and validators stay distinct.

`MockSpeechToTextProvider` and a deterministic mock interpreter drive every automated test with no
network access. Automated tests never make a live provider call.

**Initial implementation.**

> **Amended in P4 (2026-09-03).** The owner has changed the initial STT
> implementation to **ElevenLabs `scribe_v2`**, with the server secret
> `ELEVENLABS_API_KEY`. The paragraphs below describe the *superseded* Groq
> choice and are kept because the reasoning — accuracy over latency, for
> utterances carrying names and quantities — is unchanged and is what the new
> selection was made against. Everything structural here still holds exactly:
> the port, the adapter boundary, the mock in tests, the mandatory confirmation
> step, and the button fallback. Groq may still back the *text* intent
> interpreters; it no longer backs transcription. See `docs/P4_HANDOVER.md` §
> "Owner decisions recorded during P4". Voice remains P9; nothing is implemented.

```
SpeechToTextProvider          ← the port the domain depends on
    ↓
ElevenLabsSpeechToTextAdapter ← app/adapters/speech/elevenlabs.py   (P9)
    ↓
scribe_v2                     ← SPEECH_MODEL, configuration only
```

The superseded initial choice, for the record:

```
GroqSpeechToTextAdapter       ← app/adapters/speech/groq.py
    ↓
whisper-large-v3              ← SPEECH_MODEL default, configuration only
```

Chosen because this product is accuracy-sensitive in exactly the way transcription usually is not
tested for: the utterances carry **customer names and quantities**, and a transcription error
becomes a wrong business-entry candidate. Larger-model accuracy is worth the latency here, and the
mandatory confirmation step (§8.3) remains regardless — it is a second line of defence, not a
licence to accept a weaker transcriber.

Selecting a provider does **not** collapse the abstraction. `SpeechToTextProvider` stays, because
the model is expected to be re-evaluated against real user speech (D12) and may be swapped for
`whisper-large-v3-turbo` or another provider entirely. The model identifier and every
vendor-specific API detail live in the adapter and configuration only — never in `voice/`,
`service/`, or any other domain module.

Groq therefore backs two ports — transcription and interpretation — but they remain **separate
contracts** with separate schemas, prompts, validators, and authority. One vendor behind two ports
is not one port.

### 8.6 Offline behaviour

The hard offline guarantee remains the **button** workflow, which is untouched.

| | Button daily entry | Text search | Voice |
| --- | --- | --- | --- |
| Online | works | works | works when configured |
| Offline | **works** (outbox) | works on snapshot | **not guaranteed** |

Voice needs the network for transcription and interpretation. No on-device speech model is added in
V1. When voice is unavailable — offline, unconfigured, provider down, or low confidence — the
microphone affordance simply reports that and the ordinary controls remain fully usable. Voice is
optional everywhere; nothing in the daily workflow requires it.

### 8.7 Accessibility and UX principles

Frozen as product principles, not decoration: the microphone action is easy to find; tap targets
are large; reading is minimal; the transcript and interpretation appear in one simple confirmation
card; no accounting jargon; errors are phrased as the next action to take ("Say the quantity" —
not "validation error"); voice is optional and never required; and the normal controls stay
available at all times.

---

## 9. Communication / GHL adapter contract

```python
class CommunicationProvider(Protocol):
    name: str
    capabilities: CommsCapabilities   # channels, supports_templates, supports_delivery_receipts

    def send(self, msg: OutboundMessage) -> DeliveryReceipt: ...
    def parse_delivery_callback(self, headers, raw_body) -> DeliveryUpdate | None: ...
```

`OutboundMessage` carries `tenant_id`, `customer_id`, `channel`, `to` (E.164), `template_key`,
`params` (already-rendered strings, including the formatted amount), and
`idempotency_key = reminder.id`.

**Our application decides** the customer, the current amount, the bill/reminder type, the due
state, reminder eligibility, and when future reminders stop. The provider decides only delivery.
GHL never receives write access to anything financial and never computes an amount — it is handed a
rendered value.

Failure isolation: delivery outcomes write to `communication_log` only. A send failure, a rate
limit, a template rejection, or a total GHL outage can never change a balance, a statement, a
payment, or a commission record. Failed reminders retry with backoff up to a bounded count and are
then surfaced to the owner.

`MockCommunicationProvider` is the development and test default; it records messages in memory and
can be told to fail, to be slow, or to return duplicate delivery callbacks.

---

## 10. Reminder architecture

Frozen schedule, stored as tenant configuration and defaulted to the requested one:

| Day of month | Kind | Recipients |
| --- | --- | --- |
| 1 | `STATEMENT` | every active customer with an issued statement |
| 4 | `REMINDER` | outstanding > 0 |
| 8 | `REMINDER` | outstanding > 0 |
| 12 | `REMINDER` | outstanding > 0 |
| 15 | `FINAL` + `OWNER_ALERT` | outstanding > 0 (the alert goes to the business owner) |

Execution: the host's cron calls `POST /internal/jobs/run-daily` with a shared secret. The job
resolves each tenant's local business date, guards on `job_run (tenant, kind, business_date)`, and
then, for each customer and open cycle:

1. Determines the **due stage**: `due_stage` = the highest configured schedule day ≤ today's
   tenant-local day of month, and `sent_stage` = the highest stage already successfully sent for
   that customer and cycle. If there is no due stage, or `due_stage ≤ sent_stage`, nothing is sent.
2. Recomputes the **current authoritative outstanding** from the ledger. The amount is never taken
   from a statement, a cache, or a previous reminder.
3. Applies the stage's eligibility rule. For `REMINDER` and `FINAL` stages, skips customers with
   outstanding ≤ 0 — fully paid customers stop receiving outstanding reminders immediately,
   including mid-cycle after a payment. The `STATEMENT` stage is **not** an outstanding reminder: it
   goes to every active customer with an issued statement, including those who owe nothing, because
   a statement is a bill and a record rather than a dunning notice.
4. Upserts **exactly one** `reminder` row, on `(tenant, customer, cycle, schedule_day)` for the due
   stage — re-running the job is a no-op, not a duplicate message.
5. Re-checks eligibility and re-reads the amount **at send time**, then dispatches through the
   communication port. A partial payment landing between generation and send lowers the amount
   actually sent.

**Catch-up after an outage (frozen).** Step 1 is what makes this correct across an outage, and it is
the ordinary path rather than a special case. A missed day cannot be re-run "for the same business
date" — once the host returns, the tenant-local business date has already moved on. Sending at most
one stage per run, always the latest due one, means an outage costs the customer the intermediate
nudges instead of delivering them all at once as a burst:

| Outage | Run resumes | Sent |
| --- | --- | --- |
| day 4 only | day 5 | the day-4 stage — one message |
| days 4–8 | day 9 | the day-8 stage **only** — not day 4 *and* day 8 |
| through day 15 | day 16 | `FINAL` + `OWNER_ALERT`, day 15 being the latest due stage |

Eligibility still wins: a customer whose outstanding is ≤ 0 receives no catch-up *reminder* at all,
however many stages elapsed — the day-1 statement is unaffected, per step 3. `job_run` idempotency is unchanged, so a re-run on the same business
date sends nothing further. All reminder decision logic lives in our application.

---

## 11. Commission architecture

Nothing about the commercial deal is hard-coded. The plan is data.

```
accepted business event      ──► commission engine ──► commission_event (terms snapshotted)
correction / void / reversal ──────────────────────► commission_adjustment (signed, traceable)
platform settlement          ──────────────────────► commission_settlement (independent, additive)
```

**Basis** is configuration — `RECORDED_VALUE` (charge accepted), `BILLED_VALUE` (statement issued),
`COLLECTED_VALUE` (payment accepted), or `PER_EVENT` (fixed amount per accepted service record).
The engine selects its trigger from the plan; adding a basis later does not touch billing code. The
monetary base for each is the corresponding **§11.1 derivation**, not an ad-hoc sum: in particular a
`COLLECTED_VALUE` plan follows the collections definition, so voiding a payment produces a negative
commission adjustment, while a `RECORDED_VALUE` plan is unaffected by that same void.

**Snapshotting.** Every `commission_event` copies the basis, rate, and fixed amount in force at the
time. A later plan change or renegotiated rate therefore cannot rewrite earned history.

**Corrections.** When a source document is corrected, voided, or reversed, the engine emits a
`commission_adjustment` linked to the original event, computed with the **original snapshotted
terms** — never re-derived from today's plan.

**Central acceptance.** A commission event is created inside the same database transaction that
accepts the source business event on the server. Offline devices never create commission.

**Authority.** All `commission:*` capabilities belong to the platform scope only. Tenant users have
neither read nor write access to commission plans, events, adjustments, or settlements, and no API
route exposes them to a tenant principal. Settlement is platform-only and strictly additive.

**Settlement (frozen).** `commission_settlement` is an independent, append-only record of money
settled. V1 does **not** allocate a settlement to individual earning events, and no event or
adjustment carries a settlement reference. Commission outstanding is a running aggregate:

```
commission_outstanding = Σ commission_event.commission_minor
                       + Σ commission_adjustment.amount_minor
                       − Σ commission_settlement.amount_minor
```

This is what makes **partial settlement** truthful. Earn 1000, settle 400, settle 600: outstanding
moves 1000 → 600 → 0 across three immutable rows, with no earning event modified, deleted, or
forced to carry two settlement references. A single nullable `settlement_id` on an event cannot
express a half-settled event or an event spanning two settlements, which is why it was removed.

If exact settlement-to-event allocation is ever required — per-event dispute or clawback — the
answer is a dedicated allocation table introduced at that point, not a column retrofitted now. The
platform dashboard needs only earned / adjustments / settled / outstanding, all of which the
aggregate already gives.

### 11.1 Reporting derivations (frozen, unambiguous)

The three groups from the brief are **not** interchangeable, and the line between them is the
adjustment *origin* rule from §5.3. Conflating them is the specific error this section exists to
prevent.

**A. Business generated / recorded service value** — what the business actually sold:

```
service_value = Σ CHARGE
              + Σ ADJUSTMENT WHERE source_type = 'daily_service_record'
```

Payment-origin adjustments are **excluded**. Worked example: a 1000 charge, a 500 payment, then that
payment voided. The void appends a payment-origin `ADJUSTMENT` of +500, so the customer's
outstanding correctly returns to 1000 — but business generated stays **1000, not 1500**. A reversed
payment is a collection event; it neither created nor destroyed service value.

**Billed value** — what was actually presented on issued bills:

```
billed_value = Σ over issued statements of (charges_minor + service_adjustments_minor)
```

Defined separately from service value, because service recorded in the currently open cycle is
generated but not yet billed, and because a late correction (§5.5) is billed in a later cycle than
the one it occurred in. `payment_reversals_minor` is excluded for the same reason as above.

**B. Collections** — money actually received and kept:

```
collected       = − ( Σ PAYMENT + Σ ADJUSTMENT WHERE source_type = 'payment' )
outstanding     = Σ all ledger entries            (FIN-4, unchanged)
collection_rate = collected ÷ billed_value
```

Here payment-origin adjustments *are* included, with the opposite effect: the same voided 500 takes
collected from 500 back to 0. Same ledger rows, opposite treatment, decided entirely by
`source_type`.

**C. Platform commercial position** — `earned + adjustments − settled = outstanding`, per the
settlement rule above.

Kept deliberately smaller than a general accounting system: no chart of accounts, no double-entry
journals, no period locking beyond cycle close.

---

## 12. AI authority boundary

Two AI-assisted interpreters exist, with **different schemas and different authority**. Neither can
mutate anything.

```
 SEARCH (read)
 text or voice transcript ──► SearchInterpreter ──► CustomerSearchFilter
                                    ▲              ──► strict validation ──► parameterised SQL
                               no DB access
                               no write capability

 OPERATIONAL (write candidate)
 voice transcript ──────────► OperationalIntentInterpreter ──► closed OperationalIntent
                                    ▲              ──► server resolution + validation
                               no DB access        ──► human CONFIRM ──► ordinary service command
                               no write capability
```

The operational path has three independent gates the model cannot open: a closed schema that
cannot express a forbidden command, deterministic server-side resolution and validation, and an
explicit human confirmation. The model's output is a *suggestion for a person to approve*; the
write is performed by the same domain command the buttons use (§8.2).

### 12.1 Search interpretation

Groq is called through a `SearchInterpreter` port and returns **only** a `CustomerSearchFilter`
object drawn from a closed schema: `area`, `status ∈ {PAID, PARTIALLY_PAID, UNPAID}`,
`outstanding_min_minor`, `outstanding_max_minor`, `name_contains`, `has_service_on`,
`no_service_since`, `sort`, `limit ≤ 200`. Unknown fields, unknown operators, and free-form SQL
fragments are rejected outright by Pydantic before any query is built. The query itself is ordinary
parameterised SQLAlchemy against tenant-scoped tables.

Prohibited by construction: the interpreter has no tool access, no write endpoint, and no ability
to name a table or column. It cannot touch daily records, prices, bills, payments, balances,
commission, settlements, or authoritative status.

### 12.2 Operational intent interpretation

The second interpreter is described in §8.5. Its output is a closed union of `RECORD_SERVICE`,
`SKIP_SERVICE`, and `UNRESOLVED` — a payment, price, commission, settlement, correction, void, or
configuration command is **not representable** in the schema, so it cannot be requested, filtered
for, or forgotten. Everything after interpretation is deterministic server code, and the write
itself needs a human tap.

### 12.3 Privacy and prompt-injection surface

Only the user's query or transcript and the static schema description are sent. The customer list,
balances, prices, phone numbers, and addresses are **not** sent — which is also why customer
resolution is a database operation rather than a model one (§8.3).

An utterance is user-supplied text reaching a model, so injection is possible in principle; the
defence is that the output schema is closed and validated, and that no interpreter output is ever
executed — the worst achievable outcome is a wrong candidate that a person then declines to
confirm. This must stay true: if a future feature ever feeds record content into a prompt, or acts
on an interpreter's output without confirmation, this boundary needs re-review (see R9).

### 12.4 Fallback

If Groq is unavailable, slow, or returns an invalid object, search responds
`{"interpreted": false}` and the UI falls back to ordinary structured filters; voice entry reports
that it could not understand and the ordinary `[-] qty [+] CONFIRM` controls remain fully usable.
Core functionality is unaffected. `NullSearchInterpreter` and the mock intent interpreter are the
defaults in tests, and both features sit behind config flags. **Button-based daily operations must
work with no AI configured at all.**

---

## 13. Configuration model

**Environment variables — secrets and deployment-specific values, never in source control:**
`DATABASE_URL`, `JWT_SECRET`, `INTERNAL_JOB_SECRET`,
`COMMS_PROVIDER` (default `mock`), `COMMS_PROVIDER_*` credentials,
`GROQ_API_KEY`, `GROQ_ENABLED`,
`SPEECH_PROVIDER` (`mock` | `groq`; `mock` in tests, `groq` in deployment),
`SPEECH_MODEL` (default `whisper-large-v3`), `VOICE_ENABLED` (default `false`),
`CORS_ORIGINS`, `ENVIRONMENT`, `LOG_LEVEL`.

One Groq credential serves both the speech adapter and the AI interpreters — `GROQ_API_KEY` is not
duplicated into a separate STT secret. The two remain distinct ports with distinct enable flags:
`GROQ_ENABLED` governs interpretation, `VOICE_ENABLED` governs the voice feature, and
`SPEECH_PROVIDER` selects the transcription adapter. `SPEECH_MODEL` is configuration; no model
string appears in domain logic.

There are no payment-provider variables: V1 has no online gateway.

Loaded once through `pydantic-settings`. A `.env.example` listing names with empty values will be
added in P1; `.env` is git-ignored. No secret ever appears in a document, a fixture, or a log line.

**Per-tenant business configuration — database rows, editable, audited:** business name, currency
and minor-unit exponent, unit label (`unit` by default; litres / bottles / whatever the business
actually sells), timezone, billing-cycle type and
start day, reminder schedule, default unit price, default quantity.

**Platform configuration — platform scope only:** commission plans, tenant provisioning, settlement
records.

**Never configurable, because they are correctness rules:** the money representation, the rounding
rule, the ledger sign convention, idempotency behaviour, the closed intent schema, and the
requirement that a voice mutation be confirmed by a person.

---

## 14. Deployment portability

One stateless container, one PostgreSQL database, one cron trigger. That is the entire production
topology, and it runs unchanged on Fly.io, Render, Railway, a plain VPS with Docker Compose, or any
managed container host. No provider-specific runtime API is used, so the hosting provider does
**not** need to be frozen in P0.

- Frontend: static build, served by the API container or any CDN/static host.
- Scheduling: whatever the host offers calls `POST /internal/jobs/run-daily`; `job_run` makes it
  idempotent, so a duplicated or retried trigger is harmless.
- Migrations: Alembic, run as a release step.
- Free / low-cost launch: a free-tier managed Postgres plus one small always-on container is
  sufficient for a single business with a few hundred customers.
- Backups: nightly managed Postgres backup plus retained WAL. This is the one piece of
  infrastructure that must not be skipped — the ledger *is* the product.

---

## 15. API surface (shape only)

`/api/v1` — versioned from day one.

```
POST   /auth/login                     POST /auth/refresh          POST /auth/logout
GET    /customers                      POST /customers             GET/PATCH /customers/{id}
GET    /customers/{id}/history         GET  /customers/{id}/statements
POST   /service/records                POST /service/records/{id}/correct
                                       POST /service/records/{id}/void
GET    /service/day/{date}             (the daily register queue)
GET    /billing/cycles                 POST /billing/cycles/{id}/close
GET    /statements/{id}
POST   /payments                       POST /payments/{id}/void      (manual only)
GET    /reminders                      POST /reminders/{id}/send
GET    /dashboard/summary              GET  /dashboard/outstanding
POST   /search/customers  (structured) POST /search/interpret      (AI, optional)
POST   /voice/transcribe               POST /voice/interpret       (AI, optional; READ-ONLY)
POST   /sync/operations                GET  /sync/changes
POST   /internal/jobs/run-daily        (shared-secret)
--- platform scope only ---
GET    /platform/commission/summary    GET/POST /platform/commission/plans
POST   /platform/commission/settlements                GET /platform/tenants
```

Every mutating route accepts an `operation_id`, whether the caller is online or syncing. Errors use
one machine-readable envelope: `{"error": {"code", "detail", "field_errors"}}`.

Note what is **absent**, deliberately: there is no online-payment intent or provider-callback route,
and there is no voice write route. `/voice/transcribe` and `/voice/interpret` are read-only — they
return a transcript and a resolved candidate. A confirmed voice entry posts to `POST /service/records`
like any other, which is what makes §8.2 structural rather than a promise.

---

## 16. Explicit deferred decisions

Deferred on purpose. Each is isolated behind a port, a config value, or a data row, so none of them
blocks P1–P8.

| # | Open question | Owner | Isolated behind | Latest safe answer point |
| --- | --- | --- | --- | --- |
| D1 | GHL access, exact workflow / API / webhook arrangement | coordinator | `CommunicationProvider` port | before P10 |
| D2 | WhatsApp business number, Meta verification, approved templates | coordinator | `template_key` + params | before P10 |
| D3 | Final commission rate | platform owner | `commission_plan.rate_bp` | before first settlement |
| D4 | Final commission basis (recorded / billed / collected / per event) | platform owner | `commission_plan.basis` | before P3 runs on real data |
| D5 | Commission settlement schedule | platform owner | `commission_settlement` rows | before first settlement |
| D6 | Production hosting provider | us | portable container topology | before P12 |
| D7 | Non-monthly billing cycles (weekly / fortnightly) | client | `tenant.cycle_type` | only if requested |
| D8 | Restricted operator role | client | reserved role, empty capability set | a future package |
| D9 | Customer self-service portal / login | client | out of scope in V1 | not planned |
| D10 | Multi-currency per tenant | platform owner | currency on `tenant` | only if a second market appears |
| D11 | The client's real business unit (litres? bottles? service units?) | client | `tenant.unit_label`, defaulted to the generic `unit` | before first production data entry |
| D12 | Whether the selected STT model (**ElevenLabs `scribe_v2` as amended in P4**; formerly Groq `whisper-large-v3`) is good enough in production against representative real utterances — English, Urdu, Roman Urdu intent after transcription, Urdu-English code-switching, Pakistani accents, customer names, numbers and quantities, and realistic background noise — at acceptable cost and latency | us + client | `SpeechToTextProvider` port and `SPEECH_MODEL`; voice is optional and degradable | during P9 evaluation, before voice is promoted to a primary path |
| D13 | Whether transcripts may be persisted (analytics, dispute resolution) | client + us | currently ephemeral-only by rule (§8.4) | needs an explicit product and privacy decision before any change |

**Closed since the first freeze:** the speech-to-text *provider* question is settled — as
**amended in P4**, ElevenLabs `scribe_v2` is the initial implementation, superseding Groq
`whisper-large-v3` (§8.5). What remains open is D12, whether its
real-world quality, cost, and latency are acceptable. If they are not, the response is to change
the model or the adapter behind the unchanged port — **not** to redesign the voice or domain
architecture. Voice stays optional and the buttons stay the guaranteed fallback either way.

Online payments are out of scope, so the former payment-provider
questions (domestic vs international, provider selection, an existing merchant gateway, KYC and
credentials, and provider verification capability) are no longer open — they are simply not part of
V1. If online payments return, they arrive as a new provider port and a new set of decisions, with
the manual payment ledger untouched underneath.

Assumed defaults until answered — recorded so they are not mistaken for decisions: currency PKR
with exponent 2, timezone `Asia/Karachi`, monthly calendar billing cycle starting on the 1st, unit
label **`unit`** (a deliberately generic placeholder — we do not know the client's real business
unit; see D11), reminder days 1 / 4 / 8 / 12 / 15, a mock communication provider, `SPEECH_PROVIDER`
`mock` in tests and `groq` with `whisper-large-v3` in deployment, and `VOICE_ENABLED` false until
the D12 evaluation passes.
