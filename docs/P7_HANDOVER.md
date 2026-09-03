# P7 — Handover

Package: **P7 — Reminder Engine**. The business now chases its own money on a
schedule, and every decision about who, when and how much is made on the server.
Nothing committed, nothing pushed.

**Base commit:** `b93692e` — "Implement P6 owner financial dashboard and operating costs".
**Worktree:** `E:\Recurring-Service-Platform-yahya` · **branch:** `yahya` · **upstream:**
`origin/yahya`. Working tree was clean at start.

---

## 1. Scope implemented

The reminder *decision* engine, its persistence, its scheduled runner, the
provider-neutral delivery boundary, and one owner screen.

```
app/ports/comms.py          the CommunicationProvider port (P0 §9)
app/adapters/comms/         MockCommunicationProvider + the one factory
app/reminders/schedule.py   the stage configuration and the catch-up rule
app/reminders/engine.py     eligibility, the current amount, generation, dispatch
app/reminders/runner.py     the job_run guard and the per-tenant round
app/reminders/reporting.py  the owner's work list and one reminder's attempts
app/jobs/daily.py           what the cron executes
frontend/src/reminders/     /reminders
```

**Deliberately not built** (out of scope, and no placeholder exists for any of
them): n8n, Evolution API, WhatsApp Cloud/Meta, any SMS gateway, modem or relay,
voice, speech, AI intent, aliases or smart search, delivery callbacks, deployment,
a platform-owner frontend, and automatic provider-invoice ingestion. `app/` still
imports **no HTTP client at all** — `tests/test_architecture.py` asserts it.

---

## 2. The schedule, and why it is data

The frozen default (P0 §10, REM-1), stored on `tenant.reminder_schedule` since P1:

| Day of month | Kind | Goes to |
| --- | --- | --- |
| 1 | `STATEMENT` | every active customer **with an issued statement**, owing or not |
| 4 | `REMINDER` | outstanding > 0 |
| 8 | `REMINDER` | outstanding > 0 |
| 12 | `REMINDER` | outstanding > 0 |
| 15 | `FINAL` + `OWNER_ALERT` | outstanding > 0 (the alert goes to the owner) |

`app/reminders/schedule.py` is the only module that knows what a stage is, and
the days themselves live in the tenant row. `tests/test_reminders.py` asserts by
source scan that **no schedule day appears anywhere in `app/reminders/`**, and a
separate test drives a tenant configured `2 / 20` end to end. Making the schedule
editable later is a write path onto a column that already exists; P7 deliberately
does not build one, and there is no per-customer override.

A malformed schedule raises rather than falling back to the default: reminding on
days the owner did not configure is worse than not reminding, and a silent
fallback would hide the mistake for a month. Days are bounded 1..28 for the same
reason `cycle_start_day` is — a stage on the 31st does not occur in February.

`OWNER_ALERT` is **not** a configurable stage. P0 §10 pairs it with `FINAL`, so it
is derived from the final stage and cannot drift away from it.

---

## 3. Eligibility and the authoritative amount

**Which cycle a reminder chases.** The customer's *most recently issued
statement*, and that statement's `cycle_id`. A customer with no issued statement
has no reminder cycle and receives nothing — the "fail safely rather than remind
from fabricated data" rule, made concrete rather than promised. It also gives the
monthly reset for free: the next cycle close issues the next statement, which is
a new `cycle_id`, whose `sent_stage` starts at zero.

**Which amount goes out.** Always `outstanding_minor()` — `SUM(amount_minor)` over
the customer's ledger — recomputed **at send time** (REM-2). Never
`statement.closing_balance_minor`, never a cache, never an earlier reminder's
`amount_minor_at_generation`, never anything a client sent.

Generation and dispatch are two separate functions precisely so this is testable:

```
generate_due_reminder()   decides whether a stage exists, and creates it
dispatch_reminder()       re-reads the balance, then delivers
```

* **Partial payment** — generated at 1000.00, 400.00 paid, the delivered string is
  `PKR 600.00` (REM-3, A-REM-2/3). `amount_minor_at_generation` keeps the old
  figure so a person can see *why* the two differ; it is never what is sent.
