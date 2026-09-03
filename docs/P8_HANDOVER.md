# P8 — Handover

Package: **P8 — Smart Search & Customer Identification**. The product can now be
asked *"who is Ahmed bhai?"* and will answer with one customer, a question, or
nothing — never a guess. Nothing committed, nothing pushed.

**Base commit:** `1661c3b` — "Implement P7 reminder engine and delivery orchestration".
**Worktree:** `E:\Recurring-Service-Platform-yahya` · **branch:** `yahya` · **upstream:**
`origin/yahya`.

---

## 1. Recovered partial work

The previous session hit its usage limit mid-package. Its work was **kept, not
recreated**. What existed on disk at the start of this session, all uncommitted:

| State | Files |
| --- | --- |
| New, complete | `app/search/{__init__,normalize,filters,query,resolver}.py`, `app/customers/aliases.py`, `alembic/versions/0006_p8_customer_search.py`, `tests/test_search.py` (1189 lines) |
| Modified | `app/api/routes.py`, `app/api/schemas.py`, `app/audit/{models,service}.py`, `app/customers/{commands,models}.py`, `app/db_models.py`, `app/main.py`, `app/sync/{changes,serialization}.py`, `tests/{conftest,test_architecture}.py` |
| Absent entirely | every frontend change |

Reviewed against the P8 requirements and kept as designed. Three defects were
found and fixed narrowly (§11); the frontend half was built from nothing.

---

## 2. The alias model

One table, `customer_alias`, the only table beyond P0 §6's inventory. It is a
deliberate addition: P0 §8.3 requires *deterministic server-side matching against
the tenant's own customers* and P0 §12.3 forbids sending the customer list to a
model — so the names a customer is actually called have to be data, in the
tenant's own database, or identification is guesswork.

```
customer_alias(id, tenant_id, customer_id, alias, normalized,
               status, created_at, updated_at, deactivated_at)
```

* **`alias` is exactly what the owner typed** and is the only thing ever shown.
  `normalized` is its comparison key and is never displayed.
* **Not a sync entity of its own — no `row_version`.** An alias travels inside
  the customer's payload, and an alias write bumps the *customer's* version. That
  is what carries a new nickname to every device with no new entity, no new
  cursor and no new ordering question.
* **Aliases are unique per customer, never per tenant.** Two brothers can both be
  "Ahmed bhai"; the schema must be able to represent the case the resolver exists
  to answer. `uq_customer_alias_active_normalized` is partial on
  `status = 'ACTIVE'`, so one *active* spelling per customer and nothing more.
* **Correction, never deletion.** Retiring sets `INACTIVE` + `deactivated_at`;
  re-adding a retired spelling **reactivates the same row** rather than inserting
  a second, so one nickname has one history. A `BEFORE DELETE` trigger refuses
  DELETE at the database. Every add, correction, reactivation and retirement
  writes an audit event carrying the alias text before and after
  (`customer_alias.{added,updated,deactivated,reactivated}`).
* **Bounded at 20 per customer** — a bound, not a product rule: an unbounded list
  is an unbounded slice of the search index and an unbounded payload on every
  device.
* **Nothing is generated.** No model suggests an alias and none is derived from
  the name.
* Capabilities are unchanged: aliases are customer data, read under
  `customer:read` and written under `customer:write`. `search:use` was already in
  the frozen P0 §3.2 map and is what gates the search routes.

---

## 3. Normalization — one path, twice

`app/search/normalize.py` is the single server-side definition of "the same
name": NFKD → drop combining marks → NFKC → `casefold()` → every non-alphanumeric
character becomes a space → collapse and strip. Phone numbers reduce to their
digits (Unicode-aware, so Urdu digits count), and the *trailing nine* are
compared, which makes `0300-1234567` and `+923001234567` meet without this module
knowing any country's dialling plan.

There is **no `.lower()` and no whitespace rule anywhere else in the search
path**. Stored comparison keys (`customer.normalized_name`, `customer_alias.
normalized`) are written by that function at every write path, including the
migration's backfill and the test fixtures — so a customer created in 2025 is
findable under exactly the rules as one created tomorrow. Display text is never
rewritten.

**No transliteration.** `احمد` does not match `Ahmed` unless one is stored as an
alias of the other's customer. A romanisation engine would be wrong for names in
ways nobody could predict and would make identification less trustworthy, not
more. Aliases are how the two scripts meet.

