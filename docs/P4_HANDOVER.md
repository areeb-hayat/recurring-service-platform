# P4 — Handover

Package: **P4 — Customer & Daily UI**. The first frontend. Online-first, nothing committed,
nothing pushed.

**Base commit:** `95a2feb` — "Implement P3 commission engine and commercial tracking".
**Worktree:** `E:\Recurring-Service-Platform-yahya` · **branch:** `yahya` · **upstream:**
`origin/yahya`. Working tree was clean at start.

**Note on package numbering.** P3's §9 recommended reminders and the daily job as P4. The owner
directed the customer & daily UI instead. Reminders are unaffected and unstarted; the numbering
in this document follows the owner's sequence.

---

## 1. Scope implemented

A working daily round for the business owner: sign in, see today's customers, record or skip each
one, and manage the customer list. Nothing beyond that.

| Route | Screen |
| --- | --- |
| `/login` | Sign in. The only unauthenticated screen; no signup, no reset, no customer login. |
| `/today` | **Daily Register** — the primary screen. |
| `/customers` | Customer list, with a filter over the loaded rows. |
| `/customers/new` | Create a customer. |
| `/customers/:customerId` | View a customer; the same screen edits in place. |
| `*` | Redirects to `/today` — the round is what the app is opened for. |

**Deliberately not built:** Service Worker, IndexedDB, outbox, issues store, sync (P5); operating
costs (P6); voice, ElevenLabs, speech (P9); n8n, Evolution API, WhatsApp (P10); reminders,
statements UI, payment UI, commission UI, dashboards, search, AI, a platform-owner frontend,
deployment. No placeholder module exists for any of them.

---

## 2. Stack and dependencies

React 18 + TypeScript + Vite, per the P0 §1.3 freeze. npm (no package manager was previously
established; the repo had no frontend). Node v22.14.0, npm 10.9.2.

**Runtime:** `react`, `react-dom`, `react-router-dom`, `@tanstack/react-query`.
**Dev:** `vite`, `@vitejs/plugin-react`, `typescript`, `vitest`, `jsdom`, `@testing-library/react`,
`@testing-library/dom`, `@testing-library/user-event`, `@testing-library/jest-dom`,
`@types/react`, `@types/react-dom`, `@types/node`.

Four runtime dependencies. No Redux — the only cross-screen state is the session, which is one
context over `localStorage`; server state is TanStack Query's job and there is nothing left for a
store to hold. No UI framework — the whole design is one 500-line stylesheet, and a component
library would have been more code than the components it replaced. No date library, no uuid
library (uuidv7 is fifteen lines in `lib/uuid.ts`), no HTTP client beyond `fetch`.

`vitest` is pinned to `^3` rather than `^2` because Vitest 2 bundles Vite 5 and the resulting
duplicate Vite type trees make `vite.config.ts` fail to typecheck against Vite 6.

**No PWA plugin and no Workbox.** Adding one now would install an offline story P4 does not have.
That is P5's first act.

---

## 3. Backend APIs consumed

| Call | Used by |
| --- | --- |
| `POST /api/v1/auth/login` | sign in |
| `POST /api/v1/auth/refresh` | the HTTP client, on a 401 |
| `POST /api/v1/auth/logout` | sign out |
| `GET /api/v1/tenant/settings` | **new in P4** — see §4 |
| `GET /api/v1/customers` | customer list, daily register — **paged to the end**, see §4a |
| `POST /api/v1/customers` | create |
| `GET /api/v1/customers/{id}` | detail (with `outstanding_minor`, `payment_status`) |
| `PATCH /api/v1/customers/{id}` | edit |
| `GET /api/v1/service/day/{date}` | the day's records |
| `POST /api/v1/service/records` | CONFIRM (`kind: SERVICE`) and SKIP (`kind: SKIP`) |

No field was invented: `frontend/src/api/types.ts` mirrors `serialize_customer`,
`serialize_record` and the request schemas verbatim, and carries a comment saying so.

**The client never sends a `tenant_id`.** There is no parameter in the API layer that could carry
one; the bearer token decides the scope server-side. Two tests assert its absence on login and on
every write.

