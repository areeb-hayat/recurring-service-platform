# P5 — Handover

Package: **P5 — Offline & Sync**. The app now works without a network. Nothing committed,
nothing pushed.

**Base commit:** `c383af2` — "Implement P4 customer and daily workflow frontend".
**Worktree:** `E:\Recurring-Service-Platform-yahya` · **branch:** `yahya` · **upstream:**
`origin/yahya`. Working tree was clean at start.

---

## 1. Scope implemented

The frozen offline-first system from P0 §7, and only that:

```
Browser PWA
├── Service Worker      app shell + static build assets (no API responses, ever)
├── IndexedDB           outbox · issues · snapshot · meta   (one database per tenant)
└── sync engine         push queued operations · pull server changes
        │
        ▼
POST /api/v1/sync/operations     GET /api/v1/sync/changes?since=<row_version>
```

**The V1 offline write scope is CONFIRM and SKIP.** `service.record` and `service.skip`, nothing
else. Payments, corrections, voids and customer create/edit remain online-only operations; they use
the same envelope and would be a registry entry rather than a redesign. See §12 for the one
documentation clarification this required.

**Deliberately not built:** offline payments, offline customer create/edit, offline corrections,
statements/dashboard UI, operating costs (P6), reminders, n8n/Evolution/WhatsApp (P10), search, AI,
voice (P9), a platform frontend, deployment. No placeholder module exists for any of them.

---

## 2. PWA and Service Worker

`vite-plugin-pwa` in `generateSW` mode (Workbox), configured in `frontend/vite.config.ts`.

| Decision | Why |
| --- | --- |
| **Precache the app shell and build assets only** — `runtimeCaching: []` | A cached `GET /customers/{id}` looks exactly like a fresh one while carrying last week's outstanding balance. A stale balance presented as current is worse than no balance (SYN-9). The SW is never a data layer. |
| `navigateFallbackDenylist: [/^\/api\//]` | An API request can never be answered with `index.html`. |
| `clientsClaim: true`, `skipWaiting: false` | The *first* worker claims the page that registered it, so **one online load** is enough to make the app openable offline. A *later* worker does not skip waiting, so an update cannot swap the code under somebody mid-round; it takes over on the next natural load. |
| `registerType: "prompt"`, registered from `main.tsx` with no prompt UI | Same reason: an auto-updating worker reloads the page as soon as a new build lands. Nothing is at risk either way (the outbox is durable) but an interrupted round is still rude. |

A valid `manifest.webmanifest` is generated (name, short name, standalone, portrait, theme colour,
192/512/maskable icons). The icons in `frontend/public/` are a plain generated placeholder mark —
replacing them is a file swap, not a code change.

**Build output:** 12 precache entries, 265 KiB; `dist/sw.js` + `dist/workbox-*.js`. A newly
installed device still requires one online load: nothing is bundled that could substitute for it.
No secret or config value is embedded — `.env.example` carries only an empty API base URL and a
localhost dev-proxy target, and the build reads no other `VITE_*`.

---

## 3. IndexedDB

`idb` (the thin typed wrapper, per the freeze). One database **per tenant**:
`rsp-sync-v1-<tenant_id>` — see §10.

| Store | Key | Contents |
| --- | --- | --- |
| `outbox` | `operation_id` | the whole envelope, plus `seq` (creation order), `attempt_count`, `last_attempt_at`, `next_attempt_at`, `last_error`, and a small display `context` |
| `issues` | `operation_id` | envelope, context, `verdict`, machine-readable `error`, `server_state`, `created_at`, `resolved_at` |
| `snapshot` | `"<entity>:<id>"` | server-authoritative reads: `tenant` settings, `customer`, `daily_service_record` — with `row_version` |
| `meta` | key | `sync_cursor`, `feed_version`, `last_synced_at`, `business_date`, `next_seq`, `tenant_id` |

Indexes: `outbox.by_seq`, `issues.by_created_at`, `snapshot.by_entity`.

**Two writes are atomic, and they are the two that matter** (`frontend/src/sync/stores.ts`):

- `promoteToIssue` — deleting from `outbox` and writing to `issues` happen in **one** transaction.
  There is no instant at which a REJECTED or CONFLICT operation exists in neither store, so a tab
  closing between the two writes cannot lose the problem (SYN-6).
- `applyChanges` — a page of snapshot rows and the cursor that says they arrived are written in one
  transaction. The cursor therefore can never be ahead of the data: a crash mid-page rolls back
  both and the page is simply fetched again.

`settleOperation` (APPLIED/DUPLICATE) is the third: it removes the outbox entry and writes the
server's returned record into the snapshot together, so a confirmed customer never flickers back to
"still to do" — which is how somebody gets recorded twice.