* **Full payment** — the stage is `CANCELLED` at dispatch and nothing goes out;
  later stages produce no reminder at all, and no owner alert (REM-4, A-REM-4).
* **Overpayment / credit** — a negative balance is money the business holds, not
  money it chases. No reminder.
* **A voided payment** puts the balance back, and reminders resume from the next
  due stage. Eligibility follows the ledger wherever the ledger goes.
* **The `STATEMENT` stage is exempt.** A statement is a bill and a record, not a
  dunning notice, so it goes to a customer who owes nothing (P0 §10 step 3).

---

## 4. Catch-up after an outage

One function — `due_stage(schedule, day_of_month)` — returns the highest
configured day ≤ today. That is the whole rule (REM-8), and it is the *ordinary*
path rather than a special case, which is exactly why an outage cannot produce a
burst: a missed day cannot be re-run "for its own business date", because by the
time the host returns the tenant-local date has already moved on.

| Outage | Runs on | Sent |
| --- | --- | --- |
| day 4 | day 5 | the day-4 stage, alone |
| days 4–8 | day 9 or 10 | the day-8 stage **only** — day 4 is not replayed |
| through day 15 | day 16 | `FINAL` + `OWNER_ALERT`, and no earlier stage |
| days 4–8, paid in full on day 6 | day 9 | nothing at all |

**At most one customer-facing stage per customer, per cycle, per run.** Asserted
directly, across all 28 days of a month, in
`test_at_most_one_customer_stage_per_run`. A full month of runs produces exactly
the five configured stages plus the one owner alert.

`sent_stage` counts `SENT` only. A `FAILED` stage was not sent, and treating it as
sent would silently swallow a stage the customer never received. Owner alerts are
excluded from `sent_stage` — alerting the owner must not advance the *customer*
through the schedule.

---

## 5. Idempotency and concurrency

Two guarantees, and it matters which is which.

**The stage index is the correctness guarantee.**
`uq_reminder_tenant_id_customer_id_cycle_id_schedule_day_kind` admits exactly one
row per stage however the processes interleave. Generation has no pre-read to
race with: it inserts, and on conflict reloads the winner — the same technique
`ensure_open_cycle` uses. Two runners on two connections produce one row
(`test_concurrent_generation_produces_exactly_one_stage`), and a direct SQL
duplicate is refused by name.

**`job_run (tenant_id, kind, business_date)` is a short-circuit, not the
guarantee.** Three runs on one business date do the work once and send one message
(A-REM-5). Only a `SUCCEEDED` row short-circuits: a `FAILED` or still-`RUNNING`
row is re-claimed, because a process killed mid-round leaves `RUNNING` forever and
refusing to re-enter would silence that tenant for the rest of the day over a row
nobody will ever finish. Re-entering is safe because the index — not the guard —
is what prevents a second message. That is why there is no lease, no heartbeat and
no stale-run sweeper: they would protect something already protected.

**Commit granularity is per customer.** A crash halfway through a round leaves the
customers already handled durably handled and the rest untouched; the retry
resumes rather than restarting, and could not double-send even if it did.

**Manual re-dispatch** goes through the ordinary `execute_idempotent` register
under `op_type = "reminder.send"`, so a lost response replays (`DUPLICATE`, one
extra provider call, not two) while a *deliberate* second attempt uses a new
`operation_id`. That is the honest distinction between "did my click land?" and
"send it again".

---

## 6. The communication boundary

`app/ports/comms.py` — the first of P0's four ports to be declared, because P7 is
the first package with something real to deliver.

```python
class CommunicationProvider(Protocol):
    name: str
    capabilities: CommsCapabilities
    def send(self, message: OutboundMessage) -> DeliveryReceipt: ...
    def parse_delivery_callback(self, headers, raw_body) -> DeliveryUpdate | None: ...
```

`OutboundMessage` carries `tenant_id`, `customer_id`, `channel`
(`WHATSAPP | SMS | EMAIL`), `to`, a **semantic** `template_key`, already-rendered
string `params`, `idempotency_key = reminder.id`, and a `reference` used for
tracing and nothing else. There is no WhatsApp field, no template body, no vendor
message shape and no channel-specific option anywhere in these types.