**No authoritative arithmetic in JavaScript.** The client renders `charge_minor`,
`outstanding_minor` and `payment_status` exactly as received. `lib/money.ts` formats an integer for
display by splitting its digits — it never divides, multiplies or adds. Quantity arithmetic (the
`+`/`−` stepper) runs on scaled integers in `lib/decimal.ts`, so `0.1` stepped ten times is exactly
`1`, and a value never becomes a JS `number`.

---

## 4. Backend change — one route

**Prefer zero backend changes was the rule, and exactly one was needed.**

**The gap.** The tenant's own display configuration — `currency`, `currency_exponent`,
`unit_label` — was reachable only by serializing a *customer* or a *service record*. A tenant with
no customers and no records has neither. That is the state every tenant is in immediately after
provisioning, which is exactly when its owner first opens "Add customer" — and that form must
label the price field with the currency, label the quantity field with the unit, and convert the
typed price to minor units at the tenant's exponent. A client that assumed any of the three would
be hard-coding business configuration that P0 §4 put on the `tenant` row precisely so it would not
be hard-coded.

The same gap runs the other way for dates: `GET /service/day/{date}` requires a date in the path,
but the tenant's timezone lives on the tenant row, so the client could only get a business date by
first inventing one. The response does carry the authoritative `business_date`, so a
guess-then-correct loop was possible — but it means the first request of every session is for a
date the client made up, which is the thing P0 R4 exists to prevent.

**The addition.** One read-only route:

```
GET /api/v1/tenant/settings          capability: dashboard:read
→ { name, currency, currency_exponent, unit_label, timezone,
    business_date, default_quantity, default_unit_price_minor }
```

Backward-compatible in every direction: no schema change, no migration, no new table, no new
model, no new capability. `dashboard:read` is the existing P0 §3.2 capability for reading the
business's own top-level state, and only `OWNER_ADMIN` holds it — the frozen capability map is
untouched. The tenant comes from the authenticated context; there is no parameter to abuse.

The response is exactly the eight fields a P4 screen renders. Cycle configuration, the reminder
schedule, the tenant status and the tenant id are **not** returned, and a test pins the key set so
a later package cannot quietly widen it.

`default_quantity` and `default_unit_price_minor` are there because a new customer should inherit
the business's normal terms (P0 §4) rather than a number chosen in the form.

**Files:** `backend/app/tenancy/settings.py` (new), one router and one `include_router` line, plus
`("GET", "/api/v1/tenant/settings")` added to the isolation suite's `EXERCISED` inventory so the
existing SEC-3/4/6 guards cover it automatically.

**Tests:** `backend/tests/test_tenant_settings.py`, 9 tests — the configuration it returns, the
exact key set, no tenant identifier leaked, quantity as a string, **the business date is the
tenant's timezone's and not the caller's** (two tenants, one instant, two different dates), each
tenant sees only its own, 401 unauthenticated, 403 for a platform token, and 405 on every write
method.

---

## 4a. Review finding — daily-register customer completeness

Found in the P4 final review, fixed there.

**The contract.** `GET /customers` takes `limit` (default **100**, capped at **500** by the route's
`Query(le=500)` and again by `min(limit, 500)` in `list_customers`) and `offset`. It returns
`{"items": [...]}` — **no total, no cursor, no `has_more`**. The only end-of-list signal is a page
shorter than the requested limit.

**The defect.** The register issued a single request with `limit=500` and stopped. A tenant with
more than 500 active customers would have had everyone past the 500th silently absent from the
round — and because the response carries no total, nothing on screen would have said so. The same
applied to the customer list, whose "search this list" filter was therefore quietly incomplete.

**The frontend fix.** `listAllCustomers()` in `api/customers.ts` walks `offset` in 500-row pages
until a short page arrives, de-duplicating by id. Both the register (`status=ACTIVE`) and the
customer list use it. A failure mid-walk rejects rather than returning a short list — a partial
round presented as complete is exactly the failure being prevented. Termination is guaranteed:
`offset` strictly increases against a finite table.

**One backend line.** `list_customers` now orders by `(name, id)` rather than by `name` alone.
Offset pagination is only sound over a *total* order, and `customer.name` is not unique: with ties
at a page boundary the relative order is unspecified, so a walker could in principle skip or repeat
a row. The contract is unchanged — customers still come back in name order — the ordering is merely
made total, so the pagination it already offered is correct by specification rather than by luck.