**Snapshot scope.** Service records are retained for **the business date the server most recently
stated** — the one day the register renders — and no other. There is no N-day window: a retention
horizon in days would be product policy nobody asked for.

An offline device keeps using its cached business date, so its current round stays available for as
long as it stays offline; the rule only bites when a pull brings a newer date back. Records for
other dates are still *seen* — the cursor advances past them normally, so nothing is skipped — they
are simply not stored, because storing them would claim an offline availability no screen offers.
Needs Attention needs none of them: an issue carries its own intent and the `server_state` it was
given.

Cleanup is snapshot-only. `pruneServiceRecords` touches neither `outbox` nor `issues`, which are not
caches; unresolved work is never collateral of a cache sweep, and a test asserts it.

One consequence worth stating: a queued operation for *yesterday* stops appearing in today's
"Waiting to sync" list once the date rolls over, because that list is grouped by the current
business date. It is still counted in "N changes waiting" and still pushed, carrying its own
`service_date`.

Nothing beyond the P0 §3.3 auth design is stored: tokens stay in `localStorage` as in P4;
IndexedDB holds no credential.

---

## 4. One write path

P4 had `usePendingOperation` holding an envelope in memory. P5 replaces that on the register with
the outbox, so there is no "online mutation" and "offline mutation" split at all:

```
USER TAPS CONFIRM / SKIP
   → operation_id generated once (uuidv7), at the moment of intent
   → envelope written to IndexedDB outbox   ← awaited; durable before any fetch
   → if online, sync immediately; if not, it waits
   → server verdict
   → local stores and UI updated
```

The `await` on the durable write is the guarantee: `enqueue()` does not resolve until the envelope
is in IndexedDB, and only then is the network involved (SYN-5). **A fetch having been attempted
removes nothing** — only a verdict does. That is why an apparently-online action survives a lost
response, a closed tab or a pulled plug exactly as an offline one does.

`usePendingOperation` is unchanged and still used by customer create/edit, which are online-only.

**What the card says.** Once queued: *"2.000 bottle saved on this device — waiting to sync."* Never
"Recorded". "Recorded" is a statement about the server, and the server has not spoken. The round
list shows three groups — **Still to do**, **Waiting to sync**, **Done** — and only a server record
is ever called recorded.

---

## 5. Push sync — `POST /api/v1/sync/operations`

`backend/app/sync/operations.py`, a thin dispatcher over the existing domain:

- **Same validation as online, structurally.** `RecordServiceRequest` now *extends*
  `ServiceOperationPayload` (`app/sync/envelope.py`), so there is one definition of what a CONFIRM
  contains and both transports validate against it. SYN-8 is true by construction rather than by
  two copies staying accidentally identical.
- **Same command, same register.** `record_service` through `execute_idempotent`. No business logic
  was duplicated into `app/sync/`.
- **Own transaction per operation.** `execute_idempotent` commits; a domain failure rolls back only
  that operation. One bad entry cannot undo the entries beside it.
- **Provenance.** `source = SYNC` on the record and `AuditSource.SYNC` on the audit event; the
  online route still records `ONLINE`. That is the only difference between the two paths.

