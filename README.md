# Recurring Service, Billing & Collection Platform

## 1. What We Are Building

A simple web application for a business that provides a recurring product or service to customers.

The owner should be able to:

- Add and manage customers.
- Record how much product/service each customer actually received each day.
- Use a very fast daily screen:
  - `-`
  - quantity
  - `+`
  - `CONFIRM`
  - `SKIP`
- Let the system calculate charges automatically.
- Generate monthly bills/statements.
- Carry previous unpaid balances forward.
- Record full or partial payments.
- See who is:
  - Paid
  - Partially Paid
  - Unpaid
- See customer history and payment history.
- See an owner dashboard with:
  - Business generated
  - Amount billed
  - Amount collected
  - Amount outstanding
- Run reminder/follow-up logic for unpaid customers.
- Keep the important daily workflow usable even when internet is unavailable.
- Use **voice** instead of typing where that is easier — both to search and to record a day's
  service — without voice ever being required.

---

## 2. Main Users

### Business Owner / Admin

- Main user of the application.
- Logs in.
- Manages customers.
- Records daily service/quantity.
- Records payments.
- Views bills, balances, histories, reminders and dashboards.
- Can correct mistakes through controlled correction/void/reversal flows.
- May have limited software literacy, so the interface must stay simple and offer voice as an
  easier input method.

### Customer

- The end customer receiving the recurring product/service.
- Does **not** need a website login in V1.
- Receives bills/reminders.
- Pays the business.
- Exists as a customer record in the system.

### Platform Owner

- Separate platform-level account.
- Used for platform/commercial administration.
- Can view protected commission information.
- Must be separated from normal client business operations.

### Operator / Employee

- Not required for V1.
- Architecture should leave room for a future restricted operator role.

---

## 3. Core Business Flow

1. Owner creates a customer.
2. Customer has:
   - Name
   - Phone / WhatsApp
   - Address/location
   - Default daily quantity
   - Unit/service price
3. Each day the owner records the actual quantity/service delivered.
4. The system stores the historical price used for that record.
5. The system calculates the charge automatically.
6. Daily charges accumulate into the billing cycle.
7. A monthly statement is generated.
8. Previous outstanding balances carry forward.
9. Payments are recorded.
10. Current outstanding is recalculated.
11. Customer becomes:
    - Paid
    - Partially Paid
    - Unpaid
12. Reminder logic uses the latest balance.
13. Fully paid customers stop receiving outstanding reminders.

Financial rule:

`Previous Outstanding + Current Cycle Charges - Payments = Current Outstanding`

---

## 4. Daily Recording

The daily screen must be extremely simple.

```text
AHMED

[-]   2   [+]

[ CONFIRM ]

[ SKIP ]
```

Rules:

- `CONFIRM` records the displayed quantity.
- `+` increases today's quantity.
- `-` decreases today's quantity.
- `SKIP` records no service for that day.
- The user should not calculate money manually.
- Repeated clicks must not create duplicate records.
- The next customer should appear immediately after completion.

The same day can also be recorded by voice (see section 7). Voice always ends in a confirmation
step, and these buttons remain fully available at all times.

---

## 5. Billing & Payments

Build a deterministic financial engine.

Must support:

- Daily charges.
- Monthly billing cycles.
- Historical price snapshots.
- Previous balance carry-forward.
- Full payment.
- Partial payment.
- No payment.
- Current outstanding.
- Customer statements.
- Payment history.
- Manual payment recording.
- Duplicate-payment protection.
- Payment void/reversal through the correction rules in section 9.

Payments in V1 are recorded manually by the owner:

- `CASH`
- `BANK_TRANSFER`
- `OTHER`

Important:

- An **online payment gateway is not part of V1.** The client has confirmed it is not required.
- Duplicate protection comes from unique operation ids, because two genuine equal payments on the
  same day from the same customer are legitimate and must not be blocked.
- If online payments are ever added later, they plug in behind the existing payment ledger without
  changing it.

---

## 6. WhatsApp / GHL Integration

WhatsApp is in scope.

Expected approach:

```text
OUR APP
   ↓
Reminder / Bill Event
   ↓
Communication Adapter
   ↓
GHL
   ↓
WhatsApp
```

Our application remains the source of truth.

Our app decides:

- Who needs a reminder.
- Current outstanding amount.
- Reminder date.
- Correct customer.
- Message data.