**Honest note.** The new backend tests also pass against the old `ORDER BY name`: at this data size
PostgreSQL happens to return ties in id order anyway. That is a property of the current plan, not a
guarantee, which is the reason to pin it. Those tests record the specified behaviour going forward;
they are not evidence that the old query was observably broken. The *frontend* defect, by contrast,
was real and is demonstrated — `completeness.test.tsx` fails against the single-request version.

**Invariant now tested end to end:** N eligible active customers on the server → N represented by
the register, before subtracting those already recorded for the business day.

---

## 5. Authentication behaviour

Short-lived JWT access token plus opaque refresh token, exactly P0 §3.3. Stored in `localStorage`
behind `auth/session.ts`, which is the only module that knows where they live and which degrades to
an in-memory copy when a browser blocks site data.

- **Login** — `POST /auth/login`, session stored, redirect to `/today`. A failed login shows
  "Email or password is not correct." and never the server's `detail`.
- **401 on any authenticated request** — the HTTP client refreshes **once**, then replays the
  *identical* request: same method, same URL, same body, so a mutation keeps its `operation_id`
  across the refresh and cannot double-apply. Concurrent 401s share one refresh rather than racing.
- **Refresh fails** — the session is cleared, the app falls back to the login screen and shows
  "Your session has ended. Please sign in again."
- **Logout** — cleared locally first, then `POST /auth/logout` revokes the refresh token. A failed
  logout call never strands someone in a session they asked to leave.

`RequireAuth` is a routing convenience only. It mirrors client-side session state so the owner sees
a login form instead of a wall of 401s; nothing about backend authentication was weakened, and
every request still carries or fails on its own token.

No signup, no password reset, no customer login, no operator workflow, no platform-owner UI.

---

## 6. `operation_id` behaviour

Generated **once, at the moment of user intent** — the tap on Confirm, Skip today, Save customer or
Save changes — by `createOperation()` in `frontend/src/api/operation.ts`, which produces the P0 §7.2
envelope shape (`operation_id`, `op_type`, `payload`, `client_created_at`) with a **uuidv7** so the
envelope order is the order the user acted.

`usePendingOperation` holds one envelope and distinguishes the two kinds of failure P0 §7.3
separates:

- **A verdict** (4xx with the error envelope) ends the operation. The server answered; asking again
  with the same id would only replay the same answer.
- **A transport failure** (dropped connection, timeout, 5xx) is *not* a verdict. The envelope is
  **kept**, and Retry resends it byte-for-byte. If the first attempt did reach the server, the reply
  is `DUPLICATE` and nothing is recorded twice.

A new id is never minted because a fetch failed, a request timed out, or Retry was pressed — nor
because the access token expired mid-write. A 401 on a mutation refreshes once and replays the
*identical* body: same `operation_id`, same everything, so the server's register collapses the two
attempts into one logical operation. Concurrent 401s share a single refresh rather than starting a
storm, and each in-flight operation is replayed under its own original id.

**The quantity control is disabled while an operation is unresolved.** Editing the quantity and
pressing Retry would send a different payload under the same `operation_id`, which SYN-14 correctly
refuses — so the UI offers Retry (the same intent) or Discard (start again with a new one), and
never a silent mutation in between. Two tests assert the id and the whole body are identical across
a retry, for both a service record and a customer update.

**What P5 inherits.** The envelope is a plain serialisable value with P0 §7.2's field names, and the
state machine already has the `idle` / `sending` / `unresolved` shape the outbox needs. P5 writes
the envelope to IndexedDB before the network call and moves it to `issues` on a `REJECTED` or
`CONFLICT` verdict. Neither the envelope shape nor the write path has to change for that. **No
outbox, no IndexedDB and no sync code was written now.**

---

## 7. The Daily Register

One customer at a time, in a card sized for a phone held in one hand at somebody's door:

```
                 3 of 24
             Ayesha Khan
            C-001 · G-10

      [ − ]    2.000 bottle   [ + ]

          [    C O N F I R M    ]
          [     Skip today     ]
               Leave for later
```

