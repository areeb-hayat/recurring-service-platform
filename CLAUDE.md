# CLAUDE.md — Operating Rules

Recurring Service, Billing & Collection Platform.
`README.md` is the product brief. `docs/P0_ARCHITECTURE_FREEZE.md` is the frozen architecture.
`docs/P0_INVARIANTS_AND_ACCEPTANCE.md` is the contract implementation must satisfy.

Read those before changing anything financial, and prefer the simplest implementation that
satisfies them.

## Current phase

P0 (architecture freeze) and P1 (backend & data foundation) are complete — see
`docs/P1_HANDOVER.md`. The backend lives in `backend/`; there is **no frontend yet**. The next
package is P2 (financial engine: billing cycles, statements, payment ledger, carry-forward).

Do not skip ahead: no UI, no offline sync endpoint, no reminders, no commission engine, no AI and
no voice before their package. P1 deliberately contains no adapter and makes no network call.

Run the backend tests with a real PostgreSQL — never SQLite:

    cd backend
    docker compose -f docker-compose.test.yml up -d
    export TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:55432/rsp_test
    pytest

## Frozen stack

Backend Python 3.12 / FastAPI / SQLAlchemy 2.x / Alembic / PostgreSQL.
Frontend React + TypeScript + Vite, PWA with Service Worker + IndexedDB.
Tests pytest (backend), Vitest + Testing Library (frontend), Playwright (E2E).
No microservices, no Redis, no Kafka, no Kubernetes, no Celery. Scheduled work runs through an
authenticated internal job endpoint driven by the host's cron.

## Non-negotiable invariants

**Money.** Integer minor units (`BIGINT`) everywhere — DB, domain, API JSON. Never `float`, never
JS `number` arithmetic on money in the client. Quantity is `NUMERIC(12,3)` / `Decimal`, never
float. One rounding point only: `charge_minor = ROUND_HALF_UP(quantity * unit_price_minor)` at the
daily record. Statements sum already-rounded values; they never re-round.

**Server is authoritative.** The client never computes a balance, a charge, a due state, or a
payment status that anyone relies on. It renders what the server returned.

**Ledger is the single source of balance.** `ledger_entry` is append-only and never updated or
deleted. Outstanding is always `SUM(amount_minor)` over the customer's entries. Voids and
corrections append compensating entries; they never mutate an existing entry.

**Adjustment origin decides the report.** An `ADJUSTMENT` inherits the `source_type` of what it
compensates. Service-origin moves business generated; payment-origin moves collections. Never sum
`ADJUSTMENT` entries without filtering by origin — a voided payment must not inflate business
generated. See §11.1 of the architecture freeze.

**History is not destructive.** Accepted financial rows have no hard-delete path. Use
CORRECT / VOID / REVERSE, and always record original value, resulting adjustment, reason, actor,
timestamp, and source.

**Idempotency.** Every write carries a client-generated `operation_id`, generated once at user
intent and never regenerated on retry. Replaying it returns the same logical result (not a
byte-identical serialization) and creates nothing new. The `sync_operation` register is **never pruned** —
do not add a retention or archival job; a retention horizon is a duplication horizon. Never rely on
"the request probably didn't land".

**Unresolved sync outcomes persist.** `REJECTED` and `CONFLICT` operations move out of `outbox` and
into the durable `issues` store — never into nothing. Needs Attention must survive a browser
restart, and a conflicting operation is never auto-resubmitted unchanged.

**Payments are manual in V1.** No online gateway, no provider, no callbacks — `CASH`,
`BANK_TRANSFER`, `OTHER`, recorded by the owner. Do not add a payment provider, a `payment_attempt`
table, or a callback route; if online payments return, they arrive as a new port over the unchanged
ledger. Duplicate protection is `operation_id` alone, because two equal same-day cash payments are
legal.

**Voice is an input method, never an authority.** Speech and model output produce a *candidate*
intent that a person must confirm; the write then goes through the same service command the buttons
call. There is no voice write endpoint — keep it that way. Voice can only record or skip service:
payments, prices, commission, corrections, voids, and configuration are not representable in the
intent schema. Never invent a customer, quantity, or date; fail closed to the ordinary UI. Never
persist raw audio, and keep transcripts ephemeral.

**Tenancy.** Every business row carries `tenant_id`. Cross-tenant references are blocked by
composite foreign keys `(tenant_id, id)`, not by application care alone. Every query is scoped by
the principal's tenant.

**Platform commission is protected.** Tenant users have no read and no write access to commission
plans, events, adjustments, or settlements. Only the platform scope does.

**AI is never authoritative.** The interpreters may only produce a validated filter object (read)
or a closed candidate intent (write-suggestion). Neither can write, and the product — including
button-based daily operations — must work fully with AI disabled.

**Providers are replaceable.** No GHL, Groq, or speech-vendor identifier — and no model string like
`whisper-large-v3` — may appear outside `app/adapters/` and configuration. Domain modules import
ports, never adapters. The ports are `CommunicationProvider`, `SpeechToTextProvider`,
`SearchInterpreter`, and `OperationalIntentInterpreter`.

**Speech-to-text: Groq `whisper-large-v3` is the frozen initial implementation** (`SPEECH_PROVIDER`,
`SPEECH_MODEL`), selected for accuracy because utterances carry names and quantities. That froze an
implementation, not the port — the model is expected to be re-evaluated against real speech and may
be swapped. Tests always run on `MockSpeechToTextProvider`; never make a live provider call in a
test. One `GROQ_API_KEY` serves both the speech adapter and the interpreters, but they stay separate
ports with separate contracts.

## Code boundaries

```
app/core       money, ids, time, config, security primitives
app/<domain>   tenancy identity customers service billing payments
               reminders commission sync search voice
app/ports      CommunicationProvider, SpeechToTextProvider,
               SearchInterpreter, OperationalIntentInterpreter (Protocols)
app/adapters   comms/ speech/ ai/ — mock + real implementations
app/api        HTTP routers (thin: auth, validate, call domain, serialize)
app/jobs       daily job entrypoints
```

Domain → ports: allowed. Domain → adapters: forbidden. API → domain: allowed. Domain → API:
forbidden.

## Working rules

- Do not add a dependency, a table, or a background service without a concrete requirement in
  `README.md` or a P-package brief. Say what forced it.
- Do not write migrations for a model that has no test.
- Never commit secrets. Config comes from environment variables; per-business settings
  (currency, unit label, timezone, cycle, reminder days) live in the `tenant` row.
- Timezone-sensitive logic uses the tenant's timezone for business dates and UTC for instants.
  A "service date" is a date, not a timestamp.
- When a requirement is genuinely undecided (see the deferred list in `docs/P0_HANDOVER.md`),
  keep it behind a port and a mock. Do not guess and hard-code.
- Do not commit unless asked.