`frontend/src/search/normalize.ts` mirrors it for the **offline path only**. The
one difference is documented in the file: JavaScript has `toLowerCase`, not
Python's `casefold`, so a handful of exotic spellings (German `ß`) can normalize
differently. That can only make *offline* search miss a row the server would have
found; it can never identify the wrong person, because offline results are always
a list a person picks from.

---

## 4. Search and ranking

`POST /api/v1/search/customers` takes `CustomerSearchFilter` — a Pydantic model
with `extra="forbid"` — **as the request body**, so FastAPI validates it with the
same object the domain consumes. An unknown field, an inflated limit or an SQL
fragment is refused before a query is built. `sort` is closed to an enumeration,
because an ordering the caller can spell is a column name the caller can spell.

Each way of matching is a *source* — a small SELECT yielding
`(customer_id, tier, matched_on, matched_value)` — `UNION ALL`-ed, with the
strongest tier per customer kept by `DISTINCT ON`. Named integers, not a weighted
score: a number nobody can reason about is how a search starts identifying the
wrong person.

| Tier | Match |
| --- | --- |
| 100 / 95 / 90 | customer code exact · phone exact · phone suffix (9 digits) |
| 85 / 80 | canonical name exact · alias exact |
| 75 / 70 | every query word present as a whole word in the name · in an alias |
| — | **strong/weak line (70)** |
| 55 / 50 | name prefix · alias prefix |
| 45 / 40 | name substring · alias substring |
| 30 | area prefix |
| 20 | trigram `word_similarity ≥ 0.6` |

Word *order* is never required — "Ahmed bhai" and "bhai Ahmed" are the same
query. Only `query_text` produces tiers; `code`, `phone`, `area`,
`name_contains`, `customer_status`, the outstanding range and the service-date
fields are ordinary predicates that narrow the result. That split is what keeps
ranking explicable: one input ranks, the rest include or exclude.

Ordering is total — tier, then name, then id — so paging can neither skip nor
repeat, and the same query gives the same answer twice.

**PostgreSQL only, no new service.** `pg_trgm` supplies GIN indexes that make the
substring/whole-word `LIKE` patterns index-served, plus `word_similarity` for
typo tolerance at a threshold fixed in application code (never the session GUC,
so connections cannot disagree). If the extension is absent the fuzzy source is
simply not built and search degrades to exact/token/prefix matching rather than
failing. No Elasticsearch, no OpenSearch, no vector database, no external search
service.

`outstanding_min_minor` is offered instead of P0 §12.1's `status` filter, and the
reason is recorded in `app/search/filters.py`: the payment status is derived by
exactly one function, one customer at a time (FIN-11), so filtering a table by it
would mean either an N+1 or a second set-based implementation of the derivation.

---

## 5. RESOLVED / AMBIGUOUS / NOT_FOUND

`app/search/resolver.py`. A reference resolves **only** when

1. the strongest match is *strong* — a code, a phone, an exact name, an exact
   alias, or every word appearing as a whole word; **and**
2. exactly one customer holds that strength.

Anything else is `AMBIGUOUS` with a short candidate list (default 5, max 10). A
prefix, a substring, an area and a fuzzy match are suggestions however far ahead
they score: **nothing weak ever resolves**, which is why a typo cannot quietly
become a customer.

Strict dominance, not "best score wins": an exact name beats a partial one, so
typing "Ahmed" when a customer *is* Ahmed identifies him — while two customers
whose names merely contain the word produce "Which Ahmed?". Inactive customers
are excluded unless asked for. A blank reference is `NOT_FOUND` and touches the
database not at all.

`POST /api/v1/search/customers/resolve` is a POST with a body deliberately: the
reference is a person's name, and names do not belong in URLs, access logs or
browser history.

---

## 6. Website UX

* **Customers list** — the search box was a display filter over loaded rows; it
  is now the real thing. Online it calls `POST /search/customers` (names,
  aliases, codes, phones, areas); offline it searches the device's snapshot. With
  the box empty the plain snapshot list is unchanged. Inactive customers are
  included here — this is where somebody who has left the round is looked up.
* **Result rows carry what distinguishes people**: canonical name, the alias that
  matched when that is not the name, customer code, area, phone when phone is
  what matched, and the server's outstanding when the server answered. A weak
  match is badged "Possible match" rather than hidden or promoted.
