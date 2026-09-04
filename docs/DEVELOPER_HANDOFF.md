# Recurring Service Platform — Developer Handoff / Current State

> **Audience:** Yahya + Areeb / collaborating developers
> **Purpose:** Internal technical handoff so another developer can continue the project without reconstructing the architecture from old chats.
> **Status date:** 4 September 2026
> **Not client-facing.**

---

# 1. Executive summary

The project is now past the basic CRUD stage and has a substantial working architecture.

The system currently includes:

- tenant/auth foundation;
- customer management;
- daily recurring service recording;
- immutable financial ledger semantics;
- billing cycles and statements;
- manual payments and reversals;
- commission accounting;
- React owner/operator UI;
- offline-first Daily Register with durable sync;
- owner financial dashboard;
- operating-cost tracking;
- staged reminder engine;
- customer aliases and smart customer resolution/search.

The remaining major work is:

1. **manual hands-on testing / UX corrections**;
2. **P9 Voice + AI command layer**;
3. **P10 WhatsApp / messaging integration**;
4. **P11 full hardening / security / end-to-end UAT**;
5. **P12 production deployment / backups / handover**.

The most important design rule is:

> **The backend owns money, identity, authorization, reminders, and business state. AI, voice, WhatsApp, SMS, and offline clients may request actions, but they do not become the source of truth.**

---

# 2. Repository and collaboration model

GitHub repository:

```text
https://github.com/areeb-hayat/recurring-service-platform
```

## Local worktrees

### Integration / reference worktree

```text
E:\Recurring-Service-Platform
branch: main
```

### Yahya development worktree

```text
E:\Recurring-Service-Platform-yahya
branch: yahya
upstream: origin/yahya
```

## Collaboration rules

- Normal Yahya development happens only in:
  ```text
  E:\Recurring-Service-Platform-yahya
  ```

- Do not modify Areeb's worktree while doing Yahya feature work.

- Normal push target:
  ```text
  origin/yahya
  ```

- Do not push Yahya feature work directly to:
  ```text
  origin/main
  ```

- Integration into `main` should be deliberate and reviewed separately.

Before continuing any work, always run:

```powershell
cd "E:\Recurring-Service-Platform-yahya"

git branch --show-current
git status
git log -5 --oneline --decorate
git branch -vv
```

Do not assume the local or remote branch is current until these commands confirm it.

---

# 3. Known accepted commit history through P7

The last confirmed accepted commit history is:

```text
1661c3b  Implement P7 reminder engine and delivery orchestration
b93692e  Implement P6 owner financial dashboard and operating costs
4e6e4f8  Implement P5 offline-first sync and durable conflict handling
c383af2  Implement P4 customer and daily workflow frontend
95a2feb  Implement P3 commission engine and commercial tracking
ccf08a5  Implement P2 financial engine
22df746  Implement P1 backend and data foundation
3f64c35  Freeze platform architecture and requirements
c2db983  Initialize recurring service platform requirements
```

## P8 Git status

P8 was fully implemented and verified in the coding session, but the exact final P8 commit hash was **not yet confirmed in this handoff conversation**.

Therefore the first thing the next developer must do is:

```powershell
git status
git log -5 --oneline --decorate
git branch -vv
```

If P8 is still uncommitted, commit/push it after reviewing the diff.

Do not invent a P8 commit hash.

---

# 4. Phase-by-phase project history

## P0 — Architecture / invariants freeze

P0 established the rules that later packages must not casually violate.

Main decisions:

- backend is authoritative;
- money stored as integer minor units;
- quantity uses Decimal / NUMERIC;
- ledger is the balance source;
- accepted financial history is not destructively rewritten;
- statements are immutable after issue;
- payment reversal is compensating history, not delete;
- tenant isolation is mandatory;
- platform-owner commercial data is separate from tenant financial data;
- offline writes are intentionally narrow;
- communication providers cannot decide reminder eligibility or amount;
- AI cannot directly authorize sensitive financial actions.

P0 documentation:

```text
docs/P0_ARCHITECTURE_FREEZE.md
docs/P0_INVARIANTS_AND_ACCEPTANCE.md
```