**REM-7 is enforced by the boundary, not by review.** `OutboundMessage.__post_init__`
rejects a non-string param value and any key ending in `_minor`, so a raw
minor-unit balance cannot cross. Amounts are rendered server-side by
`app.core.money.format_minor` — the provider receives `"PKR 1,000.00"`, never
`300000` and never an exponent to apply.

Template keys, all four of them: `statement.issued`, `payment.reminder`,
`payment.reminder.final`, `owner.final_alert`.

`MockCommunicationProvider` is the only implementation, and the test default. It
records messages in memory, makes no network call, and can be told to fail or to
raise — which is what makes the outage tests honest code paths rather than
patches. `COMMS_PROVIDER` selects by name and an unknown name **fails at startup**
rather than falling back to the mock: a deployment that believes it is sending
real messages and is not would be the worst failure a dunning system could have.

Destination: WhatsApp where the customer has one, ordinary phone otherwise. A
customer with neither is a *visible delivery failure* — `FAILED` with "no phone or
WhatsApp number on file" — never a silent skip and never an invented address.

---

## 7. Delivery failure semantics

| Case | Result |
| --- | --- |
| Not eligible | No stage row, no delivery, nothing recorded as failed |
| Eligible, provider refused | `reminder.state = FAILED`, `last_error` set, `communication_log` row `FAILED`, `sent_at` stays NULL |
| Eligible, provider raised | Identical — an outage is a delivery fact, not a crash; the run still reports `COMPLETED` |
| Eligible, accepted | `SENT`, `sent_at` set, `communication_log` `ACCEPTED` |

A failure is never quietly upgraded and never dropped. Retries are **bounded** at
`MAX_DELIVERY_ATTEMPTS = 3` (P0 §9), after which the stage stays `FAILED` and sits
in the owner's list where a person can look at it. A failed stage is retried on the
next run *while it is still the due stage*; once a later stage becomes due the old
one is left alone, which is what stops an outage becoming a burst when the provider
comes back.

**REM-6 is structural.** `app/reminders/` imports no payment command, no statement
writer and nothing under `app.commission`; there is no code path from here to a
balance. `test_A_REM_6_a_total_provider_outage_changes_no_financial_row` compares a
fingerprint of every ledger entry, statement, payment, commission event and
commission adjustment before and after a total outage and asserts identity.

---

## 8. The day-15 owner alert

A separate `reminder` row with `kind = OWNER_ALERT` at the final stage's day,
derived from the `FINAL` stage rather than configured. It inherits the final
stage's eligibility — a customer who paid gets neither — and gets its own
exactly-once guarantee from the same unique index.

It goes to the tenant's own `OWNER_ADMIN` by the email on their identity, which is
the only owner contact the data model holds (`app_user` has no phone). Each person
keeps their own account, so the alert reaches a named human and stays
attributable. It names the customer, the current outstanding as a rendered string,
and the cycle. It carries **no** commission, plan, settlement or platform figure —
asserted.

A failed alert is retried on a later run *without* re-sending the customer's final
notice, because the two have independent delivery lives.

### Recorded clarification of P0 §6

P0 §6 freezes the reminder key as `(tenant_id, customer_id, cycle_id,
schedule_day)`. P0 §10 puts **two** communications on day 15 — the customer's
`FINAL` and the owner's `OWNER_ALERT` — which that key cannot express. `kind`
therefore joined the key. REM-5's guarantee is untouched, because the schedule maps
each day to exactly one customer-facing kind; what changed is that the owner alert
now gets the same database-level exactly-once guarantee instead of relying on
application care.

---

## 9. Owner UI — `/reminders`

Where each customer stands in this month's schedule, largest balance first.

* The amount shown is the **live authoritative outstanding**, not the reminder's
  stored generation amount — those differ after a payment, and showing the stale
  one would contradict what the next reminder would actually say.
* Status (`DUE` / `WAITING` / `ATTENTION` / `SETTLED` / `NO_STATEMENT`) is derived
  **on the server** from the schedule and the ledger. The client filters on it; it
  never works out who is due.