- The customer's name is the largest thing on the screen.
- The quantity starts at that customer's `default_quantity` from the server.
- `−`/`+` step by whole units; the field itself accepts up to three decimal places, because the
  column is `NUMERIC(12,3)` and half a unit is a real delivery. `1.2345` is refused with a plain
  sentence and Confirm is disabled.
- **Confirm** posts `kind: SERVICE` with the quantity. **Skip today** posts `kind: SKIP` with no
  quantity — a skip is a real record, not an absence, which is why it is a button rather than
  simply moving on.
- `service_date` is **omitted** on both, so the server resolves the tenant's today. The client
  never states a business date on a write.
- After a save the card stays on that customer showing what was recorded, and the person moves on
  with **Next customer**. Advancing automatically would put one entry's confirmation over the next
  customer's name, which is how the wrong person gets recorded twice.
- Below the card: "Still to do (N)" and "Done (N)", both tappable, because a real round is not a
  straight line and "who is left" should not need counting.

The register is composed from three server reads — settings, active customers, the day's records —
joined by customer id for display. The join produces "done" and "pending" and nothing else; it
derives no money and no due state.

---

## 8. Error UX

Every backend code is mapped to one sentence in `api/errors.ts`. No stack trace, no database text,
no accounting jargon, and the server's raw `detail` is never rendered.

| Code | What the person reads |
| --- | --- |
| `UNAUTHENTICATED` | "Your session has ended. Please sign in again." (on the login form: "Email or password is not correct.") |
| `VALIDATION` | "Please check the highlighted fields and try again." — plus per-field messages from `field_errors` |
| `SERVICE_ALREADY_RECORDED` | "Today is already recorded for this customer. Reload to see what was saved." |
| `CYCLE_ROLLOVER_REQUIRED` | "The current billing period has ended. Close it before recording more work." |
| `CUSTOMER_CODE_TAKEN` | "That customer code is already in use. Choose another." |
| `ROW_VERSION_CONFLICT` | "Someone else updated this customer while you were editing. Reload and try again." |
| `IDEMPOTENCY_KEY_REUSE` | "This action was already sent with different details. Reload and start again." |
| network / 5xx | "Could not reach the server…" / "The server had a problem. Nothing was lost — try again." |

**No conflict is ever silently overwritten.** A `SERVICE_ALREADY_RECORDED` or
`ROW_VERSION_CONFLICT` is shown, the form stays as it was, and no Retry button is offered — because
retrying the same id would replay the same verdict. Offline conflict handling and the durable
`issues` store are P5.

Customer edits send `expected_row_version` from the loaded row, so a concurrent change produces a
`ROW_VERSION_CONFLICT` instead of a silent overwrite.

---

## 9. Accessibility decisions

Structural, not decorative:

- **Tap targets.** 48px floor everywhere; the stepper buttons are 64px and Confirm is 72px.
- **Every control has a name.** The `−`/`+` buttons are labelled "Decrease bottle" / "Increase
  bottle" using the tenant's own unit label, and the quantity field has a visually-hidden label.
  The whole test suite drives the UI by accessible role and name, so a control losing its name
  fails a test rather than a review.
- **Live regions.** Errors are `role="alert"` (assertive); loading, success and progress are
  `role="status"` (polite), so a screen reader is not interrupted mid-sentence by a spinner.
- **Invalid input** sets `aria-invalid` and points `aria-describedby` at the message. Colour is
  never the only signal — every error and success state carries words.
- **Focus is never removed**, only restyled: a 3px `:focus-visible` ring with an offset.
- **Forms** use real `<label for>` associations throughout, `noValidate` so the app's own messages
  are what people read, and appropriate `autoComplete` / `inputMode` (`decimal` for quantity and
  price, so a phone shows a numeric keypad).
- **Mobile-first layout**: bottom navigation within thumb reach on a phone, moving to the top on a
  screen wider than 720px. Content is capped at 720px on desktop rather than stretched.
- **Theme-aware** via `prefers-color-scheme`, with contrast held in both; `prefers-reduced-motion`
  is honoured.
- **No accounting jargon** anywhere in the interface (P0 §8.7). "Recorded 2 bottle." — not
  "transaction posted".