GHL handles delivery/workflow automation.

### Leave This Pluggable

- Do **not** spread GHL-specific code through financial/business logic.
- Create a communication-provider interface/adapter.
- Use a mock/test communication provider until the real GHL setup is available.

Pending external information:

- GHL access.
- WhatsApp setup.
- Business WhatsApp number.
- Meta/number verification if needed.
- Approved templates.
- Exact GHL workflow/integration details.

---

## 7. Voice Input

Some users have limited education or software literacy. Voice is a first-class way to use the
application without typing.

Voice is always **optional**. Every normal control stays available.

### Voice Search

Press the microphone and say the same thing you could type:

- "Show unpaid customers in G-10."
- "Who owes more than five thousand rupees?"
- Roman Urdu / Urdu-English mixed speech where supported.

```text
speech
   ↓
text
   ↓
existing smart search
   ↓
normal database query
```

Voice adds no new search powers. It is another way to type.

### Voice Daily Entry

Say a normal sentence:

- "Essa bought 2 bottles"
- "Essa got three units today"
- "Essa ne 3 kitaabein khareedein"

English, Roman Urdu, and mixed phrasing are the goal, subject to what transcription can actually
support.

The system turns this into a **suggestion**, not a record:

```text
speech
   ↓
text
   ↓
understood as: customer / quantity / unit / date
   ↓
CONFIRMATION SCREEN
   ↓
normal daily service record
```

### Confirmation Is Required

Speech and name matching can be wrong, so nothing is saved until the user agrees:

```text
I heard:

Essa
2 bottles
Today

Charge: Rs. 500

[ CONFIRM ]
[ TRY AGAIN ]
```

Rules:

- If the customer name is unclear, show a short list of choices with large buttons. Never guess.
- If the quantity is missing, ask for it or use the normal `-` / `+` control. Never invent it.
- If the date is unclear, ask. Never invent it.
- If what was said conflicts with the business settings, ask. Never override settings.
- The amount is always calculated by the system, never by the AI.

### What Voice May Do In V1

Allowed:

- Search.
- Record service.
- Skip service for today.

Not allowed:

- Recording payments.
- Changing prices.
- Anything to do with commission or settlements.
- Corrections, voids, or reversals.
- Business settings or user accounts.

Those stay in normal screens. This is a safety choice for V1, not a permanent limit.

### Voice And The Internet

- Voice needs the internet.
- The normal button workflow still works offline, exactly as before.
- If voice is unavailable for any reason, the normal controls remain fully usable.

### Voice Privacy

- Recorded audio is **not** stored by the application.
- Transcripts are used for the confirmation step and not kept.
- Saved records store the ordinary business data, plus a note that the entry came from voice.

### Leave This Pluggable

- Do not hard-code one speech-to-text vendor into business logic.
- Create a speech-to-text interface with a mock/test provider for development and tests.
- The initial implementation is **Groq speech-to-text with `whisper-large-v3`**, chosen because
  accuracy matters more than speed here: the spoken sentences carry customer names and quantities,
  and a mis-heard word becomes a wrong entry suggestion.
- The interface stays in place because the model may change after real-user testing. Quality for
  English, Urdu, Roman Urdu, mixed speech, and Pakistani accents still has to be proven in practice,
  and the confirmation step is required either way.

---

## 8. Offline-First Requirement

The owner asked for maximum practical offline usage, especially daily recording.

Use:

- PWA.
- Service Worker.
- IndexedDB/local persistent storage.
- Persistent sync queue.
- Idempotent server operations.

Previously synced information should remain visible where practical:

- Customer list.
- Customer details.
- Current balance/status snapshot.
- Daily history.
- Payment history.
- Previous statements.
- Outstanding list.
- Dashboard snapshot.
- Basic local search/filtering.

Offline writes should include at least:

- Daily records.
- Quantity changes.
- Skip.
- Manual payment recording.
- Selected customer notes/details if safe.

Show visible status:

- Synced.
- Offline.
- Last synced time.
- Number of pending changes.
- Syncing.
- Needs Attention.

When internet returns:

- Sync queued operations.
- Do not duplicate accepted operations.
- Validate on the server.
- Refresh local authoritative state.
- Detect conflicts instead of silently guessing.

Commission is generated only after the relevant business event reaches and is accepted by the central server.

---