| Verdict | When | What happens |
| --- | --- | --- |
| `APPLIED` | first acceptance | record + ledger + commission + register row, one transaction |
| `DUPLICATE` | `(tenant_id, operation_id)` already registered | no side effect; the **stored original result** is returned |
| `REJECTED` | validation / authorization / business refusal | no effect, and the operation is **not** registered — a transient rejection never permanently burns an `operation_id` |
| `CONFLICT` | the `(customer, service_date)` active slot is taken, or `IDEMPOTENCY_KEY_REUSE` | no effect; `server_state` carries the authoritative record (or, for key reuse, the first acceptance's result) |

An unexpected (non-`DomainError`) exception is deliberately **not** caught: that is a defect, not a
verdict, and swallowing it as REJECTED would make a server bug permanently terminal for a device's
queued work. It propagates, the request fails, and the client keeps the whole batch queued.

`op_type` and `kind` must agree, and the online route now registers under the same `op_type` a
queued envelope would use (`service.skip` for a SKIP), so a retry that changes transport is
recognised as the same request rather than refused as key reuse.

**Client side** (`frontend/src/sync/engine.ts`): batches of 50, pushed in `seq` order. `APPLIED` and
`DUPLICATE` drain; `REJECTED` and `CONFLICT` promote to `issues` atomically. A network error,
timeout or 5xx is not a verdict — the entry stays queued with bounded exponential backoff (2 s
doubling to a 5 minute ceiling, capped exponent, never zero, never a busy loop). Sync is triggered
on start, on `online`, on `visibilitychange`, immediately after an enqueue, and from the **Sync
now** button.

---

## 6. Change feed — `GET /api/v1/sync/changes?since=<row_version>`

`backend/app/sync/changes.py`. The cursor is the **shared `row_version` sequence** (P0 §6), never a
timestamp. Every value comes from one sequence and is unique across all tables, so
`ORDER BY row_version` is a total order with no tiebreaker needed.

- Tenant-scoped from the authenticated context; a platform principal is refused 403.
- `limit + 1` is read per entity so "is there another page" is answered by data; the extra row is
  never returned and never advances the cursor.
- `cursor` is the `row_version` of the **last row actually handed over** — never the sequence head,
  never a page boundary the caller did not receive. Replaying an older cursor re-delivers rows the
  client already has, which is harmless because rows are applied by identity: a superset, never a
  gap (SYN-10).
- **No tombstones, and none needed.** Nothing is hard-deleted (FIN-12/AUD-7); a record leaves the
  active set by changing status, which arrives as an ordinary update.
- **`head`** is the tenant's greatest current `row_version`. A first-time device reads `head`,
  *then* seeds its snapshot from the ordinary read routes, then continues the feed from `head` —
  so anything written in between has a higher version and still arrives. Tenant-scoped on purpose:
  the sequence's own `last_value` would be cheaper and would tell every tenant how much every other
  tenant writes.

**Entities:** `tenant`, `customer`, `daily_service_record`. `payment`, `statement` and
`ledger_entry` carry `row_version` too and join the list in the package that builds a screen for
them; streaming them now would put financial rows on devices with nothing to render them and every
temptation to add them up. `SYNC_FEED_VERSION` exists so that admitting one is safe — a client whose
stored feed version differs **discards its cursor and resynchronises from zero**, which is the only
way a newly added entity's older rows can reach a device already past them. That resync clears the
snapshot only; the outbox and issues are not caches and are never touched.

Commission never appears at any version: those tables carry no `row_version` at all, so there is no
mechanism by which a tenant could pull one. A test asserts it.

**Commit-order safety (SYN-10).** `row_version` is allocated by a non-transactional `nextval`
*inside* a transaction, so allocation order and commit order can disagree — see D4 in §12 for the
defect and `app/sync/serialization.py` for the tenant-scoped advisory lock that closes it. The short
version: within a tenant, feed-visible writes now allocate in the same order they commit, so the
committed versions are always a prefix of the allocated ones and a cursor can skip nothing.

**Client apply.** `tenant` settings are written by the direct `GET /tenant/settings` read on every
sync rather than from the feed — the business date changes daily without the tenant *row* changing,
so waiting for a version bump would leave an online device believing in yesterday.

---

## 7. Issues / Needs Attention

Route `/attention`, `frontend/src/sync/IssuesPage.tsx`. Each entry shows the customer, the intended
action and quantity, the business date, one plain sentence for the error (from the same message map
every other screen uses — the server's raw `detail` is never rendered), and, for a conflict, what
the server actually holds.

**The only action is "I have reviewed this".** No resend, no merge, no overwrite. Resending the
same operation replays the same refusal; sending a changed one under the same `operation_id` is
refused outright (SYN-14). If a corrected entry is genuinely needed it is a **new deliberate act
with a new `operation_id`**, made on the register like any other entry — nothing here creates one.

Resolving stamps `resolved_at` and **keeps the row**; deleting it would make the review
indistinguishable from the entry never having existed. Reviewed issues stay behind a "Show
reviewed" toggle.

A conflict's `server_state` is also written into the snapshot, so the customer correctly shows as
**Done** on the round — the work *is* recorded, just not by this device's operation.

**Visible sync status** (`SyncStatus.tsx`, in the app frame so it follows the person): the six
frozen P0 §7.5 states — `Synced` · `Offline` · `Last synced <time>` · `N changes waiting` ·
`Syncing` · `Needs Attention`. `N changes waiting` is the `outbox` count. Needs Attention is driven
by unresolved `issues` **and nothing else**, is shown *beside* the current state rather than instead
of it, and therefore survives refreshes, restarts and later successful syncs of unrelated work. A
"Attention" nav destination appears only while something is waiting.

The status line carries no attempt counters, cursors or error codes.

---

## 8. Authentication and offline

Unchanged from P4 in mechanism, and now unable to cost anybody their work:

- An expired access token **never** prevents a local CONFIRM/SKIP from entering the outbox: the
  write is local and happens first.
- A 401 on a push is refreshed once and the **identical** envelope is replayed by `api/client` —
  same `operation_id`, same body. A test asserts the two request bodies are equal.
- If the refresh fails, the session ends and the push stops. The outbox, issues and snapshot are
  untouched and wait for whoever signs in next. A test asserts the entry is still queued after the
  app has fallen back to the login screen.
- Signing out closes the database handle and **deletes nothing**.

**Offline detection is the *and* of two facts.** `navigator.onLine` means "there is an interface",
not "the server can be reached" — it is `true` on a captive-portal wifi, on a dead uplink, and under
Playwright's network emulation. The status line reports `navigator.onLine && lastAttemptSucceeded`;
retrying is still driven by `navigator.onLine` alone, because "unreachable" is exactly the state a
retry exists to leave.

---

## 9. Business date

The server's tenant-timezone business date stays authoritative. `Date.now()` is never an authority
anywhere in the sync path.

- The register renders `business_date` from the snapshot — the last value the *server* stated — and
  prints it in words at the top of the screen rather than the word "Today", so the person can see
  which day thirty taps are being filed under before making them.
- **A queued operation carries `service_date`**, and the value it carries is that same
  server-stated business date. This is the one behavioural change from P4, which omitted
  `service_date` so the server would resolve "today". Omitting it offline would silently refile
  Saturday's round under Sunday whenever a sync crossed midnight — precisely what §7 forbids. The
  server applies its ordinary single rule (`validate_service_date`: not in the future; no maximum
  historical age exists by design), so a late sync of an old round is accepted as that round.
- `client_created_at` is carried because P0 §7.2 defines it, and is advisory only. A test posts an
  envelope stamped 2020 and asserts the record still lands on the tenant's business date.

**The residual ambiguity, stated plainly.** A device that goes offline before midnight and keeps
working after it will record against the cached (yesterday's) business date until it next reaches
the server. That is the smallest safe behaviour: the alternative is a device clock deciding a
business date, which R4 exists to prevent. It is visible — the date is the largest thing in the
header — and it is recoverable, because a wrong day is a correction, not a loss. A multi-day
offline clock policy was **not** invented.

---

## 10. Two tenants on one browser

P0 §7.1 says `meta` is cleared on sign-out, but does not say what happens to work still queued when
somebody signs out mid-round. Deleting the outbox to protect the next tenant's privacy would throw
away accepted human intent (SYN-5/SYN-12); one shared database would show tenant A's customers to
tenant B.

**Naming the database after the tenant settles both at once.** `rsp-sync-v1-<tenant_id>`: signing
out closes the handle and touches no data, and a different tenant opens a different database it
cannot see past. Signing back in resumes exactly where the round stopped. The tenant id is a
*namespace*, never an authority — every request is still scoped server-side by the bearer token,
and the client still never sends a `tenant_id`.

Tested end to end: queue offline as tenant A, sign out, sign in as tenant B, assert B sees none of
A's customers and A's queued round is still there.

**Known limitation:** the namespace is the tenant, not the user, because the stored session does not
carry a user id. Two users of the *same* tenant on one browser share a queue, so an operation
created by one and synchronised by the other is attributed to the latter. With one owner-admin per
tenant this cannot arise today; it is noted as R4 below.

---

## 11. Verification

Run once each, after the last code change.

| Check | Result |
| --- | --- |
| Frontend tests (`npx vitest run`) | **90 passed**, 0 failed, 7 files |
| TypeScript (`tsc --noEmit`) | **clean** — `strict`, `noUncheckedIndexedAccess`, `verbatimModuleSyntax` |
| Production build (`vite build`) | **succeeded** — `index.js` 254 kB (81 kB gzip), `index.css` 6.27 kB; PWA precache 12 entries / 265 KiB |
| Playwright acceptance (`npx playwright test`) | **5 passed**, 0 failed — re-run after the retention change, which alters the pull path every case exercises |
| Backend suite (PostgreSQL 16) | **760 passed**, 0 failed, 5m31s — 687 from P1–P4 unchanged, plus 73 new |
| `git diff --check` | clean |
| Built assets scanned for secrets / vendor identifiers | none — no `VITE_*` value is embedded; the only "secret" match is React's own `__SECRET_INTERNALS_…` symbol |
| Precache manifest | 12 entries (8 distinct URLs: the shell, CSS, JS, manifest and icons), **0** under `/api` |

### Frontend tests

| File | Tests | Covers |
| --- | --- | --- |
| `lib/decimal.test.ts` | 10 | exact quantity arithmetic, money display, uuidv7 (unchanged) |
| `auth/auth.test.tsx` | 10 | login, no `tenant_id`, routing, bearer header, refresh-and-replay, expiry, logout |
| `auth/refresh-replay.test.tsx` | 5 | **a push meeting an expired token**: one refresh, identical replay, same `operation_id`; **the outbox survives a failed refresh**; no second retry; concurrent 401s share one refresh |
| `daily/register.test.tsx` | 14 | server-stated business date, pending/queued/done, stepper, decimal quantity, **enqueue before any network call**, CONFIRM/SKIP payloads incl. `service_date`, "waiting to sync" vs "Recorded", next customer, "Leave for later" writes nothing, no snapshot → unavailable offline |
| `daily/completeness.test.tsx` | 9 | pagination completeness, now through the sync **seed**: 1200 customers across three pages all reach the snapshot and the round |
| `customers/customers.test.tsx` | 13 | list from snapshot, filter, create payload in minor units, duplicate code, detail with server-derived balance, no delete, `expected_row_version`, row-version conflict, stable `operation_id` |
| `sync/sync.test.tsx` | 29 | **the P5 core** — enqueue-before-network; network error / 5xx keep the outbox; survives browser restart; same `operation_id` on retry; APPLIED and DUPLICATE drain; REJECTED and CONFLICT move atomically to issues; issues survive restart, are never re-sent, and stay raised through unrelated successful syncs; resolution keeps the row; status counts and states; offline snapshot reads; no snapshot → unavailable offline; **tenant cache separation**; cursor continuation, no cursor advance past an unreceived page, feed-version resync; **only the current business date's records are stored**; **cleanup never touches queued work or issues**; no money rendered; no `tenant_id` on a push; no busy loop |

### Playwright acceptance (`frontend/e2e/`)

Against the **production build**, in a `launchPersistentContext` profile — a fresh Playwright
context is a fresh profile, and "survives a browser restart" is a claim about storage that outlives
the process.

| Case | Result |
| --- | --- |
| **A-SYN-5** — load and sync, go offline, CONFIRM 10 customers, reload, close and reopen the browser: all 10 still queued, "10 changes waiting" visible, 0 server records; then online → exactly 10 records | **passed** |
| **A-SYN-6** — server applies the operation and its response is destroyed in transit; the client retries the identical envelope; **1 record, 1 registered operation, ≥2 pushes**; outbox drains | **passed** |
| **A-SYN-7** — another device records the same customer/date; one operation applies, the other becomes a durable issue with the server's state; it is **not** re-sent; unrelated work syncs afterwards; Needs Attention stays raised | **passed** |
| **A-SYN-12** — force one REJECTED and one CONFLICT, restart the browser: both issues present, outbox empty, **neither re-sent** (push count unchanged); only "I have reviewed this" clears them | **passed** |
| **PWA** — after one online load, reopen offline: the shell comes from the Service Worker and the round from IndexedDB | **passed** |

**The E2E server is a fixture, not the real backend** (`e2e/server.js`). These four cases are
statements about the *client*; the server semantics they lean on — one effect per `operation_id`,
DUPLICATE on replay, CONFLICT on a taken slot — are proven directly against PostgreSQL in
`backend/tests/test_sync_operations.py`. What the fixture adds is the two faults a real server
cannot be asked to produce on demand: a response dropped after the effect committed, and another
device having filled the same slot.

### Backend tests added

| File | Tests | Covers |
| --- | --- | --- |
| `test_sync_operations.py` | 32 | APPLIED (record, ledger, register, `source=SYNC`); **SKIP creates no ledger entry and no commission**; **commission earned server-side on an accepted SERVICE, never device-side**; DUPLICATE incl. **five concurrent identical envelopes over HTTP → one APPLIED, four DUPLICATE, none CONFLICT** (SYN-15); CONFLICT with authoritative `server_state`; `IDEMPOTENCY_KEY_REUSE`; REJECTED for bad quantity, unknown customer, future date, malformed payload, unknown field, **out-of-scope `op_type`**, op_type/kind mismatch; **A-PAY-8: `payment.record`/`payment.void` are refused and leave no payment, ledger, commission or register row**; rejections and conflicts leave the register untouched; **batch transaction independence**; business-date preservation |
| `test_sync_changes.py` | 25 | feed shape and entities; **no ledger/payment/statement exposed**; `head` semantics and the seed handover; ordering, monotonicity, replay-is-a-superset; updates and voids arrive as updates; paging walks every row exactly once and the cursor never passes an undelivered row; **A-SYN-3 fault injection over the sync path** — neither effect nor register survives, and the retry applies; no pruning anywhere in `app/sync/` |
| `test_tenant_isolation.py` (+5) | 5 | A-SYN-8 cross-tenant customer → REJECTED `NOT_FOUND`; the feed never returns another tenant's rows; no commission entity is syncable; **platform principal refused on both sync routes**; both routes added to the `EXERCISED` inventory so the SEC-3/4/6 guards cover them automatically |

---

## 12. Defects and clarifications found

**D1 — the register's cursor skipped a customer after a save.** Introduced by P5's own change and
caught by A-SYN-12. P4's "Next customer" incremented a cursor into the pending list; in P5 a queued
customer *leaves* that list immediately, so the list had already shifted and incrementing stepped
over the next person — a house missed in silence. Split into two movements: after a save the
selection clears and the cursor stays (the same index is now the next person); "Leave for later"
still advances it, because nothing changed.

**D2 — an accepted operation briefly reappeared as pending.** Waiting for the change feed to deliver
the record left a beat in which a just-confirmed customer showed as "still to do" — which is exactly
how somebody gets recorded twice. The push response already contains the server's own serialization
of the record, so `settleOperation` writes it into the snapshot in the same transaction that drains
the outbox entry.

**D3 — `navigator.onLine` is not a reachability signal.** It reports an interface, not a server. The
status line now reports the conjunction of the browser's claim and whether the last attempt got
through; retry scheduling still uses `navigator.onLine` alone.

**D4 — the change feed could lose a row: the commit-order gap (found in review, fixed).** This was
shipped as risk R1 and was wrong to ship. `row_version` comes from a non-transactional `nextval`
called *inside* a transaction, so:

```
A allocates 100 ........................ commits late
B          allocates 101 ... commits early
feed                            sees 101, cursor -> 101
A                                          commits 100   <- never delivered
```

A client past 101 would never receive 100 — the gap SYN-10 forbids — and it needs only two
concurrent same-tenant writes plus a feed read between the commits, which is exactly what a
two-device round (A-SYN-7) produces. Not a hypothetical for a package that supports multi-device.

**The fix** (`app/sync/serialization.py`): every transaction about to allocate a `row_version` for
an entity the feed carries first takes a **tenant-scoped PostgreSQL transaction advisory lock**
(`pg_advisory_xact_lock`), and holds it through commit or rollback. Consequences:

- within a tenant, feed-visible writes allocate in the order they commit, so the committed versions
  are always a *prefix* of the allocated ones. A committed 101 cannot exist while 100 is
  uncommitted, because 101 could not have been allocated;
- **feed reads need no lock of their own** — the dangerous state no longer exists, so a reader's
  cursor can skip nothing. A shared read lock would restate the guarantee at the price of every
  pull blocking behind an in-flight write. The property is asserted directly rather than assumed;
- an *xact* advisory lock rather than a mutex or a lock table: it is released by PostgreSQL on
  commit **and** on rollback, so no code path can leak it; it needs no table; it is per tenant, so
  tenants never wait on each other; and it is held across the commit itself, which an application
  mutex cannot honestly promise.

It is taken in `execute_idempotent` **before** the register is claimed. That ordering is load-
bearing: taking it after would let one transaction hold an uncommitted register row while waiting
on the lock whose holder waits on that row's unique index — a deadlock between two *identical*
envelopes, which is precisely what A-SYN-1/2 fires five of at once. One order for everybody: lock,
register, effect. Per-operation transaction independence and every existing idempotency guarantee
are unchanged; the whole `test_idempotency` and `test_sync_operations` suites pass untouched.

**The rule for a later package** is one line: when a new entity joins `SYNC_ENTITIES`, add the
`op_type`s that mutate it to `FEED_WRITING_OP_TYPES`. No command has to remember anything.
`payment`, `statement` and `ledger_entry` are deliberately absent because P5's feed does not carry
them — registering them would serialize writes for nothing. `tenant` is absent because nothing
mutates a tenant row; a test pins that correspondence so the omission surfaces if it ever changes.

**D5 — the advisory lock key collided across tenants (found by its own test, fixed).** The key was
the first four bytes of the tenant UUID. Tenant ids are **uuidv7**, whose leading bytes are a
millisecond timestamp — so two tenants provisioned in the same millisecond got the same key and
would have queued behind each other for no reason. Caught immediately by
`test_lock_keys_differ_between_tenants`, whose two fixtures collide. The key is now a BLAKE2b hash
of all sixteen bytes. A residual collision costs a little needless serialization and can never
produce a *missing* lock, which is the only failure that would matter.

**D6 — a 14-day snapshot retention window was invented (found in review, removed).** `RETAINED_DAYS
= 14` was a number nobody chose, documented as a guess. Replaced by the deterministic rule in §3:
retain the currently cached authoritative business date, which is exactly what the P5 UI renders.
No constant, and no configurable setting — the client has not asked for one.

**C1 — P0 §7.2's `op_type` list read as a promise.** It enumerates `service.correct`,
`payment.record` and `customer.update` alongside the two the product actually guarantees offline. A
dated clarification block was added at §7.2 (2026-09-03) stating that the list is the envelope's
extensible vocabulary and that **V1's offline write guarantee is CONFIRM and SKIP**. This narrows
nothing that was built — §8.6 already made the button daily entry "the hard offline guarantee" — and
the server enforces it with a per-operation REJECTED for any other `op_type`. Nothing else in P0 was
touched.

**C2 — the payload model moved, so validation cannot drift.** `RecordServiceRequest` now extends
`ServiceOperationPayload` in `app/sync/envelope.py`, with the shared `QuantityStr` in
`app/core/schema_types.py`. An operation is not the property of one HTTP route, and SYN-8 requires
both transports to validate identically; one definition makes that structural instead of two copies
staying accidentally identical. `app/api` → `app/sync` is an API → domain import and is allowed;
the reverse still is not, and the architecture guard still enforces it.

**C3 — PAY-8 promised offline payment recording (corrected).** `docs/P0_INVARIANTS_AND_ACCEPTANCE.md`
said "Manual payment recording works offline and syncs under the ordinary outbox rules", and
A-PAY-8 was "record a payment offline, restart the browser, sync". Both contradicted the settled V1
scope. PAY-8 keeps its identifier and now states that payment recording is **online-only in V1** and
that `payment.record` / `payment.void` are not accepted sync operations; A-PAY-8 is rewritten as the
assertion actually worth making, and is now tested
(`test_PAY8_a_payment_cannot_be_synchronised_and_leaves_no_trace`): both op types are `REJECTED`,
and **no payment, ledger entry, commission row or register entry** exists behind the refusal.

This is a scope correction, not a capability that was built and removed — no offline payment path
ever existed. The same stale claim was corrected in `docs/P0_HANDOVER.md` (two feature lists that
included "offline recording"), and the superseded criterion was noted in the P2 and P3 handovers'
open-items ledgers. No client-facing document was touched.

**C4 — no defect was found in P1–P4 behaviour.** Every route P5 consumes behaved as its handover
describes.

---

## 13. Risks

**R1 — adding a feed entity costs every device a full resync.** By design
(`SYNC_FEED_VERSION`), and correct, but for a large tenant that is a real download. P6 should add
its entities before the data grows, or give them a windowed bootstrap the way P5 did for service
records. *(Was R3.)*

**R2 — the IndexedDB namespace is the tenant, not the user.** Two users of one tenant on one browser
share a queue, so a queued operation is attributed to whoever syncs it. Impossible today (one
owner-admin per tenant) and fixable by carrying the user id in the stored session. *(Was R4.)*

**R3 — the feed serializes feed-visible writes per tenant.** The cost of closing D4. Within a tenant
those writes were already effectively serial — one owner drives one round, and a sync batch applies
its operations one transaction at a time — so the lock is uncontended in practice, and different
tenants never wait on each other. If a tenant ever has many concurrent writers, this becomes the
throughput ceiling, and the answer is a finer lock (per customer/date) rather than a weaker one.

**R4 — `localStorage` holds the refresh token** (P4 R2, unchanged). Mitigation is a CSP at
deployment, not code. Worth a decision before P12.

**R5 — the E2E suite runs against a fixture server**, by the reasoning in §11. It proves client
behaviour; it does not re-prove server behaviour, which pytest does against real PostgreSQL.

**R6 — `react-router-dom@6` carries two moderate advisories** (open redirect via backslash;
constructor injection in SSR hydration, which this SPA does not use). The fix is a major upgrade to
v7; unrelated to P5 and deliberately not bundled into it.

**R7 — A-SEC-9 (CI secret scanning) is still open**, from P1 through P5.

---

## 14. Recommended next package

**P6 — operating costs and the owner's numbers.** The owner-facing Operating Costs area recorded in
P4 §11 (estimated monthly cost from measured usage × versioned provider rates, actual invoices
logged against it, variance and history, never mixed into commission), plus the statements and
dashboard reads P2 and P3 already compute server-side but nothing renders.

One thing P6 should do while it is in the sync layer anyway: register `payment` and `statement` as
feed entities **before** those tables grow (R1) — and, when it does, add their `op_type`s to
`FEED_WRITING_OP_TYPES` so they inherit the commit-order boundary.

Two things P6 must not lose: the `sync_operation` register is never pruned, and a conflicting
operation is never auto-resubmitted unchanged.

---

## 15. Files

**Created**

```
backend/app/core/schema_types.py        shared constrained wire types
backend/app/sync/envelope.py            op-type vocabulary + the shared payload model
backend/app/sync/operations.py          dispatch and the four verdicts
backend/app/sync/changes.py             the change feed and `head`
backend/app/sync/serialization.py       the SYN-10 commit-order boundary (D4)
backend/tests/test_sync_operations.py   test_sync_changes.py
backend/tests/test_sync_serialization.py
frontend/src/sync/  types.ts  db.ts  stores.ts  api.ts  engine.ts
                    SyncProvider.tsx  SyncStatus.tsx  IssuesPage.tsx
                    useLocalData.ts  sync.test.tsx
frontend/e2e/       offline-sync.spec.ts  server.js
frontend/playwright.config.ts
frontend/public/    icon-192.png  icon-512.png  apple-touch-icon.png
docs/P5_HANDOVER.md
```

**Modified**

```
backend/app/api/schemas.py         RecordServiceRequest extends the shared payload;
                                   SyncOperationEnvelope, SyncOperationsRequest
backend/app/api/routes.py          sync_router (+2 routes); online record route registers
                                   under op_type_for_kind
backend/app/main.py                registers sync_router
backend/tests/test_tenant_isolation.py   sync routes in EXERCISED + 5 new cases
backend/tests/test_architecture.py       app/sync/{changes,operations}.py in the SEC-3 guard
backend/app/sync/idempotency.py    takes the commit-order boundary before the register claim
frontend/vite.config.ts            vite-plugin-pwa, Workbox, manifest; vitest excludes e2e
frontend/package.json              +idb; -D vite-plugin-pwa, fake-indexeddb, @playwright/test;
                                   e2e scripts
frontend/tsconfig.json             pwa client types; e2e in include
frontend/index.html                theme-color, apple-touch-icon
frontend/src/main.tsx              registerSW
frontend/src/providers.tsx         SyncProvider
frontend/src/App.tsx               /attention route
frontend/src/components/AppShell.tsx     SyncStatus, conditional Attention destination
frontend/src/daily/useRegister.ts        reads the snapshot; three states per customer
frontend/src/daily/DailyRegisterPage.tsx offline states, two kinds of "move on"
frontend/src/daily/ServiceCard.tsx       enqueues; "waiting to sync"
frontend/src/customers/CustomerListPage.tsx    reads the snapshot
frontend/src/customers/CustomerCreatePage.tsx  reads settings from the snapshot
frontend/src/styles.css            sync status, pending notice, issue cards
frontend/src/test/{setup,fixtures,...}   fake-indexeddb, SyncProvider, stubServer
frontend/src/{auth,daily,customers}/*.test.tsx   rewired to the new write path
CLAUDE.md                          phase, code boundaries, e2e command, offline scope
docs/P0_ARCHITECTURE_FREEZE.md     §7.2 dated clarification (C1)
docs/P0_INVARIANTS_AND_ACCEPTANCE.md     PAY-8 / A-PAY-8 corrected to online-only (C3)
docs/P0_HANDOVER.md                two feature lists that said "offline recording" (C3)
docs/P2_HANDOVER.md  P3_HANDOVER.md      one dated note each: A-PAY-8 superseded (C3)
.gitignore                         playwright artifacts, dev-dist
```

| `test_sync_serialization.py` | 9 | **SYN-10 under concurrency** — the commit-order boundary (D4): a second same-tenant writer cannot allocate while the first is uncommitted, a feed read inside that window cannot advance its cursor past it, both changes are delivered afterwards with no gap; paging one row at a time; **another tenant is not blocked**; **rollback releases the lock and poisons nothing**; the registry rule stays aligned with `SYNC_ENTITIES`; and the boundary is **wired into real `execute_idempotent` operations** — that last test fails if the lock is removed |

The 73 new backend tests are 32 + 25 + 9 + 5 above, plus **2** from the parameterized SEC-3 scoping
guard, which now covers `app/sync/changes.py` and `app/sync/operations.py` automatically.

`docs/P0_INVARIANTS_AND_ACCEPTANCE.md` **was** modified, for PAY-8 / A-PAY-8 only (C3).

**Dependencies added:** `idb` (runtime); `vite-plugin-pwa`, `fake-indexeddb`, `@playwright/test`
(dev). Each is forced by a frozen requirement: the typed IndexedDB wrapper, the Workbox PWA build,
a real IndexedDB under Vitest, and the E2E runner P0 §1.3 froze.

---

## 16. Git state

Branch `yahya`, base `c383af2`, upstream `origin/yahya`. **Nothing was committed and nothing was
pushed**, as instructed. `git diff --check` reports no whitespace errors. No secret and no `.env`
value is in the diff. All changes are in the working tree.