* **The source is always printed** — "Searching everyone on the books" versus
  "Offline — searching the customers already on this device… anyone added or
  renamed since this device last synchronised will not appear". Two different
  claims, and a round makes different decisions depending on which it read.
* **A dropped connection falls back to the device and says so.** An empty result
  would read as "this person does not exist", which is a lie a round would act on.
* **Alias management** lives on the customer detail page ("Also known as"): list,
  add, correct in place, retire. Online only, each write carrying an
  `operation_id` generated once at the click. There is no delete control because
  there is no delete path.
* Mobile-friendly throughout — the existing `.field` / `.list` / `.row` controls,
  no new layout primitives. **No command palette was built.**

---

## 7. Daily Register integration

`frontend/src/daily/RegisterSearch.tsx`, above the card. Typing lists candidates
restricted to today's round; **Enter asks the resolver** and that is where the
contract shows: RESOLVED opens the card, AMBIGUOUS asks "Which one?" with the
candidates, NOT_FOUND says so plainly. A customer who exists but is not on the
round is reported as such — a different fact from "no such person".

It **only ever selects an existing card**. Untouched / Done / Waiting to sync /
Needs attention are untouched, no write path was added, and there is no second
register state machine. A test asserts the three lists do not move while
searching and that no operation is queued.

Once a customer is selected, the existing P6 financial view is one tap away from
their detail page. **No natural-language "show Ahmed's bill" was built** — P9
will interpret that sentence and reuse this resolver plus the P6 view.

---

## 8. Offline behaviour

* **Online** — the server searches the whole book, authoritatively.
* **Offline** — `frontend/src/search/local.ts` ranks the P5 snapshot's customers
  with the mirrored tiers and the *same* resolution rule. Offline is not an
  excuse to guess: two candidates on the device are still a question.
* **No typo tolerance offline.** Trigram similarity is PostgreSQL's; inventing a
  different notion of "close enough" would be a second definition. Offline
  matches exactly, by whole word, by prefix or by substring.
* **No balances offline.** A local candidate carries `outstanding_minor: null`
  and the row prints nothing — never a zero standing in for a figure the device
  cannot vouch for (FIN-4 / SYN-9).
* **Aliases need no new feed entity.** They travel inside the customer payload,
  which is the simpler design the brief asked to prefer.
* `SYNC_FEED_VERSION` **2 → 3**. No entity joined; the bump exists because every
  customer row now carries `aliases`, and rows already on a device would
  otherwise keep the old shape until something unrelated changed them. The client
  already handles a version change generically: it clears the **snapshot only** —
  outbox and issues are not caches and are untouched. A snapshot row lacking
  `aliases` is read as "none known here", not "none exist", so search degrades
  rather than breaking.
