# P0 — Handover

Package: **P0 Architecture Freeze**. Design and documentation only — no application code,
dependencies, migrations, database files, or lockfiles were created. Nothing was committed.

Baseline at start: branch `main`, HEAD `c2db983` ("Initialize recurring service platform
requirements"), clean tree, one tracked file (`README.md`).

---

## 1. What P0 froze

**Stack.** Python 3.12 / FastAPI / SQLAlchemy 2.x / Alembic / PostgreSQL 16, with React +
TypeScript + Vite as a PWA using a Service Worker and IndexedDB. pytest + hypothesis, Vitest,
Playwright. One process, one database, one cron trigger. No broker, no cache server, no worker
daemon, no Kubernetes.

**Money.** Integer minor units (`BIGINT`) everywhere — storage, domain, and JSON. Quantity is
`NUMERIC(12,3)`, explicitly not assumed integer; the unit label is tenant configuration. Exactly one
rounding point: `round_half_up(quantity × unit_price_minor)` at the daily record. Nothing downstream
re-rounds, so a statement total can never disagree with the sum of its lines.

**One ledger.** `ledger_entry` is the single, append-only source of every balance. Outstanding is a
running integer sum, which makes `previous + charges − payments = current` true by construction
rather than by reconciliation. Source documents carry lifecycle status; the ledger carries only
facts and compensations.

**Reporting boundaries.** Business generated, billed value, and collections are three distinct
derivations from that one ledger, separated by adjustment **origin** rather than sign:
service-origin adjustments move business generated, payment-origin adjustments move collections.
A voided payment returns outstanding to its pre-payment figure without inflating business generated
— 1000 charged, 500 paid, payment voided leaves business generated at 1000, not 1500. Statements
carry the two adjustment kinds in separate columns so billed value inherits the same clean split.

**History.** No hard-delete path for any accepted financial record. CORRECT / VOID / REVERSE, with
original value, adjustment, reason, actor, timestamp, and source preserved and linked. A superseded
record stays visible. Deliberately *not* event sourcing — one append-only ledger plus a linked
supersede chain plus an audit table.

**Late corrections.** A correction to a closed cycle keeps its true `occurred_on` but posts to the
open cycle. Issued statements are immutable and the invariant chain still holds across cycle
boundaries. This was the single most consequential design decision in the package.

**Tenancy.** Shared schema, `tenant_id` on every business row, cross-tenant references blocked by
composite foreign keys `(tenant_id, id)` at the database level rather than by application care.
Tenant provisioning is a platform action; there is no self-service signup.

**Authorization.** Two disjoint principal scopes (tenant, platform) and a flat static capability
map — no RBAC framework. No tenant role holds any `commission:*` capability, which is what makes
the platform commission boundary structural instead of procedural. Customer is not a login role.
`OPERATOR` exists as a reserved value with an empty capability set.

**Offline/sync contract.** Durable IndexedDB outbox written before any network attempt; one
client-generated `operation_id` per user intent, never regenerated; a server-side
`(tenant_id, operation_id)` register, **never pruned in V1**, that replays the same *logical* result
— not byte-identical serialization — with no new side effect; four per-operation verdicts
(`APPLIED` / `DUPLICATE` / `REJECTED` / `CONFLICT`), where `REJECTED` and `CONFLICT` are promoted
into a separate durable `issues` store instead of being dropped, so Needs Attention survives a
browser restart and nothing auto-resubmits; conflicts detected and surfaced, never merged; a
monotonic `row_version` cursor for delta pull.

**Payments — manual only (scope change).** V1 has no online gateway. The owner records `CASH`,
`BANK_TRANSFER`, or `OTHER` payments; every financial behaviour is unchanged (full, partial, unpaid,
overpayment credit, carry-forward, void/reversal, outstanding-driven reminders, and a
`COLLECTED_VALUE` commission basis over accepted manual payments). Duplicate protection rests
entirely on `operation_id`, since there is no provider reference to lean on.

> **Corrected 2026-09-03 (P5).** "offline recording" was listed here among the retained payment
> behaviours. Payment recording is **online-only in V1** — the offline write guarantee is CONFIRM
> and SKIP alone (P0 §7.2 clarification, PAY-8). Nothing was removed: no offline payment path was
> ever built.

**Voice input (new first-class requirement).** Two experiences: voice search, which transcribes and
then joins the existing read-only interpreter path; and voice daily entry, which produces a
*candidate* intent that a person must confirm. Transcription is **Groq `whisper-large-v3`** behind
the `SpeechToTextProvider` port — an implementation choice, not an architectural dependency. There is deliberately **no voice write endpoint** —
confirmation posts to the ordinary `POST /service/records`, so a voice record is identical to a
button record after acceptance apart from `input_method = VOICE`.

**Communication.** A provider-independent port. Our application owns customer, amount, type, due
state, eligibility, and stop conditions; the provider receives a rendered value. Delivery outcomes
touch `communication_log` and nothing else, so a GHL outage cannot corrupt billing.

**Reminders.** Days 1 / 4 / 8 / 12 / 15 as tenant configuration, driven by an idempotent daily job
guarded on `(tenant, job_kind, business_date)`, always recomputing the authoritative outstanding at
send time. After an outage, catch-up is by **stage**: the latest due stage is sent once and skipped
intermediate stages are never replayed, so returning on day 9 after a day-4-to-8 outage sends the
day-8 reminder only.

**Commission.** Configurable plan with four possible bases and an integer basis-point rate. Terms
are snapshotted onto every event, so renegotiation cannot rewrite earned history. Corrections
produce signed adjustments computed at the original terms. Settlement is an **independent,
append-only record of money settled** that allocates to no event and stamps nothing — which is what
makes partial settlement (earn 1000, settle 400, settle 600) representable without touching earned
history. Created only inside the transaction that centrally accepts the source event.

**Groq.** A `SearchInterpreter` port that may return only a closed-schema filter object, validated
server-side before any query is built. No tool access, no write path, no table or column naming, no
customer PII sent. Fully optional — the product is proven to work with it disabled.

**Voice safety boundary.** The operational interpreter emits a closed union
(`RECORD_SERVICE` / `SKIP_SERVICE` / `UNRESOLVED`), so a payment, price, commission, settlement,
correction, void, or configuration command is not *representable* — the limit is structural, not a
filter. Ambiguous customers never auto-select, missing quantities and dates are never invented,
config conflicts fail closed, prices are computed server-side, raw audio is never stored, and
transcripts are ephemeral. Voice is optional and offline-irrelevant: the button workflow is
untouched and remains the hard offline guarantee.

**Deployment.** Portable container + Postgres + cron-callable job endpoint. The hosting provider is
intentionally not frozen.

**Data model.** Eighteen tables, with tenant ownership, uniqueness/idempotency keys, immutability
rules, and correction links specified for each. Four candidate tables from the brief were
deliberately merged away or dropped as unnecessary (balance cache, price history,
settlement-allocation lines, tenant-settings table). Client-side, IndexedDB holds four stores:
`outbox`, `issues`, `snapshot`, `meta`.

---

## 2. What remains intentionally unresolved

Full table with owner, isolation mechanism, and latest safe answer point: **§16 of
`docs/P0_ARCHITECTURE_FREEZE.md`** (D1–D13, renumbered as questions closed). Summary:

**WhatsApp / GHL (D1–D2)** — GHL access, the exact workflow/API/webhook arrangement, the business
number, Meta verification, and approved templates. All sit behind the `CommunicationProvider` port
and a `template_key` + params contract. Development proceeds on `MockCommunicationProvider`.

**Commercial (D3–D5)** — final rate, final basis, settlement schedule. All are `commission_plan`
and `commission_settlement` data, not code.

**Product/infra (D6–D11)** — hosting provider, non-monthly cycles, the operator role, any customer
portal, multi-currency, and the client's real business unit label. None blocks V1.

**Voice (D12–D13)** — the *provider* question is now closed: Groq `whisper-large-v3` is the frozen
initial implementation. What remains open is whether its real-world quality, cost, and latency are
acceptable against representative utterances (English, Urdu, Roman Urdu intent, code-switching,
Pakistani accents, customer names, quantities, realistic noise), and whether transcripts may ever be
persisted — currently ephemeral-only by rule, and any change needs an explicit product and privacy
decision. Both sit behind the `SpeechToTextProvider` port, so an unfavourable evaluation changes a
model or an adapter, never the architecture.

**Closed by the scope change** — the former payment-provider questions (domestic vs international,
provider selection, existing merchant gateway, KYC and credentials, verification capability) are no
longer open: online payments are simply not in V1.

Assumed defaults, recorded so they are not mistaken for client decisions: PKR with exponent 2,
`Asia/Karachi`, monthly calendar cycle from the 1st, unit label **`unit`** — a deliberately generic
placeholder, since we do not actually know the client's business unit (now tracked as D11) —
reminder days 1/4/8/12/15, mock communication and speech providers, and voice disabled until a
speech provider is selected.

---

## 3. Recommended next package

**P1 — Backend & Data Foundation.** Nothing else should start first; P2's financial engine needs
P1's tables, and every UI package needs a real API.

Scope for P1:

1. Repository scaffold matching §2.1 of the architecture freeze, with the import-linter test
   (A-SLOT-5) in place from the first commit so the port/adapter boundary can never rot.
2. `app/core/money.py` — minor-unit type, `round_half_up`, quantity handling — with hypothesis
   property tests (A-FIN-1, A-FIN-2, A-FIN-3) **before** any table exists. This is the foundation
   everything else inherits.
3. Alembic baseline covering `tenant`, `app_user`, `user_session`, `customer`,
   `daily_service_record`, `ledger_entry`, `audit_event`, `sync_operation`, plus the shared
   `row_version` sequence — including every constraint named in §6 (partial unique index on active
   daily records, composite FKs, checks).
4. A schema-assertion test that reads constraints from the live database, so a dropped index in a
   later migration fails the build.
5. Authentication, the capability map, and the `TenantContext` scoping layer.
6. The tenant isolation suite (A-SEC-3/4), route-enumerated from OpenAPI.
7. Customer CRUD and daily service record record/correct/void, with the audit trail.
8. `.env.example` (names only, empty values) and `.gitignore`.

Explicitly not in P1: billing cycles, statements, payments, reminders, commission, sync endpoints,
any UI, voice or AI of any kind, and any provider adapter beyond the mock stubs.

Suggested first review checkpoint: after item 3, confirm the migration's constraints against §6
before building on them — a constraint added late is far more expensive than one added first.

---

## 4. Files created or changed

| File | Status | Purpose |
| --- | --- | --- |
| `README.md` | **modified** | The authoritative product brief, updated for the two scope changes: the online payment gateway removed from V1, and voice input added (voice search, voice daily entry, mandatory confirmation, allowed/forbidden voice actions, online dependency and fallback, privacy, accessibility rationale). Build order, pending items, golden rules, and success criteria updated to match. |
| `CLAUDE.md` | created | Operating rules for future sessions: phase, stack, non-negotiable invariants, code boundaries |
| `docs/P0_ARCHITECTURE_FREEZE.md` | created | The frozen architecture: stack rationale, modules, roles, tenancy, data model, financial authority, sync contract, voice architecture, comms contract, reminders, commission, AI authority boundary, config, deployment, deferred decisions |
| `docs/P0_INVARIANTS_AND_ACCEPTANCE.md` | created | Numbered invariants (FIN, SEC, SYN, PAY, REM, COM, AUD, VOI) with testable acceptance criteria |
| `docs/P0_HANDOVER.md` | created | This document |

`README.md` was already tracked and is **modified**, not created; it remains the authoritative
product brief and must be committed together with these documents so the requirements and the
architecture cannot drift apart. No other file was created, modified, or deleted, and no commit has
been made.

---

## 5. Risks and assumptions requiring future attention

**R1 — Manual duplicate payments cannot be fully prevented (accepted).** Two genuine cash payments
of the same amount on the same day are legal, so no natural key can forbid them. Protection rests
entirely on `operation_id`. The server-side half of that is now permanent — the register is never
pruned (SYN-13), so there is no retention horizon past which a retry would be re-accepted. The
remaining exposure is therefore purely client-side: if the UI ever regenerates the id on retry,
duplicate payments become possible. Guard it with a test in P1 and P5, and warn (never block) on
same-amount same-day repeats.

**R2 — Transcription quality for the actual language mix is unproven.** Roman Urdu, Urdu-English
code-switching, and Pakistani accents are exactly where general STT models are weakest, and this is
the feature aimed at the least software-literate users. Evaluate real utterances against a real
provider before committing to voice as a primary path (D12). Groq `whisper-large-v3` was chosen as
the initial implementation precisely because accuracy matters more than latency here, but that is a
starting point, not evidence. The mitigation if quality disappoints is a model or adapter swap
behind the unchanged port — and voice stays optional while the buttons already work.

**R3 — Balance is computed, not cached.** Correct and simple, and fine for hundreds of customers.
Measure the outstanding-list and dashboard queries at realistic volume during P6. If they degrade,
add a rebuildable cache table — never a hand-maintained running total.

**R4 — Timezone and business date.** Everything hinges on "today" in the tenant's timezone while
instants are UTC. A device with a wrong clock, or a user recording just before local midnight, can
target the wrong service date. Freeze the rule in P1 — the server assigns the business date from
the tenant timezone, and the client's `client_created_at` is advisory only — and test it around
midnight boundaries.

**R5 — Offline conflict UX is unspecified.** The contract guarantees conflicts are *detected*. What
the owner sees and can do about one is a P5 design task, and it is the most likely place for a
frustrating experience. Budget real design time for it.

**R6 — Commission basis is still open (D4).** All four bases are supported, but the trigger point
differs per basis. Once chosen, backfilling events for historical data will need a one-off, audited
script. Prefer settling D4 before P3 runs against real data.

**R7 — GHL's actual shape is unknown (D1).** The port assumes a send call plus an optional delivery
callback. If GHL turns out to be workflow-triggered rather than message-triggered, the adapter
absorbs it — but the `template_key` + params contract may need widening. Confirm early; the port
survives, the message contract may not.

**R8 — Statement immutability versus real-world pressure.** Owners will eventually ask to "fix last
month's bill". The frozen answer is a posted adjustment on the current cycle, not an edit. This is
a client-communication matter as much as a technical one; raise it before the first month closes.

**R9 — Prompt-injection surface, now larger.** The customer database is still never sent to the
model, and customer resolution stays a deterministic database operation. But a voice transcript is
user-supplied text reaching a model, so the surface is real. Three things hold the line: the output
schema is closed, everything after interpretation is deterministic server code, and a write needs a
human tap — so the worst achievable outcome is a wrong candidate someone declines. Re-review this
if a future feature ever feeds record content into a prompt, or acts on interpreter output without
confirmation.

**R10 — Spoken-name resolution will degrade as the customer list grows.** "Essa" is unambiguous
among 40 customers and much less so among 400, and near-homophones are common. VOI-4 forbids
guessing, so the failure mode is safe but can become *annoying* — a candidate list every time. Watch
it during P9 and be willing to add a spoken short-code or route-based narrowing.

**R11 — Voice cost and latency are per-utterance and unbudgeted.** A daily round of 200 customers is
200 transcriptions if voice becomes the primary path, and `whisper-large-v3` is the larger, slower,
costlier option in its family. Measure before enabling it broadly (D12); `whisper-large-v3-turbo` is
the obvious fallback if the trade lands badly.

**R12 — Nothing is committed.** These four files are untracked working-tree changes. They need to
be committed before P1 starts, or the freeze is not actually frozen.

---

## 6. Scope change before commit — online payments out, voice in

Two client requirements changed after the P0 audit and before any implementation, so P0 was revised
rather than committed.

**A. Online payment gateway removed from V1.** The client confirmed no gateway is needed. Rather
than keep dead complexity behind a port nobody will call, the whole online-payment architecture was
deleted: the `PaymentProvider` port and mock, the `payment_attempt` table, provider references,
payment intents, the callback/webhook route, the PULL / SIGNED_PUSH verification model, the
redirect and success-page rules, provider state vocabulary, provider env vars and adapter
directory, PayFast/JazzCash references, the five payment deferred decisions, and the
gateway-verification PAY invariant family and its A-SLOT proof.

Kept intact: the `payment` entity, the append-only ledger, full/partial/unpaid/overpayment
behaviour, carry-forward, payment history, governed void/reversal, `operation_id` idempotency,
outstanding-driven reminders, and `COLLECTED_VALUE` commission over accepted manual payments. The
PAY family was rewritten around manual payments rather than deleted. (P5 corrected "offline manual
recording" out of this list — see the note in §"Payments — manual only" above and PAY-8.)

If online payments ever return, they arrive as a new provider port over an unchanged ledger. That
is a smaller job than carrying the abstraction unused through V1.

**B. Voice input is a first-class V1 requirement.** Users may have limited software literacy, so
voice is a lower-friction way to express operations that already exist. Added: a §8 voice
architecture, `SpeechToTextProvider` and `OperationalIntentInterpreter` ports with mocks, an
`input_method` provenance column, a confirmation-card contract, conservative privacy rules, an
offline degradation table, accessibility principles, an expanded §12 AI authority boundary, and the
VOI invariant family with fifteen acceptance criteria.

The load-bearing decision is that **there is no voice write endpoint**. `/voice/*` only transcribes
and interprets; a confirmed entry posts to the ordinary `POST /service/records`. "Voice uses the
same domain path" is therefore a property of the route table rather than a rule someone must
remember, and voice inherits every financial, tenancy, idempotency, and history invariant for free.

Net effect on the data model: **nineteen tables → eighteen** (`payment_attempt` removed; voice adds
no table, because audio is never stored and transcripts are ephemeral). Deferred decisions
renumbered D1–D13. Pluggable integration points: three (GHL/WhatsApp, speech-to-text, AI
interpreters) — the phrase "two open integration slots" is retired.

All seven previously accepted audit corrections are preserved unchanged.

---

## 7. Blocking issues

None. No unresolved item prevents P1 from starting, and every deferred decision (D1–D13) is
isolated behind a port, an environment value, or a database row. The scope change **reduces** P1's
surface: there is less to build, not more.

---

## 8. Post-review corrections applied

An external review of the first freeze found seven defects. All are corrected in place; the stack,
the ledger design, tenancy, the provider ports, and the Groq boundary are unchanged.

| # | Defect | Correction |
| --- | --- | --- |
| 1 | `settlement_id` on earning rows could not express partial settlement (earn 1000, settle 400, then 600) | Column removed from `commission_event` and `commission_adjustment`. `commission_settlement` is now an independent append-only record; outstanding is the aggregate. Allocation deferred until genuinely needed. |
| 2 | Business generated summed all `ADJUSTMENT` entries, so a voided payment would have inflated it from 1000 to 1500 | Adjustment **origin** rule frozen (§5.3). Business generated counts service-origin only; collections count payment-origin. New §11.1 defines business generated, billed value, and collections separately. Statement adjustment column split by origin. |
| 3 | 180-day idempotency retention was a duplication horizon | `sync_operation` retained indefinitely in V1, never pruned. No archival subsystem added. |
| 4 | `REJECTED` left the outbox with nowhere durable to go, and conflicts could retry-loop | Fourth IndexedDB store `issues` added. `REJECTED` / `CONFLICT` are promoted into it transactionally; they survive restart, drive Needs Attention, and never auto-resubmit. |
| 5 | "Catch up on the next run for the same business date" is impossible once the date has moved | Stage-based catch-up frozen: send only the latest due stage, at most one message per run. |
| 6 | `litre` was assumed as the default unit without evidence | Default is now the generic `unit`; the real unit is tracked as a deferred decision (D16 at the time, renumbered D11 by the later scope change). |
| 7 | "Byte-identical replay" over-promised against a JSONB result column | Requirement restated as same logical result, same authoritative entity, no new side effect. |

Affected invariants: FIN-8, new FIN-14/15/16, SYN-2, SYN-6, SYN-7, SYN-11, new SYN-12/13, REM-8,
COM-6, new COM-11. Affected acceptance IDs: A-FIN-8, A-FIN-9, A-SYN-1/2, A-SYN-7, A-SYN-8,
A-COM-6; new A-FIN-14, A-FIN-15, A-FIN-16, A-SYN-12, A-SYN-13, A-REM-8a, A-REM-8b, A-REM-8c,
A-REM-8d, A-COM-6b.

The table count is unchanged at nineteen: defect 1 removed two columns rather than a table, and the
`issues` store is client-side IndexedDB, not a database table.