## 9. Data Integrity

Financial/business history must not simply disappear.

For accepted financial/service records:

- No normal hard-delete.
- Use:
  - Correct
  - Void
  - Reverse
- Store:
  - Original record
  - Adjustment
  - Reason
  - Actor
  - Timestamp

Example:

```text
Original:
3 units = Rs.750

Correction:
2 units = Rs.500

Adjustment:
-Rs.250
```

The historical record remains visible.

---

## 10. Commission / Commercial Tracking

The platform is intended to support a commission/licensing model.

Track separately:

### Business Generated

- Recorded service/sale value.
- Billed value.

### Collections

- Payments recorded.
- Outstanding amount.
- Collection percentage.

### Platform Commercial Position

- Commission earned.
- Commission adjustments.
- Commission settled.
- Commission outstanding.

Commission configuration should be flexible because the exact commercial basis may be finalized later.

Possible basis:

- Recorded service value.
- Billed revenue.
- Collected revenue.
- Fixed amount per event.

The client must not be able to edit the protected commission ledger or mark platform commission as settled.

---

## 11. Tenant-Aware Architecture

Build the system so it can support more than one licensed client later.

```text
PLATFORM
   ├── Client A
   ├── Client B
   └── Client C
```

Rules:

- Every client/business record belongs to a tenant.
- Client A cannot see Client B.
- Platform-owner account is separate.
- Do not build a complex SaaS self-service system in V1.
- Make the data model and authorization tenant-aware from Day 1.

---

## 12. Groq Smart Search

Add a small AI-assisted search feature.

Examples:

- "Show unpaid customers in G-10."
- "Who owes more than Rs. 5,000?"
- Roman Urdu queries.

The same search works by voice (section 7): speech becomes text, and the text goes through this
same path.

AI may:

- Interpret natural language.
- Convert it into strict search/filter parameters.
- Interpret a spoken daily-entry sentence into a **suggested** service entry, which a person must
  confirm before anything is saved.

AI may **not**:

- Change payments.
- Change balances.
- Change prices.
- Create/modify billing records.
- Change commission.
- Mutate authoritative financial data.
- Save anything directly, including from voice.

Backend validates filters and performs the real deterministic database query.

If Groq is unavailable:

- Normal filters/search still work.
- Core application continues to work.

---

## 13. Reminder Engine

Implement the requested reminder schedule:

- 1st: bill/statement.
- 4th: reminder.
- 8th: reminder.
- 12th: reminder.
- 15th: final reminder + owner alert.

Rules:

- Always check the latest outstanding balance.
- Partial payment reduces the next reminder amount.
- Fully paid customer stops future outstanding reminders.
- Do not let GHL decide the financial amount.
- Reminder logic remains inside our application.

---

## 14. Build Order

### P0 — Architecture Freeze

- Requirements matrix.
- Roles.
- Tenant model.
- Data model.
- Financial invariants.
- Commission invariants.
- Offline/sync contract.
- Audit/correction rules.
- API boundaries.
- GHL adapter contract.
- Speech-to-text adapter contract.
- Voice authority/confirmation model.
- Acceptance criteria.

### P1 — Backend & Data Foundation

- Application structure.
- Database.
- Migrations.
- Tenant.
- Users.
- Customers.
- Daily service records.
- Audit records.

### P2 — Financial Engine

- Daily charges.
- Price snapshots.
- Billing cycles.
- Statements.
- Carry-forward.
- Payment ledger.
- Outstanding/status calculation.

### P3 — Commercial Tracking

- Generated value.
- Billed value.
- Collected value.
- Outstanding.
- Commission plans/events/adjustments/settlements.

### P4 — Customer & Daily Operations UI

- Customer management.
- Daily register.
- `- / quantity / +`.
- Confirm.
- Skip.
- Next-customer flow.
- Responsive/mobile UI.

### P5 — Offline & Sync

- PWA shell.
- Cache.
- IndexedDB.
- Persistent queue.
- Retry/idempotency.
- Conflict handling.
- Sync status.

### P6 — Statements, Payments & Dashboards

- Statements.
- Manual payments.
- Customer account history.
- Owner dashboard.
- Outstanding list.
- Commercial dashboard.

### P7 — Reminder & Communication Engine

- Reminder schedule.
- Current-balance logic.
- Communication logs.
- Communication adapter.
- Mock provider.