* The three alias op types joined `FEED_WRITING_OP_TYPES` (they bump a feed
  entity's `row_version`) and **none** joined `SUPPORTED_OP_TYPES`:
  `POST /sync/operations` refuses them, exactly as it refuses customer create and
  edit. **Offline CONFIRM and SKIP are unchanged.**

---

## 9. The P9 / P10 reuse contract

```
free text (typed now; a transcript in P9; a message in P10)
        │
        ▼
POST /search/customers/resolve  →  resolve_customer(session, ctx, reference)
        │
   ┌────┼───────────┐
   ▼    ▼           ▼
RESOLVED  AMBIGUOUS  NOT_FOUND
(one id)  (candidates) (nothing)
```

`resolve_customer` takes a session, a tenant context and a string. It knows
nothing about channels, and there is deliberately no per-channel matching code
for a later package to grow — two implementations of "which customer is this?"
are two answers to a question that must have one. A later package that needs to
identify somebody calls this function or this endpoint and handles all three
answers; it does not add a fourth, and it does not act on AMBIGUOUS.

**Approved P9 usability goals this foundation must serve** (recorded here so they
survive the package boundary; none is implemented): voice-assisted customer
creation; spoken confirmation / read-back; aliases and nicknames; pause and
resume of a voice workflow; changing a future default quantity by voice;
retrospective service entry by voice; a spoken customer bill breakdown; audio and
customer-identification help for low-literacy operators; and targeted
clarification when only *part* of a command is ambiguous — for which AMBIGUOUS
carrying candidates, rather than a bare failure, is the enabling shape.

**Future text channels** — WhatsApp and SMS (P10) — reach the same resolver. P8
implements only the reusable alias / search / resolution foundation.

---

## 10. Migration

**One migration**, `0006_p8_customer_search`, containing only P8's schema:

* `CREATE EXTENSION IF NOT EXISTS pg_trgm` (needs an elevated role once at
  migration time — `rds_superuser` on RDS, `cloudsqlsuperuser` on Cloud SQL);
* `customer.normalized_name` + a Python backfill that runs the **application's
  own** `normalize_text` over existing rows;
* indexes: `ix_customer_tenant_id_normalized_name`,
  `ix_customer_tenant_id_lower_code` (functional), `ix_customer_normalized_name_trgm`;
* `customer_alias` with its composite FK `(tenant_id, customer_id)` (SEC-2), four
  check constraints, the partial unique index, two btree indexes, a trigram GIN
  index, and the `BEFORE DELETE` trigger;
* `downgrade()` removes all of it and deliberately leaves `pg_trgm` installed.

Nothing financial moves: no column on `ledger_entry`, `payment`, `statement`, any
`commission_*`, any `operating_cost_*` or any reminder table changes.

**Verified** on a scratch database: `upgrade head` → `downgrade -1` → `upgrade
head`. After the downgrade the table, the column, all four indexes and the
trigger function are gone; after the second upgrade all are back. The backfill
was exercised separately with a pre-existing row: `"  Muhammad   ÁHMED-Khan "`
→ `muhammad ahmed khan`.

---

## 11. Defects found and fixed

1. **`tests/test_search.py` used `ctx.business_date`**, which does not exist —
   the attribute is `ctx.today`. One-line fix.
2. **A scale test asserted that `"Number00042"` matched exactly one of 500
   synthetic customers.** It matched 20: the seeded names (`Customer Number00042`
   vs `…00004`) share almost every trigram, so the fuzzy source correctly
   surfaced the neighbours. The product behaviour is right, so the test was
   rewritten to pin what actually matters — exactly one *strong* match, ranked
   first, the rest marked WEAK, the page inside the filter's cap, and nothing but
   the exact match when fuzzy is off.
3. **A React state race in the new alias UI**: which alias to retire was held in
   `useState` and read by a request dispatched in the same click, so the first
   retire would have sent the previous row's id (or none). The alias id now
   travels in the operation payload.

Four pre-existing tests pinned values P8 legitimately changes, and were updated
narrowly rather than weakened:

* `test_architecture` — `trigram_available(session)` takes a session and no
  `ctx`. It is added to the SEC-3 exception list with its reason: it queries
  `pg_extension`, a deployment fact identical for every tenant, so there is
  nothing to scope.
* `test_p6_sync_feed` / `test_sync_changes` — both asserted `SYNC_FEED_VERSION
  == 2`. They now assert the *transition* (`>= 2`, `> 1`) and that the feed
  reports whatever the constant is, which is what those tests were about.
* `test_sync_serialization` — `ENTITY_WRITERS` mapped one module per entity;
  `customer` now has two writers (`customers/commands.py` and
  `customers/aliases.py`). The map holds a tuple of modules.

`tests/test_tenant_isolation.py`'s route-inventory guard did its job: it failed
on six unregistered tenant-scoped routes. A `TestP8SearchIsolation` class was
added with real cross-tenant coverage (search, resolve and all four alias routes)
before registering them.

**No clarification was needed against P0.** `search:use` already existed in the
frozen capability map; the one addition beyond P0 §6's table inventory is
`customer_alias`, justified above and recorded in `app/db_models.py::P8_TABLES`.

---

## 12. Tests

**Backend — 1075 passed** against real PostgreSQL (`docker-compose.test.yml`,
never SQLite). `tests/test_search.py` is 124 of them, over normalization,
matching, ranking, fuzzy behaviour, resolution semantics, aliases, the customer
payload, tenant isolation, the HTTP surface, feed integration, scale and the
absence of any interpreter. `tests/test_tenant_isolation.py` gained 6.

Proven explicitly: tenant A cannot search tenant B; an alias is not searchable
across tenants; resolution never crosses one and answers NOT_FOUND rather than a
near miss; the filter has nowhere to name a tenant (`extra="forbid"` → 422); a
platform principal is 403 on every search and alias route; a foreign customer id
is 404, never 403; result size is bounded by the filter and again by the query; a
SQL fragment is only ever a string; alias loading is batched (search issues ≤ 2
statements for 25 rows, serializing 20 customers issues exactly 1); and search
over 1000 customers resolves correctly and quickly. Populations of 100, 500 and
1000+ are exercised.

**Frontend — 187 passed** (Vitest), of which `src/search/search.test.tsx` is 51:
the normalization mirror, offline matching and offline identification, the
list's server-backed search, the network-failure fallback, the register's
RESOLVED / AMBIGUOUS / NOT_FOUND behaviour online and offline, that searching
does not disturb the round, that the P6 financial view is one tap from a selected
customer, and that retiring an alias issues no DELETE.

**Typecheck** (`tsc --noEmit`) and **production build** both clean.
Playwright was **not** run: P8 introduces no browser-only guarantee — the Service
Worker, the outbox and the snapshot are unchanged — and rerunning P5/P7's browser
suite ceremonially proves nothing.

---

## 13. Risks

* **`pg_trgm` needs an elevated role at migration time.** Documented in the
  migration. The application degrades honestly if the extension is missing, but
  the substring indexes would be missing too, so a large book would search
  slowly. Check it exists after deploying.
* **The client's `toLowerCase` is not `casefold`.** Offline only, and it can only
  cause a miss, never a wrong identification. If it ever matters the fix is a
  full case-folding table in `normalize.ts`, not a second matching path.
* **The fuzzy threshold (0.6) has never met real Roman-Urdu spelling.** It is
  deterministic and pinned by tests, and nothing fuzzy can resolve — so a wrong
  threshold produces noise in a list, not a wrong customer. Expect to tune it
  against real queries.
* **Alias quality is an operational matter.** Search is only as good as the
  nicknames somebody troubles to record. Nothing generates them, on purpose.
* **Offline search cannot claim completeness** and says so; a device that has not
  synchronised since a customer was added will not find that customer. This is
  stated on screen rather than hidden.

---

## 14. Recommended P9

Voice, on this foundation and nothing new underneath it: `SpeechToTextProvider`
with ElevenLabs `scribe_v2` behind the port (`app/adapters/speech/`), tests always
on the mock; a closed candidate-intent schema limited to record and skip; a
confirmation step a person must take; and **`resolve_customer` as the only way a
transcript becomes a customer id** — with AMBIGUOUS driving the targeted
clarification the P9 goals ask for. No voice write endpoint. Raw audio is never
persisted and transcripts stay ephemeral. The button workflow stays authoritative
and always available.

---

## 15. Git status

Nothing committed and nothing pushed. `HEAD` is still `1661c3b` on `yahya`.
`git diff --check` is clean.

```
 M backend/app/api/routes.py
 M backend/app/api/schemas.py
 M backend/app/audit/models.py
 M backend/app/audit/service.py
 M backend/app/customers/commands.py
 M backend/app/customers/models.py
 M backend/app/db_models.py
 M backend/app/main.py
 M backend/app/sync/changes.py
 M backend/app/sync/serialization.py
 M backend/tests/conftest.py
 M backend/tests/test_architecture.py
 M backend/tests/test_p6_sync_feed.py
 M backend/tests/test_sync_changes.py
 M backend/tests/test_sync_serialization.py
 M backend/tests/test_tenant_isolation.py
 M frontend/src/api/operation.ts
 M frontend/src/api/types.ts
 M frontend/src/customers/CustomerDetailPage.tsx
 M frontend/src/customers/CustomerListPage.tsx
 M frontend/src/customers/customers.test.tsx
 M frontend/src/daily/DailyRegisterPage.tsx
 M frontend/src/sync/sync.test.tsx
 M frontend/src/test/fixtures.tsx
 M CLAUDE.md
?? backend/alembic/versions/0006_p8_customer_search.py
?? backend/app/customers/aliases.py
?? backend/app/search/
?? backend/tests/test_search.py
?? docs/P8_HANDOVER.md
?? frontend/src/api/aliases.ts
?? frontend/src/api/search.ts
?? frontend/src/customers/CustomerAliases.tsx
?? frontend/src/daily/RegisterSearch.tsx
?? frontend/src/search/
```