---

## 10. Verification

Run once each, at the end.

| Check | Result |
| --- | --- |
| Frontend tests (`npx vitest run`) | **66 passed**, 0 failed, 6 files |
| TypeScript (`tsc --noEmit -p tsconfig.json`) | **clean**, no errors |
| Production build (`vite build`) | **succeeded** — 102 modules, `index.js` 232.78 kB (74.13 kB gzip), `index.css` 6.27 kB (1.84 kB gzip) |
| Backend suite (PostgreSQL 16) | **687 passed**, 0 failed — 668 from P1–P3 unchanged, plus 19 new (9 tenant settings, 10 pagination) |
| `git diff --check` | clean |

The backend suite was run because backend source changed. `strict` TypeScript is on, including
`noUncheckedIndexedAccess`, `noUnusedLocals` and `verbatimModuleSyntax`.

**Frontend test coverage** (48 tests in 4 files):

| File | Tests | Covers |
| --- | --- | --- |
| `lib/decimal.test.ts` | 11 | exact quantity arithmetic, no float drift, money display, uuidv7 |
| `auth/auth.test.tsx` | 9 | login success/failure, no `tenant_id` sent, authenticated routing, bearer header, refresh-and-replay on 401, session expiry to login, logout revocation (token present, no stale auth header), logout when revocation fails |
| `auth/refresh-replay.test.tsx` | 5 | **a mutation meeting an expired token**: one refresh, identical replay body, same `operation_id`, refreshed bearer, one logical operation; no second retry; no replay when the refresh fails; concurrent 401s share one refresh and each keeps its own id |
| `daily/register.test.tsx` | 19 | server-chosen business date, pending/done split, stepper, decimal quantity, CONFIRM payload, SKIP payload, next-customer, **stable `operation_id` across retry**, locked control while unresolved, discard, conflict and rollover rendering, day-load failure, **"Leave for later" writes nothing** |
| `daily/completeness.test.tsx` | 9 | **pagination completeness**: pages past the cap, exact-multiple boundary, single-page short-circuit, limit/offset/status on every page, de-duplication, failure not silently truncated; register shows all 1200 across three pages; done/pending subtraction across pages; customer list paged in full |
| `customers/customers.test.tsx` | 13 | list, filter, empty state, tenant defaults, create payload in minor units, duplicate code, invalid quantity refused, detail with server-derived balance and status, no delete affordance, patch with `expected_row_version`, immutable code, row-version conflict, **stable `operation_id` across retry** |

HTTP is mocked at the `fetch` boundary by a small hand-rolled harness (`src/test/http.ts`) that
records every request, so a test can assert on the *second* attempt's body as easily as the first —
which is what the `operation_id` guarantee actually requires. A service-worker mocking library was
not added: the harness is forty lines and has no transport of its own to go wrong.

---

## 11. Owner decisions recorded during P4

Recorded here, with the narrowest possible edits elsewhere. None of these are implemented in P4.

### Collaboration
Yahya's development worktree is `E:\Recurring-Service-Platform-yahya`, branch `yahya`, pushing to
`origin/yahya`. Yahya development is never pushed directly to `origin/main`. Areeb's worktree and
branch are not modified from here. Integration into `main` is a separate, deliberate action.
→ recorded in `CLAUDE.md` § "Collaboration".

### Production account model
No shared generic privileged login is to be created. The client/business owner holds their own
`OWNER_ADMIN` account; Yahya and Areeb each hold their own `PLATFORM_OWNER` identity. Equal platform
capability, separate identities, so audit events stay attributable. No real production user or
password is hard-coded in source, migrations, seeds or fixtures. P4 needs no platform-owner
frontend and has none. → recorded in `CLAUDE.md` § "Production accounts".

### Voice / speech-to-text — provider changed
The initial STT implementation for **P9** is now **ElevenLabs `scribe_v2`**, server secret
`ELEVENLABS_API_KEY`, superseding Groq `whisper-large-v3`. `SpeechToTextProvider` remains the port
and remains replaceable; raw audio is never persisted; the button workflow stays authoritative and
always available. Groq may still be used later for *constrained text intent interpretation* — not
for transcription. **Voice is still P9 and nothing was implemented in P4.**
→ `CLAUDE.md` speech-to-text rule rewritten; `docs/P0_ARCHITECTURE_FREEZE.md` §8.5 carries a dated
amendment block (the superseded choice is kept beside it, because the *reasoning* — accuracy over
latency for utterances carrying names and quantities — is unchanged), and §16's closed-decision
note and D12 row name the new model. Nothing else in P0 was touched.