* The schedule strip renders the days the response carried. Nothing in the
  frontend knows 1/4/8/12/15 — a Vitest case drives a `2 / 20` tenant.
* **There is no "send reminders" button.** A person pressing one is exactly how a
  schedule turns into a message flood. The single write is "Try sending again" on
  one failed delivery, which re-dispatches an existing stage and reports honestly
  when the server cancelled it instead because the customer has since paid.
* Filters: Needs action (default), Everyone, Settled.
* Mobile: the row stacks, name and amount stay readable at 360px, the retry
  control is a full tap target, and status colour is never the only signal — every
  badge spells its state out.
* **Online only**, and it says so offline rather than showing a stale stage.

`GET /reminders/{id}` returns the full attempt log — channel, provider, state,
error, and the already-rendered payload — so a failure is investigable and REM-7
is checkable after the fact.

---

## 10. Cron / runner

```
POST /api/v1/internal/jobs/run-daily      header: X-Job-Secret
```

One call, every active tenant, once a day. No Celery, no Redis, no Kafka, no queue
worker, no Kubernetes scheduler — the frozen stack, unchanged.

**The route takes no tenant, no date and no body**, asserted by inspecting the
route's own dependant: `query_params`, `path_params` and `body_params` are all
empty. That is what makes "the cron cannot be used as a tenant escape" structural
rather than a check — there is nowhere to point it. Each tenant gets its own
`SystemContext`, resolving *its own* business date from *its own* timezone (P0 R4),
so one 02:00 UTC trigger serves Karachi and Honolulu correctly.

The secret is compared with `hmac.compare_digest`. An unset `INTERNAL_JOB_SECRET`
returns **503 `JOB_ENDPOINT_DISABLED`**, never an open endpoint. A tenant bearer
token is not a job credential, and neither is a platform one — both 401. No secret
is committed anywhere; `.env.example` carries the name only.

One tenant raising does not stop the round: its failure is reported and the next
tenant is processed, because one misconfigured tenant must not silence everybody
else's reminders.

**New context type: `SystemContext`.** A third scope beside `TenantContext` and
`PlatformContext`, with `user_id = None`. The cron has no user, holds no token and
passes no capability check; inventing a service account would make its audit rows
indistinguishable from a person's.

---

## 11. Migration

One migration, `0005_p7_reminder_engine`, adding exactly three tables — the last
three P0 §6 named. With these, every table the architecture freeze specified
exists; a new table after this is a new decision rather than a deferred one.

* **`reminder`** — the stage register. Unique
  `(tenant_id, customer_id, cycle_id, schedule_day, kind)`; composite FKs to
  `customer` and `billing_cycle` (SEC-2); CHECKs binding `sent_at`/`cancelled_at` to
  the state, and `schedule_day` to 1..28.
* **`communication_log`** — one row per delivery attempt, with the rendered payload,
  the provider's message id and the error.
* **`job_run`** — unique `(tenant_id, kind, business_date)`.

**Nothing else in the schema moves.** No column is added to `ledger_entry`,
`payment`, `statement`, any `commission_*` or any `operating_cost_*` table — REM-6
expressed as an absence of foreign keys.

**No `row_version` on any of them**: none is a client sync entity, and a version
column would quietly make them syncable. `SYNC_FEED_VERSION` stays **2** and
`SYNC_ENTITIES` is unchanged.

**Deletes are blocked by trigger; updates are not.** Both tables carry a lifecycle
P0 §6 specifies (`PENDING → SENT/FAILED/CANCELLED`, `QUEUED → ACCEPTED → DELIVERED`),
so blocking UPDATE would forbid the transitions the freeze describes. What is
blocked is DELETE: "we reminded them and they still did not pay" is evidence
(AUD-1). No `btree_gist`, no EXCLUDE — a stage is a point, not a range.

Verified `head → 0004 → head`: the three tables and the trigger function
disappear on downgrade and come back on upgrade, with the stage index present.

---

## 12. Audit

`SYSTEM` / `JOB` for the runner, with `actor_user_id` NULL — the cron is not a
person, and no audit row claims it was one. `TENANT` / `ONLINE` with the real
`actor_user_id` for an owner's manual re-dispatch, reason "re-dispatched by the
owner". A reader can always tell the two apart.

