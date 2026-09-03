# CLAUDE.md — Operating Rules

Recurring Service, Billing & Collection Platform.
`README.md` is the product brief. `docs/P0_ARCHITECTURE_FREEZE.md` is the frozen architecture.
`docs/P0_INVARIANTS_AND_ACCEPTANCE.md` is the contract implementation must satisfy.

Read those before changing anything financial, and prefer the simplest implementation that
satisfies them.

## Current phase

P0 (architecture freeze), P1 (backend & data foundation), P2 (financial engine), P3
(commercial tracking / commission), P4 (customer & daily UI), P5 (offline & sync), P6
(owner financial dashboard & operating costs), P7 (reminder engine) and P8 (smart
search & customer identification) are complete —
see `docs/P1_HANDOVER.md` through `docs/P8_HANDOVER.md`. The backend lives in `backend/`
and the frontend in `frontend/`.
P2 added billing cycles, posting-cycle resolution, immutable statements, manual payments with
void, the derived payment status and the §11.1 reporting derivations. P3 added the four
commission tables, the four earning bases, snapshotted terms, signed adjustments, aggregate
settlement and the platform-only commission surface. P4 added the first frontend: login, the
authenticated shell, customer list/create/view/edit and the Daily Register.

**P5 made the app offline-first.** A Workbox Service Worker caches the app shell; four IndexedDB
stores (`outbox`, `issues`, `snapshot`, `meta`) live in `frontend/src/sync/`; `POST /sync/operations`
and `GET /sync/changes` live in `backend/app/sync/`. The Daily Register now reads the snapshot and
writes through the outbox, so there is one write path online and off.

**V1's offline write guarantee is CONFIRM and SKIP only** (`service.record`, `service.skip`) — see
the dated clarification at P0 §7.2. Payments, corrections, voids, customer create/edit and every
operating-cost write stay online-only; the envelope is extensible, the scope is not. The Service
Worker caches **no API response** — business data offline comes from the snapshot, and a missing
snapshot says "Unavailable offline" rather than guessing.

**P6 built the owner's financial surface.** The dashboard (`/dashboard/summary`,
`/dashboard/outstanding`), the statement list, manual payment recording and reversal, the customer
financial view, and a new **Operating Costs** area — what the business pays its *providers*,
recorded against versioned rates with estimated-vs-actual variance. `payment` and `statement`
joined the sync feed (read-only; `SYNC_FEED_VERSION` is now **2**), and their op types joined
`FEED_WRITING_OP_TYPES` so they inherit the SYN-10 commit-order boundary.

**Operating costs are a third, separate accounting concept** — see the dated P0 §15a addition and
the COST invariants. `ledger_entry` is what a customer owes the business; `commission_*` is what
the business owes the platform; `operating_cost_*` is what the business owes its providers. They
are never summed, never share a table, and never share a capability (`cost:read` / `cost:write`
were added to `OWNER_ADMIN`). No provider price is hard-coded — every rate is a row.

**P7 built the reminder engine.** The frozen schedule (day 1 statement, days 4 / 8 / 12
reminders, day 15 final plus owner alert) lives on `tenant.reminder_schedule` and is read
through `app/reminders/schedule.py` — no schedule day is written down in business logic.
Three tables joined the schema (`reminder`, `communication_log`, `job_run`), completing the
set P0 §6 named. The host's cron calls `POST /internal/jobs/run-daily` with a shared secret;
the runner iterates every active tenant on that tenant's own business date.

**Catch-up is by stage, never by replaying missed dates.** Each run sends at most **one**
customer-facing stage — the highest configured day ≤ today — so an outage costs the
intermediate nudges instead of delivering them all at once. The amount is always the
current authoritative outstanding recomputed at send time; a partial payment lowers it and
a full payment cancels the stage. The `reminder` unique key
`(tenant, customer, cycle, schedule_day, kind)` is the correctness guarantee; `job_run` is
only a same-day short-circuit. A reminder chases the customer's **latest issued statement**,
so no statement means no reminder rather than a fabricated amount.