### Operating-expense feature — approved future requirement
An owner-facing **Operating Costs** area that tracks infrastructure and provider operating expenses
**separately from P3 commission accounting**: estimated monthly cost from measured usage ×
versioned provider rates, actual invoice amounts logged against it, estimated-vs-actual with
variance, monthly history, low/normal/high usage projections, covering hosting, database, backups,
domain, voice, LLM, messaging and other configured costs. **It must never mix into commission.**
Target phase: primarily **P6**, with real usage feeds arriving in P9/P10/P12. Not implemented, and
no placeholder table, model or route was created.

### WhatsApp — planned P10 direction
n8n workflow automation + Evolution API + PostgreSQL + Redis on the client-owned Hostinger VPS. An
initial Baileys / WhatsApp Web route may be used as a low-cost starting path; it is **unofficial and
must not be represented as guaranteed long-term**, and the migration path to the Meta official
WhatsApp Cloud API must be preserved. Not implemented; the `CommunicationProvider` port still has no
adapter.

### Domain
Domain acquisition and ownership are intentionally unresolved: the client may supply an existing
domain, or Yahya/Areeb may purchase and configure one and later hand ownership and billing to the
client. This blocks nothing.

---

## 12. Defects and clarifications found

**D1 — the tenant could not read its own configuration.** The gap in §4, and the only backend
change P4 made. Worth calling a defect rather than a missing feature: P0 §4 says currency, unit
label and timezone are per-tenant configuration "never a code constant", and until P4 the only way
for any client to obtain them was to already have business data.

**D2 — the daily register loaded only the first page of customers** (found in the P4 review,
fixed there). Full account in §4a. A real defect: a tenant past 500 active customers would have had
people silently missing from the round.

**D3 — `list_customers` ordered by a non-unique key**, making its own offset pagination sound only
by accident (§4a). One line: `ORDER BY (name, id)`.

**D4 — no defect was found in P1, P2 or P3 behaviour.** Every route P4 consumes behaved as its
handover describes. The error envelope is uniform enough that the whole error map is one table.

**C1 — `GET /service/day/{date}` is the day's *records*, not the day's *queue*.** P0 §15 calls it
"the daily register queue", but it returns `daily_service_record` rows, so it lists who is *done*,
not who is *due*. The register composes the queue itself from `GET /customers?status=ACTIVE` joined
to that day. That is a display join over two server facts and needs no backend change, and it is
noted here so a later package does not read P0 §15 and expect a queue endpoint to exist.

**C1b — "Leave for later" is purely local.** Verified in the review: it calls the same `onNext`
handler as "Next customer", which only moves a client-side cursor. It creates no service record, no
`SKIP`, no `operation_id`, no request of any kind, and therefore no ledger entry. The customer stays
pending and comes round again. Kept as convenience behaviour.

**C2 — a customer code cannot be changed.** `UpdateCustomerRequest` has no `code` field. The form
disables the input and says so, rather than appearing to offer an edit that would be dropped.

**C3 — there is no customer name search endpoint.** The list filter therefore runs over the rows
already loaded, at the backend's own 500-row cap. Honest at this scale; a real search is P0 §12.1's
package.

---

## 13. Risks

**R1 — closed in the P4 review.** The list and the register now walk the pagination to its end
(§4a). What remains is a *performance* observation rather than a correctness one: a very large
tenant costs one request per 500 customers on load, and the register renders every row. Neither is
a problem at this scale, and both are addressable without a contract change if either becomes one.

**R2 — `localStorage` holds the refresh token.** Standard for a bearer-token SPA and the P0 §3.3
model, but it is readable by any script that gets onto the origin. The mitigations that exist are
real (short access-token life, server-side revocable refresh token, one origin, no third-party
scripts, no CDN); the mitigation that does not exist is XSS-proofing, which is a deployment concern
(CSP) rather than a code one. Worth a decision before P12.