### P8 — Smart Search

- Natural-language interpretation.
- Strict filter schema.
- Server validation.
- Deterministic DB queries.
- Fallback normal search.

### P9 — Voice Input

- Speech-to-text interface.
- Mock/test speech provider.
- Voice search.
- Spoken daily entry into a suggested intent.
- Confirmation screen.
- Ambiguity/missing-value handling.
- Voice provenance on saved records.
- Clean fallback when voice is unavailable.

### P10 — Real GHL / Real Speech Provider

Only when external details are available:

- Connect GHL.
- Test WhatsApp delivery.
- Select and connect a real speech-to-text provider.
- Test real Urdu / Roman Urdu / mixed speech.
- Production activation when credentials/approvals are available.

### P11 — Full E2E Hardening

Test:

- Billing correctness.
- Partial/full payments.
- Carry-forward.
- Offline restart.
- Duplicate sync.
- Lost network responses.
- Duplicate manual payments.
- Reminder cancellation.
- Voice confirmation, ambiguity, and refusal of unsupported voice commands.
- Tenant isolation.
- Client/platform permissions.
- Immutable history.
- AI outage.
- GHL/provider outage.
- Speech provider outage.

### P12 — Deployment & Handover

- Production configuration.
- HTTPS.
- Secrets.
- Scheduled jobs.
- Backup/recovery.
- Final E2E verification.
- Documentation.
- Admin instructions.

---

## 15. What Is Still Pending?

These are **not blockers for core development**.

### Voice

The speech provider is chosen for the first implementation: Groq with `whisper-large-v3`.
Still open:

- Whether real-world quality is good enough for English, Urdu, Roman Urdu, mixed speech, Pakistani
  accents, customer names, quantities, and normal background noise.
- Acceptable cost and latency per spoken entry.
- Whether transcripts may ever be stored (currently they are not).

If quality disappoints, the answer is a different model or provider behind the same interface — not
a redesign.

### WhatsApp / GHL

- Final GHL access.
- WhatsApp connection status.
- Business number.
- Meta/number verification if needed.
- Approved templates.
- Exact workflow/integration details.

### Commercial

- Final commission rate.
- Final commission basis.
- Settlement schedule.

These should be configuration/business decisions, not hard-coded assumptions.

---

## 16. What We Can Build Right Now

Start immediately with:

- Architecture.
- Database.
- Customer management.
- Daily recording.
- Billing.
- Statements.
- Manual payments.
- Outstanding/status logic.
- Dashboards.
- Offline.
- Sync.
- Audit history.
- Commission architecture.
- Reminder engine.
- Mock communication adapter.
- Mock speech provider.
- Groq search.
- Voice search and voice daily entry against mocks.
- Automated tests.

Then plug GHL and the selected speech provider into their defined interfaces later.

---

## 17. Golden Rules

- Keep the UI extremely simple.
- Do not make the user calculate anything.
- Server is authoritative.
- Offline changes are queued safely.
- Never duplicate service/payment operations.
- Never silently overwrite historical financial facts.
- Never let AI mutate authoritative financial data.
- Never save a voice entry without explicit confirmation.
- Never let voice reach payments, prices, commission, corrections, or settings.
- Never guess a customer, quantity, or date from speech.
- Never store raw voice audio.
- Never let GHL become the source of financial truth.
- Never hard-code a speech or AI vendor into core business logic.
- Keep client data tenant-isolated.
- Keep platform commission authority protected.
- Prefer the simplest implementation that satisfies these rules.

---

## 18. Definition of Success

The owner can:

- Log in.
- Add customers.
- Record a full day quickly from a phone, by button or by voice.
- Keep recording if internet temporarily disappears.
- Generate accurate bills automatically.
- Record manual payments.
- See correct outstanding balances.
- Automatically stop/reduce reminders after payment.
- Send bills/reminders through the eventual GHL/WhatsApp integration.
- See clear dashboards and history.
- Correct mistakes without erasing history.
- Use Smart Search without giving AI financial authority.
- Speak a daily entry, check the confirmation screen, and save it — with the same result as
  pressing the buttons.
- Keep working normally when voice or AI is unavailable.

The platform side can:

- Track the agreed commission model.
- Reconcile commission to source business events.
- Keep tenant data isolated.
- Add future licensed clients without rebuilding the product from scratch.