**P7 declares the first port, `CommunicationProvider`, and the first adapter.** The
application decides who, which stage, how much and whether to suppress; the provider only
delivers, and it is handed an *already-rendered* amount string — `OutboundMessage` rejects a
non-string param or any key ending in `_minor`. `MockCommunicationProvider` is the only
implementation and makes no network call. Reminders are **server-only**: no reminder write
enters the P5 outbox, no reminder table carries `row_version`, and `SYNC_FEED_VERSION` stays
**2**.

**P8 made the product able to find a person.** `customer_alias` records the names a
customer is actually called; `app/search/` holds one normalization path, one tenant-scoped
ranked query (`POST /search/customers`) and one channel-independent resolver
(`POST /search/customers/resolve`). Aliases travel inside the customer payload rather than
as a sync entity — an alias write bumps the *customer's* `row_version` — and
`SYNC_FEED_VERSION` is now **3** so devices re-seed rows written before `aliases` existed.
Alias writes are online-only, exactly as customer create and edit are; offline CONFIRM and
SKIP are unchanged.

**Customer identity is never guessed.** `resolve_customer` answers RESOLVED (one
authoritative id), AMBIGUOUS (candidates for a person to choose between) or NOT_FOUND, and
nothing weak — a prefix, a substring, an area or a fuzzy match — ever resolves, however far
ahead it ranks. Two customers matching equally is a question, never a coin toss dressed up
as a ranking. That resolver is the contract P9 voice and P10 text channels reuse: there is
deliberately no per-channel matching code for them to grow. Matching is PostgreSQL and
application logic only (`pg_trgm` for indexes and typo-tolerant *candidates*) — no
Elasticsearch, no vector database, no external search service — and no model generates,
suggests or interprets anything.

Do not skip ahead: no AI and no voice before their package, and **no real messaging transport
before P10** — no n8n, Evolution, WhatsApp, Meta Cloud API, SMS gateway, GSM modem or Android
relay. `Channel.SMS` exists as a port value only; P10 may add SMS as a second channel on the
unchanged port, and connects real messaging usage to P6's operating costs. P1–P7 make no
network call to any provider — P6 *records* provider expenses, and P7 *records* what to send.

Run the backend tests with a real PostgreSQL — never SQLite:

    cd backend
    docker compose -f docker-compose.test.yml up -d
    export TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:55432/rsp_test
    pytest

Run the frontend tests, typecheck and build from `frontend/`:

    cd frontend
    npm install
    npm test            # vitest
    npm run typecheck   # tsc --noEmit
    npm run build       # typecheck, then vite build
    npm run e2e         # playwright, against the production build in dist/

The Playwright suite needs `npm run build` to have run (it tests the real Service Worker) and
`npx playwright install chromium` once.

## Collaboration

Two developers, two worktrees, one repository. Yahya's development worktree is
`E:\Recurring-Service-Platform-yahya` on branch `yahya`, pushing to `origin/yahya`. Never push
Yahya development directly to `origin/main`, and never modify Areeb's worktree or branch from
this one. Integration into `main` is a separate, deliberate action — not a side effect of
finishing a package.

## Production accounts

There is **no shared generic privileged login**, and none is to be created. Each person holds
their own identity so every audit event stays attributable:

- the client / business owner has their own `OWNER_ADMIN` account;
- Yahya has their own `PLATFORM_OWNER` identity;
- Areeb has their own `PLATFORM_OWNER` identity.

Equal platform capability, separate identities. Never hard-code a real production user or
password into source, a migration, a seed script or a test fixture.

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

**Operating costs are not commission and not the customer ledger.** `operating_cost_*` records
what the business pays its providers. It never posts a ledger entry, never moves a customer's
outstanding balance, and never touches a commission row — and the reverse holds too. Provider rates
are versioned data with non-overlapping effective ranges; a recorded month snapshots the terms it
used, so a later rate change never restates it. Money stays integer minor units, usage stays
`Decimal`, and amounts stay in the provider's own currency — there is no FX feature and totals are
per currency. No usage means no estimate and no invoice means no actual: never a zero.