**R3 — no E2E tests.** P0 §1.3 freezes Playwright for E2E, and P5 makes it genuinely necessary
because offline/online toggling is only testable there. P4 deliberately did not add a Playwright
harness for four screens that unit tests already drive by accessible role.

**R4 — the register's client-side join assumes both reads are of the same day.** They are, because
the day read is keyed by the server's `business_date` and both are invalidated together. A future
screen that lets the owner browse another date must not reuse this hook without revisiting that.

**R5 — no load has been measured** (P3 R2, unchanged). The register issues three reads on mount.

**R6 — A-SEC-9 (CI secret scanning) is still open**, from P1 through P4. Still cheap to close.

---

## 14. Recommended next package

**P5 — offline and sync.** It is the reason P4 was built the way it was: the operation envelope,
the unresolved-operation state machine and the single HTTP boundary are all in place, and P5's job
is to persist envelopes rather than to reshape the write path.

In order: the Service Worker and app-shell caching; the four IndexedDB stores (`outbox`, `issues`,
`snapshot`, `meta`); writing the envelope to `outbox` **before** the network call; the bulk
`POST /sync/operations` endpoint on the backend and the four verdicts; promotion of `REJECTED` and
`CONFLICT` into the durable `issues` store; the visible sync states from P0 §7.5; and Playwright,
because offline/online toggling is only honestly testable there.

Two things P5 must not lose: the `sync_operation` register is never pruned, and a conflicting
operation is never auto-resubmitted unchanged.

---

## 15. Files

**Created**

```
frontend/package.json  package-lock.json  tsconfig.json  vite.config.ts
         index.html  .env.example
frontend/src/main.tsx  App.tsx  providers.tsx  styles.css
frontend/src/api/       client.ts  errors.ts  operation.ts  types.ts
                        auth.ts  customers.ts  service.ts  tenant.ts
frontend/src/auth/      session.ts  AuthContext.tsx  LoginPage.tsx
                        auth.test.tsx  refresh-replay.test.tsx
frontend/src/components/ AppShell.tsx  RequireAuth.tsx  Feedback.tsx  QuantityStepper.tsx
frontend/src/customers/ CustomerListPage.tsx  CustomerCreatePage.tsx
                        CustomerDetailPage.tsx  CustomerForm.tsx  customers.test.tsx
frontend/src/daily/     DailyRegisterPage.tsx  ServiceCard.tsx
                        useRegister.ts  usePendingOperation.ts
                        register.test.tsx  completeness.test.tsx
frontend/src/lib/       decimal.ts  money.ts  uuid.ts  decimal.test.ts
frontend/src/test/      setup.ts  http.ts  fixtures.tsx
backend/app/tenancy/settings.py
backend/tests/test_tenant_settings.py  test_customer_pagination.py
docs/P4_HANDOVER.md
```

**Modified**

```
backend/app/api/routes.py          + tenant_router and GET /tenant/settings
backend/app/main.py                registers tenant_router
backend/app/customers/commands.py  list_customers orders by (name, id) — review §4a
backend/tests/test_tenant_isolation.py  + the new route in EXERCISED
CLAUDE.md                          phase, Collaboration, Production accounts,
                                   frontend code boundaries, speech-to-text rule
docs/P0_ARCHITECTURE_FREEZE.md     §8.5 STT amendment block; §16 closed-decision
                                   note and the D12 row
.gitignore                         node_modules/, frontend/dist/, frontend/.vite/
```

`docs/P0_INVARIANTS_AND_ACCEPTANCE.md` was **not** modified — P4 found no defect in it.

---

## 16. Git state

Branch `yahya`, base `95a2feb`, upstream `origin/yahya`. **Nothing was committed and nothing was
pushed**, as instructed. `git diff --check` reports no whitespace errors. No secret and no `.env`
value is in the diff; `frontend/.env.example` contains only a blank API base URL and a localhost
dev-proxy target. All changes are in the working tree.

`.venv/` was created at the worktree root and `backend[dev]` installed into it; it is already
covered by the existing `.gitignore` entry and is not part of the diff.
