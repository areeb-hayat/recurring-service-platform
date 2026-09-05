/**
 * The test that pins the fixture to the client's first-sync contract.
 *
 * **Why this exists.** P5 built the e2e fixture server and its handover left a
 * standing instruction: *"when adding a new feed-visible entity or mutation path,
 * update the feed writer serialization set and the tests that pin correspondence."*
 * There was no such test for the *frontend* fixture. So when P6 made
 * `SyncEngine.seed()` read `/payments` and `/statements`, and P8 took the feed to
 * version 3, `e2e/server.js` silently fell out of step: it 404'd those endpoints
 * and still announced `feed_version: 1`. The whole browser suite went red — but at
 * sign-in, with an opaque *"element not found: Customer 01"*, because the real
 * failure (a rejected first sync) was three layers down.
 *
 * This is the guard that was missing. It is browserless and fast, it runs in the
 * same `npm run e2e` job, and it fails with a message that names the drift
 * directly — *"GET /api/v1/statements → 404; SyncEngine.seed() reads it"* — so the
 * next person who changes the feed cannot ship a stale fixture unnoticed.
 *
 * **The expectation lives here, on purpose.** A correspondence test must hold its
 * own independent statement of the contract and check the fixture against it — if
 * it read the expected values *out of* the fixture, it would be checking the
 * fixture against itself and could never catch a drift. So the constants below are
 * the client's real first-sync contract, restated. Keep them in step with
 * `SyncEngine.seed()` and the backend's `SYNC_FEED_VERSION`; the fixture
 * (`e2e/server.js`) is what must satisfy them. Only `BUSINESS_DATE` — a value the
 * fixture genuinely owns — is imported from it.
 *
 * That `seed()` really reads exactly these endpoints is proven separately by the
 * four sync acceptance cases in `offline-sync.spec.ts`, which drive the real
 * `seed()` in a browser and fail if it grows a read this list lacks.
 */

import { expect, test } from "@playwright/test";

import { BUSINESS_DATE } from "./server.js";

/** The backend's SYNC_FEED_VERSION the client now expects to be served. */
const EXPECTED_FEED_VERSION = 3;

/** Every entity the feed must name at the current version (P6 + P8). */
const EXPECTED_FEED_ENTITIES = [
  "tenant",
  "customer",
  "daily_service_record",
  "payment",
  "statement",
];

/** Every path `SyncEngine.seed()` fetches on a first sync (GET, must be 200). */
const SEED_ENDPOINTS = [
  "/api/v1/customers",
  `/api/v1/service/day/${BUSINESS_DATE}`,
  "/api/v1/payments",
  "/api/v1/statements",
];

test.describe("first-sync contract — the fixture must satisfy SyncEngine.seed()", () => {
  for (const path of SEED_ENDPOINTS) {
    test(`GET ${path} is served (seed() reads it)`, async ({ request }) => {
      const response = await request.get(path);
      expect(
        response.status(),
        `${path} must return 200 — SyncEngine.seed() reads it on a first sync, ` +
          `and a 404 here rejects the whole sync and strands the app offline. ` +
          `Add it to e2e/server.js.`,
      ).toBe(200);

      // Every seed read is a list or a day payload; both carry an `items` array.
      const body = await response.json();
      expect(
        Array.isArray(body.items),
        `${path} must return an { items: [...] } shape for seed() to consume.`,
      ).toBe(true);
    });
  }

  test("the sync feed advertises the current version and entity set", async ({
    request,
  }) => {
    const response = await request.get("/api/v1/sync/changes?since=0&limit=1");
    expect(response.status()).toBe(200);
    const body = await response.json();

    expect(
      body.feed_version,
      `The fixture must serve the client's current feed version ` +
        `(${EXPECTED_FEED_VERSION}). If the real SYNC_FEED_VERSION changed, update ` +
        `FEED_VERSION in e2e/server.js — and this expectation — to match, or a ` +
        `resync is triggered mid-suite.`,
    ).toBe(EXPECTED_FEED_VERSION);

    for (const entity of EXPECTED_FEED_ENTITIES) {
      expect(
        body.entities,
        `The feed must list "${entity}" among its entities.`,
      ).toContain(entity);
    }
  });
});