Recorded: `reminder_run.completed` (one row per run), `reminder.generated`,
`reminder.sent`, `reminder.failed`, `reminder.cancelled`,
`reminder.owner_alerted`. **Not** recorded: anything per customer when nothing
happened — a quiet run leaves exactly one audit row, asserted.

Two new allow-list entries (`reminder`, `job_run`). No configuration-change action
was added, because P7 exposes no schedule write.

---

## 13. Offline boundary

Reminder generation and delivery are **server-only**. `reminder.send` is declared
in the frontend `OpType` union with an explicit online-only comment, exactly as the
payment and operating-cost types are; it never enters the outbox, and
`SUPPORTED_OP_TYPES` in `app/sync/envelope.py` is unchanged, so
`POST /sync/operations` refuses it. No reminder table has a `row_version`, so
nothing can stream into the snapshot by accident.

---

## 14. Tests

**Backend — 942 passed, one full run against a real PostgreSQL** (7m35s). 91 of
them are the new `tests/test_reminders.py`, plus 9 added to
`tests/test_tenant_isolation.py` (`TestP7ReminderIsolation`,
`TestInternalJobSurface`).

Covering, by number from the brief: day 1 / 4 / 8 / 12 / 15 due (1–5), fully paid
suppressed (6), partial payment uses the reduced balance (7), overpaid suppressed
(8), day-10 catch-up sends only day 8 (9), day-15/16 catch-up sends the final only
(10), at most one stage per run across all 28 days (11), duplicate runner
invocation (12), concurrent runners on two connections (13), a `SystemExit`
mid-round then a safe retry (14), delivery failure retained and never reported as
sent (15), the owner alert exactly once (16), tenant isolation (17), tenant-local
business date and two-timezone divergence (18), the amount derived from the ledger
(19), and an old statement amount unable to override the current balance (20).

Plus: the schedule as configuration and its rejection of malformed input, the
source guard that no schedule day is hard-coded, the bounded retry, the
no-contact failure, the port's own REM-7 enforcement, the `job_run` guard, the
disabled-endpoint 503, the delete-blocking triggers, the audit provenance split,
and the "next expected stage" derivation (defect 5 below).

**Frontend — 136 passed** (15 new in `src/reminders/reminders.test.tsx`),
`npm run typecheck` clean, `npm run build` clean.

Playwright was **not** run: P7 adds no browser-only guarantee — no Service Worker
behaviour, no IndexedDB store, no offline write path — that Vitest cannot prove
honestly. Re-running the P5 acceptance suite for ceremony would have proved
nothing new.

---

## 15. Defects and clarifications found

1. **P0 §6's reminder key cannot express P0 §10's day 15.** Resolved by adding
   `kind` to the unique key; see §8 above. Recorded rather than quietly patched.
2. **`communication_log` is not append-only** in the `ledger_entry` sense, and P0
   §6 already implies this by giving it a `QUEUED → ACCEPTED → DELIVERED` state. It
   is protected against DELETE instead, which is the guarantee that actually
   matters for reminder history.
3. **A stale `RUNNING` job_run would have locked a tenant out for a day.** The
   first design skipped on `RUNNING`; a process killed mid-round would then have
   silenced that tenant until midnight. Changed to re-claim anything that is not
   `SUCCEEDED`, which is safe because the stage index — not the guard — prevents a
   second message.
4. **The owner has no phone number in the data model.** `app_user` carries email
   only, so the day-15 alert goes by email. If the client wants it on WhatsApp,
   that is an owner-contact field and a decision, not an assumption to make here.
5. **"Next expected stage" was derived from the last stage sent alone**, which
   announced "next: day 1" on the 5th for a customer nothing had been sent to
   yet — a day already behind us. Fixed to take the later of what was sent and
   what today has already made due, with three tests.

---

## 16. Remaining risks

* **Nothing is actually delivered yet.** The mock records and discards. Until P10
  wires a real transport, "SENT" means "the mock accepted it".