These documents should still be treated as the architectural source of truth unless a later handover explicitly records a deliberate amendment.

---

## P1 — Backend and data foundation

P1 built the core backend.

Main pieces:

- FastAPI application;
- PostgreSQL persistence;
- tenant model;
- authentication / authorization;
- customer model;
- daily service record model;
- append-oriented ledger;
- audit trail;
- sync operation / idempotency foundation;
- row versioning where needed.

Important correctness work:

- no arbitrary backdate cap;
- future service dates rejected;
- idempotency protects same operation ID;
- same ID with different payload is rejected;
- concurrent identical operations serialize safely;
- real PostgreSQL used for meaningful backend verification.

P1 accepted commit:

```text
22df746
```

---

## P2 — Financial engine

P2 added the actual recurring billing / collection model.

Main pieces:

- billing cycles;
- issued statements;
- manual payments;
- payment history;
- payment reversal / void;
- financial reporting;
- current outstanding derivation;
- late correction semantics.

Supported V1 payment methods:

```text
CASH
BANK_TRANSFER
OTHER
```

Payment rules:

- amount must be positive;
- partial payment allowed;
- full payment allowed;
- overpayment / credit allowed;
- payment is idempotent;
- no payment hard delete;
- reversal uses compensating ledger behavior.

Statement rules:

- issued statements immutable;
- old statement is not silently recalculated;
- corrections preserve the original business occurrence date;
- late financial corrections post into the current valid open cycle.

P2 accepted commit:

```text
ccf08a5
```

---

## P3 — Commission / commercial engine

P3 added the platform/shareholder commercial accounting layer.

Supported commission bases:

```text
RECORDED_VALUE
BILLED_VALUE
COLLECTED_VALUE
PER_EVENT
```

Main rules:

- commission events snapshot the terms that applied when generated;
- later plan changes do not rewrite historical earnings;
- commission adjustments preserve original commercial terms;
- settlements are separate append-only aggregates;
- no allocation mutation on historical earning rows;
- platform-only authority;
- commission remains separate from tenant customer accounting.

P3 accepted commit:

```text
95a2feb
```

Important architectural rule:

> Operating costs, customer balances, and platform commission are three separate concepts.

Do not merge them.

---

## P4 — Main React website / Daily Register

P4 built the main frontend.

Technology:

- React 18;
- TypeScript;
- Vite;
- TanStack Query.

Main screens/workflows:

- login;
- customer list;
- create customer;
- customer detail;
- edit customer;
- Daily Register.

Daily Register UX:

```text
Customer
[-] [qty] [+]
[CONFIRM]
[SKIP TODAY]
→ next customer
```

It also preserves "Leave for later".

The daily workflow distinguishes:

- still to do;
- done.

P4 accepted commit:

```text
c383af2
```

---

## P5 — Offline-first / PWA / sync

P5 is one of the most important architecture packages.

Frontend local stores:

```text
outbox
issues
snapshot
meta
```

Per-tenant IndexedDB:

```text
rsp-sync-v1-<tenant_id>
```

### Hard V1 offline write guarantee

Only these operations are supported offline:

```text
service.record   → Daily Register CONFIRM
service.skip     → SKIP TODAY
```

Do not broaden this casually.

The following remain online-only unless there is a future explicit architecture decision:

- payment.record;
- payment.void;
- customer edits;
- alias edits;
- corrections;
- operating-cost writes;
- reminder sends;
- commission operations;
- settlement operations;
- configuration changes.

### Sync behavior

- operation ID generated once and retained;
- network/timeout/5xx stays queued;
- REJECTED/CONFLICT moves to durable issues;
- conflict resolution is fail-closed;
- authentication failure preserves local data;
- sync state is visible to the operator.

### Major P5 concurrency fix

The original row-version allocation model could theoretically skip a row if two same-tenant writes committed out of order.

This was fixed with PostgreSQL transaction advisory locking for feed-visible same-tenant writes.

The tenant advisory key was improved from a UUID prefix to a BLAKE2b-derived hash of the full UUID bytes.

Important invariant:

> When adding a new feed-visible entity or mutation path, update the feed writer serialization set and the tests that pin correspondence.

P5 accepted commit:

```text
4e6e4f8
```

---

## P6 — Owner financial dashboard + Operating Costs

P6 added the owner-facing finance/admin surface.

Main functionality:

- owner dashboard;
- current outstanding;
- customer counts;
- current-cycle summaries;
- recent payment activity;
- customer financial view;
- statement list/detail;
- manual payment UI;
- payment history;
- payment void/reversal UI;
- operating-cost tracker;
- rate history;
- usage estimates;
- actual invoice entries;
- variance reporting;
- monthly history;
- planning/scenario calculator.

### Operating Costs architecture

Operating costs are tenant business infrastructure costs.

Examples:

- application/database hosting;
- voice transcription;
- AI intent interpretation;
- backup storage;
- WhatsApp automation hosting;
- messaging fees;
- domain;
- other approved provider costs.

They are **not**:

- customer billing;
- customer ledger entries;
- platform/shareholder commission.

The model supports:

- configurable cost item/provider;
- versioned effective rates;
- measured usage;
- estimated cost;
- actual invoice;
- variance;
- historical monthly view.

Formula:

```text
variance = actual - estimated
```

No actual invoice is invented when none exists.

Currencies are not silently converted.

Provider prices are stored as versioned data, not hard-coded application logic.

### P6 sync extension

P6 added read-only sync visibility for:

- payment;
- statement.

Payment writes remained online-only.

P6 accepted commit:

```text
b93692e
```

---

## P7 — Reminder engine

P7 implemented the reminder decision engine.

Default schedule:

```text
Day 1   → Statement
Day 4   → Reminder
Day 8   → Reminder
Day 12  → Reminder
Day 15  → Final reminder + owner alert
```

### Current-balance rule

Reminder amount is derived from the current authoritative balance at execution time.

Example:

```text
Statement = 1000
Customer pays = 400
Next reminder = 600
```

Never reuse the stale statement amount.

### Full / partial payment behavior

- full payment → later outstanding reminders stop;
- partial payment → reminders continue using reduced current balance;
- credit / overpayment → no outstanding reminder;
- voided payment may make reminders eligible again.

### Catch-up rule

If the reminder runner was down:

```text
send only the latest currently due unsatisfied stage
```

Example:

```text
Day 4 missed
Day 8 missed
System runs Day 10
→ send Day 8 only
```

Never flood the customer with every missed reminder.

At most one customer-facing stage per customer/cycle/run.

### Provider boundary

Backend decides:

- who is eligible;
- which stage is due;
- amount;
- whether to suppress;
- semantic message intent.

External provider only delivers.

Future n8n / Evolution / WhatsApp / SMS must not take over business authority.

### Cron

A protected daily job endpoint exists.

Cron secret behavior:

- unset → disabled;
- secret compared safely;
- route does not accept arbitrary tenant selection from request input.

P7 accepted commit:

```text
1661c3b
```

---

## P8 — Smart customer search / aliases / identification

P8 implementation was completed and verified.

Reported final verification:

```text
Backend PostgreSQL tests: 1075 passed
Frontend tests:          187 passed
TypeScript:              clean
Production build:        clean
Migration round-trip:    verified
git diff --check:        clean
```

### Alias model

Customer aliases/nicknames are supported.

Example:

```text
Canonical:
Muhammad Ahmed Khan

Aliases:
Ahmed
Ahmed bhai
Chacha Ahmed
Shop wala Ahmed
```

Important rules:

- multiple aliases per customer;
- tenant-scoped;
- aliases are searchable;
- aliases are not required to be unique across a whole tenant;
- accepted aliases are retired/reactivated rather than destructively deleted;
- alias mutation bumps the parent customer version;
- no AI-generated aliases.

### Search normalization

One controlled normalization path was introduced.

Reported behavior:

- Unicode normalization;
- case folding;
- repeated whitespace normalization;
- punctuation normalization;
- phone digit normalization;
- no automatic transliteration engine.

### Search ranking

Reported deterministic tiers:

```text
exact customer code  100
exact phone            95
phone suffix           90
exact name             85
exact alias            80
name tokens            75
alias tokens           70
prefix                  55/50
substring               45/40
area                    30
trigram                 20
```

### Critical identity contract

```text
RESOLVED
AMBIGUOUS
NOT_FOUND
```

The resolver must never silently choose an ambiguous customer.

A weak/fuzzy top result remains only a candidate.

This is critical because P9 and P10 will reuse this resolver.

### P8 website integration

P8 added:

- server-backed customer search;
- alias management;
- search result disambiguation context;
- Daily Register customer jump;
- offline snapshot search;
- navigation to customer financials.

### P8 sync behavior

Reported:

```text
SYNC_FEED_VERSION = 3
```

P8 changed the customer snapshot shape to include alias data.

Alias writes remain online-only.

No new independent alias feed entity was required.

### PostgreSQL extension

P8 uses:

```text
pg_trgm
```

Production deployment must confirm the selected PostgreSQL environment supports/enables this extension and the migration handles it correctly.

---

# 5. Current architecture summary

## Backend

Core stack:

```text
Python
FastAPI
PostgreSQL
Alembic
pytest
```

Important backend domains include:

```text
identity/auth
customers
service
billing
payments
ledger
commission
sync
costs
reminders
search
audit
```

Do not treat frontend calculations as authoritative financial logic.

## Frontend

Core stack:

```text
React
TypeScript
Vite
TanStack Query
PWA / service worker
IndexedDB
Vitest
```

Important frontend areas include:

```text
customers
daily
dashboard
payments
statements
costs
reminders
search
sync
```

---

# 6. Core business invariants

These should not be changed casually.

## Balance

```text
Previous Outstanding
+ Current Cycle Charges
- Payments
± Adjustments
= Current Outstanding
```

## Money

- store authoritative money in integer minor units;
- no float-based money arithmetic;
- quantity uses Decimal;
- charge rounding occurs once with explicit half-up behavior.

## Ledger

- append-oriented;
- single balance source;
- corrections/reversals preserve history.

## Statements

- immutable after issue.

## Payments

- manual in V1;
- CASH / BANK_TRANSFER / OTHER;
- partial/full/overpayment supported;
- no hard delete;
- reversal uses compensating history.

## Commission

- separate from customer accounting;
- separate from Operating Costs.

## Identity

- ambiguous customer identity must fail closed.

## Offline

- only CONFIRM and SKIP are guaranteed offline writes.

## Reminder authority

- backend determines amount/stage/eligibility.

## AI / messaging authority

- AI/provider does not become financial or identity authority.

---

# 7. Planned P9 — Voice / AI

P9 has not yet been implemented.

Current planned providers:

```env
SPEECH_PROVIDER=elevenlabs
SPEECH_MODEL=scribe_v2
ELEVENLABS_API_KEY=...
```

Planned intent provider:

```env
INTENT_PROVIDER=groq
INTENT_MODEL=openai/gpt-oss-20b
```

## Planned architecture

```text
Audio
  ↓
ElevenLabs Scribe v2
  ↓
Transcript
  ↓
Constrained text intent interpreter
  ↓
P8 customer resolver
  ↓
Deterministic backend validation
  ↓
Explicit confirmation when required
  ↓
Existing backend command
```

ElevenLabs should transcribe only.

Groq / the LLM should interpret text into constrained intent, not directly mutate business state.

## Sensitive actions

Voice/LLM must not directly authorize:

- payments;
- pricing;
- commission;
- settlements;
- platform configuration;
- other sensitive financial administration.

## Approved voice usability features

The following have been approved for P9 planning:

1. voice-assisted customer creation;
2. spoken readback / confirmation;
3. aliases / nicknames;
4. pause/resume voice workflow;
5. change future default quantity by voice;
6. retrospective service entry by voice;
7. customer bill breakdown by voice;
8. customer/audio identification help for low-literacy operators;
9. targeted clarification when only part of a command is ambiguous.

## Privacy

Do not store raw audio by default.

---

# 8. Planned P10 — WhatsApp / SMS / messaging

P10 has not yet been implemented.

Current likely direction:

```text
Backend
   ↓
semantic message / command
   ↓
n8n
   ↓
Evolution API
   ↓
WhatsApp
```

Possible SMS channel later.

Likely infrastructure:

```text
n8n
Evolution API
PostgreSQL
Redis
client-owned Hostinger VPS
```

Initial low-cost route may use WhatsApp Web / Baileys-style connectivity.

Important:

> This is unofficial and should not be represented to the client as guaranteed-reliable.

Keep a migration path to:

```text
Meta WhatsApp Cloud API
```

The messaging provider must not decide:

- balance;
- reminder stage;
- identity;
- payment state;
- business authorization.

Those stay in the backend.

---

# 9. Provider / production ownership model

Development accounts may temporarily belong to developers.

Production should generally use client-owned accounts where practical.

Recommended pattern:

1. client creates production provider account;
2. client owns billing;
3. restricted production credential is created;
4. credential is inserted into production environment;
5. code remains unchanged.

Never ask the client to send card details.

---

# 10. What should happen RIGHT NOW before P9

Do not immediately begin voice integration.

This is the correct checkpoint for manual developer/owner testing.

## Step 1 — Verify Git / close P8

Run:

```powershell
cd "E:\Recurring-Service-Platform-yahya"

git branch --show-current
git status
git log -5 --oneline --decorate
git branch -vv
```

If P8 is still uncommitted:

```powershell
git add -A
git diff --cached --stat
git diff --cached --check
```

Review before committing.

Then commit P8 and push:

```powershell
git push origin yahya
```

Verify:

```powershell
git status
git branch -vv
```

The working tree should be clean and `yahya` should align with `origin/yahya`.

---

# 11. Manual testing checkpoint

The project should now be tested by the actual developers before P9.

Do a real business workflow, not only unit tests.

## Recommended test flow

### Authentication

- log in;
- log out;
- log in again;
- verify tenant isolation assumptions.

### Customers

- create a customer;
- edit customer;
- inspect customer detail;
- add several aliases;
- retire/reactivate an alias if UI supports it.

### Search

Try:

```text
exact canonical name
alias
customer code
phone
partial name
ambiguous common name
slight typo
```

Confirm:

- strong unique match resolves;
- ambiguous query asks which customer;
- weak fuzzy match does not silently select someone.

### Daily Register

For several customers:

- change quantity;
- CONFIRM;
- SKIP TODAY;
- Leave for later;
- jump to a customer from search;
- verify Done / Waiting / Needs attention states remain correct.

### Offline

Disconnect the browser/network.

Try only:

- CONFIRM;
- SKIP TODAY.

Verify:

- operation is queued;
- sync state is visible;
- reconnecting sends it;
- duplicate records are not created;
- unresolved conflicts remain visible.

Do not expect offline customer edits/payments/etc.

### Financials

- inspect current outstanding;
- view statement history;
- record partial payment;
- record remaining full payment;
- create an overpayment/credit scenario;
- void/reverse a payment;
- verify balances remain correct;
- confirm history is retained.

### Dashboard

Verify:

- outstanding;
- cycle summary;
- customer counts;
- recent payment activity.

### Operating Costs

Create/test:

- provider/cost item;
- versioned rate;
- usage;
- estimated cost;
- actual invoice;
- variance;
- monthly history.

Verify:

- actual is not invented;
- currencies stay separate;
- operating costs do not affect customer outstanding;
- operating costs do not affect commission.

### Reminders

Inspect:

- due customer;
- reminder stage;
- final/attention state;
- current outstanding amount;
- retry behavior for a failed delivery.

---

# 12. UX issue triage before P9

While manually testing, write findings under:

```text
BLOCKING
IMPORTANT
POLISH
```

## BLOCKING

Examples:

- wrong money;
- wrong customer;
- broken sync;
- duplicate financial entry;
- reminder sent incorrectly;
- authorization leak;
- page unusable.

## IMPORTANT

Examples:

- confusing payment flow;
- difficult customer lookup;
- unclear offline state;
- awkward mobile Daily Register;
- dashboard labels unclear.

## POLISH

Examples:

- spacing;
- wording;
- visual hierarchy;
- cosmetic layout.

Fix BLOCKING and justified IMPORTANT issues before P9.

Do not delay P9 for endless cosmetic polishing.

---

# 13. Remaining roadmap

## P9 — Voice / AI

Build:

- ElevenLabs Scribe v2 integration;
- constrained intent schema;
- Groq text intent interpretation;
- P8 resolver reuse;
- explicit confirmation/readback;
- approved voice workflows;
- no raw audio storage by default;
- provider usage measurement for Operating Costs.

## P10 — Messaging

Build:

- real CommunicationProvider adapter;
- WhatsApp transport;
- n8n / Evolution integration;
- possible SMS transport/foundation;
- inbound/outbound text intent reuse;
- idempotent provider delivery;
- delivery identity preserved across timeout/retry;
- provider usage feeds into Operating Costs.

Important unresolved production concern:

> If a provider accepts a message but our response is lost, retries must use the same logical delivery identity and must not create a duplicate customer message.

## P11 — Hardening / UAT

This is the formal stabilization phase.

Should include:

- full end-to-end workflows;
- security review;
- tenant isolation audit;
- concurrency/race testing;
- duplicate submission testing;
- degraded network behavior;
- offline/reconnect edge cases;
- browser/mobile testing;
- provider outage testing;
- secret scanning;
- dependency/advisory review;
- restore/recovery behavior;
- owner acceptance testing;
- UX fixes;
- final regression suite.

Known older risk worth revisiting:

```text
react-router-dom v6 advisories
```

Also ensure the secret-scanning acceptance item that remained open in earlier handovers is fully closed by P11.

## P12 — Deployment / production handover

Should include:

- production application hosting;
- production PostgreSQL;
- environment variables/secrets;
- domain;
- HTTPS/TLS;
- cron scheduling;
- production voice credentials;
- production messaging credentials;
- backups;
- restore test;
- off-site backup strategy;
- production `pg_trgm` support;
- monitoring/logging;
- client-owned provider accounts;
- admin/user creation;
- operator training;
- operations guide;
- technical handover;
- final project-history document.

---

# 14. Planned production infrastructure / cost direction

The final stack is not completely frozen, but current planning includes:

### Main app/database
Potential managed deployment such as Railway or equivalent.

### Backups
Primary DB backup/PITR plus encrypted off-site dump storage, with Cloudflare R2 considered.

Suggested retention concept:

```text
7 daily
4 weekly
6 monthly
```

plus periodic restore tests.

### WhatsApp automation
Potential client-owned Hostinger VPS for:

```text
n8n
Evolution API
PostgreSQL
Redis
```

### Domain
Either:

- client provides domain; or
- development team buys/configures and transfers ownership/billing at handover.

Do not block development on this.

---

# 15. Cost examples already used in planning

These are planning inputs, not permanent hard-coded application constants.

## ElevenLabs Scribe v2

Planning rate:

```text
$0.22 / audio hour
```

Example at 5 seconds/voice command:

```text
100 commands/day
≈ 4.17 hours/month
≈ $0.92/month

500 commands/day
≈ 20.83 hours/month
≈ $4.58/month

1000 commands/day
≈ 41.67 hours/month
≈ $9.17/month
```

## Groq planned model

```text
openai/gpt-oss-20b
```

Previously used planning rates:

```text
$0.075 / 1M input tokens
$0.30  / 1M output tokens
```

Re-check provider rates before production.

## Cloudflare R2 planning

Previously used planning basis:

```text
first 10 GB-month free
then $0.015 / GB-month
```

Again: re-check current production pricing later.

---

# 16. Internal developer workflow

The project was implemented in packages.

Normal workflow:

```text
1. verify clean Git state
2. read latest handover
3. read only relevant frozen invariants
4. implement one bounded package
5. targeted tests during implementation
6. one final full verification
7. write docs/Px_HANDOVER.md
8. review
9. commit locally
10. push origin/yahya
```

Do not run broad repository audits repeatedly unless there is a real correctness reason.

---

# 17. Testing discipline

During development:

- targeted backend tests;
- targeted frontend tests;
- migration/schema tests when relevant.