**Reminders decide nothing outside this application.** The schedule, the eligibility, the
amount and the suppression are the server's; a delivery provider only delivers, and receives a
rendered string rather than a balance or a rule. A run sends at most one customer-facing stage
per customer per cycle — always the latest due one — so an outage can never produce a burst.
The amount is the current authoritative outstanding read at send time, never a statement total
and never a previous reminder's figure; outstanding ≤ 0 stops every further outstanding
reminder in that cycle. A communication failure writes only to `communication_log` and the
reminder's own state: it can never move a balance, a statement, a payment or a commission row.
Reminder history has no delete path. Reminder generation and delivery are **server-only** — no
reminder operation belongs in the offline outbox.

**Platform commission is protected.** Tenant users have no read and no write access to commission
plans, events, adjustments, or settlements. Only the platform scope does. Commission is earned by
the server inside the transaction that accepts the source business event, never by a client; every
`commission_event` snapshots the plan terms in force, and a correction, void or reversal appends a
signed `commission_adjustment` computed with those **original** terms. Settlement is additive and
allocates to nothing: `earned + adjustments − settled = outstanding`. Do not add a `settlement_id`
column or a settlement-allocation table.

**AI is never authoritative.** The interpreters may only produce a validated filter object (read)
or a closed candidate intent (write-suggestion). Neither can write, and the product — including
button-based daily operations — must work fully with AI disabled.

**Providers are replaceable.** No GHL, ElevenLabs, Groq or other vendor identifier — and no model
string like `scribe_v2` — may appear outside `app/adapters/` and configuration. Domain modules import
ports, never adapters. The ports are `CommunicationProvider`, `SpeechToTextProvider`,
`SearchInterpreter`, and `OperationalIntentInterpreter`.

**Speech-to-text: ElevenLabs `scribe_v2` is the initial implementation** (`SPEECH_PROVIDER`,
`SPEECH_MODEL`, secret `ELEVENLABS_API_KEY`), selected for accuracy because utterances carry names
and quantities. This supersedes the earlier Groq `whisper-large-v3` choice (P0 §8.5, amended in P4).
It froze an implementation, not the port — the model is expected to be re-evaluated against real
speech and may be swapped again. Groq may still be used later for *constrained text intent
interpretation*; it is no longer the transcriber. Tests always run on `MockSpeechToTextProvider`;
never make a live provider call in a test. Raw audio is never persisted and transcripts stay
ephemeral. **Voice is P9 — do not implement any of this earlier**, and the button workflow stays
authoritative and always available whatever happens to voice.

## Code boundaries

```
app/core       money, ids, time, config, security primitives
app/<domain>   tenancy identity customers service billing payments
               commission costs reminders sync search voice
               (app/search: normalize + filters + query + resolver)
app/ports      CommunicationProvider (P7), SpeechToTextProvider,
               SearchInterpreter, OperationalIntentInterpreter (Protocols)
app/adapters   comms/ (mock only) — speech/ and ai/ belong to P9 and P8
app/api        HTTP routers (thin: auth, validate, call domain, serialize)
app/jobs       daily job entrypoints
```

```
frontend/src/api        typed HTTP boundary, error envelope, operation envelope
frontend/src/auth       session storage, AuthContext, login screen
frontend/src/components shell, auth gate, feedback, quantity stepper
frontend/src/customers  list, create, detail/edit, the financial view
frontend/src/daily      the Daily Register
frontend/src/dashboard  the owner overview
frontend/src/statements issued statements: list and detail
frontend/src/payments   manual payment recording (online only)
frontend/src/costs      operating costs: rates, usage, invoices, scenarios
frontend/src/reminders  where each customer stands in the schedule (online only)
frontend/src/search     the search box, candidate rows, and the offline mirror
frontend/src/lib        exact quantity arithmetic, money display/parsing, uuidv7
frontend/src/sync       IndexedDB stores, the sync engine, sync status, Needs Attention
frontend/e2e            Playwright acceptance suite and its fixture server
```

The frontend renders what the server returned. It never computes a charge, a balance, a due
state, a payment status or a commission figure, and it never sends a `tenant_id` — the bearer
token decides the scope. Quantity arithmetic goes through `lib/decimal.ts` on scaled integers,
never through a JS `number`.

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