* **The reminder cycle is the latest issued statement.** If the owner never closes
  a billing period, no statement is issued and no reminder is ever sent. That is
  the safe failure, but it is a failure that looks like silence — the
  `NO_STATEMENT` status on `/reminders` is the only thing that surfaces it, and a
  nudge to close the period may be worth adding.
* **Delivery receipts are declared but not received.** `DeliveryUpdate` and
  `parse_delivery_callback` exist on the port so P10 has somewhere to put one;
  there is no callback route, no parser and no adapter for one in P7.
* **The schedule is not editable through the product.** It is tenant data with no
  write path. If the client wants to change the days, that is a small P8+ screen,
  not a redesign.
* **Owner-alert routing is single-recipient.** The first active `OWNER_ADMIN` by
  creation order. A business with two owner-admins alerts one of them.

---

## 17. Deferred to later packages

* **P8** — structured search and aliases, the `SearchInterpreter` port. Nothing
  exists for it.
* **P9** — voice. `SpeechToTextProvider`, `OperationalIntentInterpreter`, the
  ElevenLabs `scribe_v2` adapter. `app/voice`, `app/speech` and `app/adapters/speech`
  are still asserted absent.
* **P10** — real messaging transport: WhatsApp first (n8n / Evolution / Cloud API,
  a coordinator decision), and **SMS as a second channel on this same port** where
  the business wants it. `Channel.SMS` already exists as a value; no gateway, modem
  or relay exists anywhere and none is implied. P10 also connects real messaging
  usage to P6's operating costs — P7 adds no vendor price and creates no invoice.
* **P11 / P12** — deployment, domain, the platform-owner frontend.

**Recommended next: P8 (search and aliases).** It is the last thing standing
between the daily register and a real round with a few hundred customers, it
touches no money, and it needs nothing from a vendor — so it can land while the
P10 provider arrangement is still being decided.

---

## 18. Git status

Nothing committed. Nothing pushed. Working tree carries the P7 changes only.

**New files**

```
backend/app/ports/comms.py
backend/app/adapters/__init__.py
backend/app/adapters/comms/__init__.py
backend/app/adapters/comms/mock.py
backend/app/reminders/__init__.py
backend/app/reminders/models.py
backend/app/reminders/schedule.py
backend/app/reminders/engine.py
backend/app/reminders/runner.py
backend/app/reminders/reporting.py
backend/app/jobs/__init__.py
backend/app/jobs/daily.py
backend/alembic/versions/0005_p7_reminder_engine.py
backend/tests/test_reminders.py
frontend/src/api/reminders.ts
frontend/src/reminders/RemindersPage.tsx
frontend/src/reminders/reminders.test.tsx
docs/P7_HANDOVER.md
```

**Modified**

```
backend/.env.example                      INTERNAL_JOB_SECRET / COMMS_PROVIDER now used
backend/app/core/config.py                the two settings above
backend/app/core/errors.py                JobEndpointDisabledError (503)
backend/app/core/money.py                 format_minor (REM-7's rendered string)
backend/app/db_models.py                  P7_TABLES
backend/app/main.py                       reminder_router, internal_job_router
backend/app/api/deps.py                   provider dependency, job-secret auth
backend/app/api/routes.py                 three reminder routes + the cron route
backend/app/api/schemas.py                SendReminderRequest
backend/app/audit/models.py               seven reminder actions
backend/app/audit/service.py              record_system_event, two allow-lists
backend/app/ports/__init__.py             re-exports the comms port
backend/app/tenancy/context.py            SystemContext
backend/tests/conftest.py                 comms + job_headers fixtures
backend/tests/test_architecture.py        adapters/comms admitted; guards tightened
backend/tests/test_schema.py              the three P7 tables admitted
backend/tests/test_tenant_isolation.py    P7 routes + the internal-job surface
frontend/src/App.tsx                      /reminders
frontend/src/api/operation.ts             reminder.send (online-only)
frontend/src/api/types.ts                 reminder shapes
frontend/src/components/AppShell.tsx      the Reminders destination
frontend/src/styles.css                   reminder row, steps, badges
CLAUDE.md                                 phase note
```

`git diff --check` reports nothing.