At the end of a package:

- full PostgreSQL backend suite once;
- full frontend Vitest suite once;
- TypeScript strict typecheck;
- production frontend build;
- migration up/down/up if migration added;
- `git diff --check`;
- `git status`.

Browser E2E should be used when there is a browser-only behavior that unit/integration tests cannot honestly prove.

P11 is the main full-system E2E/UAT phase.

---

# 18. Important handover files

Read these before changing architecture:

```text
CLAUDE.md
docs/P0_ARCHITECTURE_FREEZE.md
docs/P0_INVARIANTS_AND_ACCEPTANCE.md
docs/P1_HANDOVER.md
docs/P2_HANDOVER.md
docs/P3_HANDOVER.md
docs/P4_HANDOVER.md
docs/P5_HANDOVER.md
docs/P6_HANDOVER.md
docs/P7_HANDOVER.md
docs/P8_HANDOVER.md
```

The latest handover should normally be read first, then only the relevant P0 sections.

Do not reread/reinterpret the entire repo from scratch if the handover already records the settled decision.

---

# 19. Things the next developer must NOT casually change

Do not change these without an explicit architecture discussion:

- offline writes beyond CONFIRM/SKIP;
- money from integer minor units to float;
- ledger as source of customer balance;
- immutable statement history;
- compensating payment reversals;
- customer resolver fail-closed ambiguity;
- tenant isolation;
- platform-vs-tenant capability separation;
- reminder current-balance behavior;
- reminder latest-stage-only catch-up;
- Operating Costs / commission separation;
- communication provider as delivery-only;
- AI/voice authority boundaries;
- sync no-gap / advisory serialization logic;
- direct Yahya feature pushes to `main`.

---

# 20. Immediate developer checklist

Before continuing:

```text
[ ] Verify current branch is yahya
[ ] Verify exact HEAD
[ ] Verify whether P8 is committed
[ ] Verify whether P8 is pushed to origin/yahya
[ ] Working tree clean
[ ] Read docs/P8_HANDOVER.md
[ ] Run application locally
[ ] Perform hands-on smoke test
[ ] Record BLOCKING / IMPORTANT / POLISH issues
[ ] Fix only justified pre-P9 issues
[ ] Freeze clean checkpoint
[ ] Begin P9
```

---

# 21. Short architecture rulebook

When unsure, follow these rules:

```text
Do not guess customer identity.
Do not guess financial amounts.
Do not silently rewrite history.
Do not silently resolve sync conflicts.
Do not let AI become financial authority.
Do not let messaging providers decide reminder logic.
Do not casually broaden offline writes.
Do not mix operating costs with commission.
Do not leak tenant data across tenants.
Do not bypass the existing backend commands just because a new channel is easier.
```

---

# 22. Current done vs left snapshot

## Done

```text
P0 Architecture freeze
P1 Backend/data/auth foundation
P2 Financial engine
P3 Commission engine
P4 Main frontend / Daily Register
P5 Offline-first sync
P6 Owner financial dashboard + Operating Costs
P7 Reminder engine
P8 Smart search / aliases / customer identification
```

## Do now

```text
Close/verify P8 in Git
Manual hands-on test
Fix real workflow issues
```

## Still to build

```text
P9  Voice / AI
P10 WhatsApp / SMS messaging
P11 Hardening / E2E / security / UAT
P12 Production deployment / backups / handover
```

---

# 23. Handoff note to the next developer

The project does not need to be rewritten.

A lot of the difficult correctness work has already been done:

- money semantics;
- immutable financial history;
- idempotency;
- offline durability;
- sync concurrency;
- tenant isolation;
- reminder catch-up;
- customer ambiguity handling.

The next developer should **reuse those foundations**, not create parallel implementations.

P9 and P10 should be integration layers over the existing business commands.

The right mental model is:

```text
INPUT CHANNEL
(web / offline / voice / WhatsApp / SMS)
        ↓
shared parsing / customer resolution / validation
        ↓
existing authoritative backend command
        ↓
ledger / service / reminder / financial state
```

That is the architecture to preserve going forward.
