import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { chromium, expect, test, type BrowserContext, type Page } from "@playwright/test";

/**
 * The P5 acceptance cases that only a real browser can answer.
 *
 * Each of these turns on something Vitest cannot simulate honestly: a profile
 * that survives the browser closing, a Service Worker serving a navigation with
 * the network switched off, a response dropped mid-flight. They are run against
 * the production build, because a development build has no Service Worker.
 *
 * A **persistent context** is used throughout: a fresh Playwright context is a
 * fresh profile, and "the outbox survives a browser restart" is a claim about
 * storage that outlives the process, not about a variable that outlives a
 * function.
 */

const BASE = "http://localhost:4173";

let profileDir: string;

test.beforeEach(async ({ request }) => {
  await request.post(`${BASE}/__test/reset`);
  profileDir = mkdtempSync(join(tmpdir(), "rsp-e2e-"));
});

test.afterEach(() => {
  try {
    rmSync(profileDir, { recursive: true, force: true });
  } catch {
    /* Windows sometimes holds a handle briefly; the temp dir is disposable. */
  }
});

async function openBrowser(): Promise<BrowserContext> {
  return chromium.launchPersistentContext(profileDir, {
    serviceWorkers: "allow",
    args: ["--no-sandbox"],
  });
}

/** Sign in and wait until the round has been synchronised onto the device. */
async function signIn(page: Page): Promise<void> {
  await page.goto(`${BASE}/login`);
  await page.getByLabel("Email").fill("owner@alpha.test");
  await page.getByLabel("Password").fill("correct horse");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Customer 01" })).toBeVisible();
  await expect(page.getByText(/Synced/)).toBeVisible();
}

/** Confirm the customer currently on the card and move to the next one. */
async function confirmCurrent(page: Page): Promise<void> {
  await page.getByRole("button", { name: "Confirm" }).click();
  await page.getByRole("button", { name: "Next customer" }).click();
}

/** The Needs Attention chip in the app frame — not the page heading. */
function attentionChip(page: Page) {
  return page.getByRole("link", { name: /Needs Attention/ });
}

async function serverState(request: { get: (u: string) => Promise<{ json(): Promise<any> }> }) {
  return (await request.get(`${BASE}/__test/state`)).json();
}

test("A-SYN-5 — ten offline confirms survive a reload and a browser restart", async ({
  request,
}) => {
  let context = await openBrowser();
  let page = await context.newPage();
  await signIn(page);

  await context.setOffline(true);
  for (let i = 0; i < 10; i += 1) await confirmCurrent(page);

  await expect(page.getByText("10 changes waiting")).toBeVisible();
  expect((await serverState(request)).records).toHaveLength(0);

  // A reload keeps them.
  await page.reload();
  await expect(page.getByText("10 changes waiting")).toBeVisible();

  // And so does closing the browser entirely and opening it again.
  await context.close();
  context = await openBrowser();
  page = await context.newPage();
  await context.setOffline(true);
  await page.goto(`${BASE}/today`);

  await expect(page.getByText("10 changes waiting")).toBeVisible();
  await expect(page.getByText("Waiting to sync (10)")).toBeVisible();
  expect((await serverState(request)).records).toHaveLength(0);

  // Back online, the queue drains into exactly ten server records.
  await context.setOffline(false);
  await expect(page.getByText(/Synced/)).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("10 changes waiting")).toBeHidden();
  expect((await serverState(request)).records).toHaveLength(10);

  await context.close();
});

test("A-SYN-6 — a response lost after the server committed becomes a DUPLICATE, not a second record", async ({
  request,
}) => {
  const context = await openBrowser();
  const page = await context.newPage();
  await signIn(page);

  // The next push is applied and then its response is thrown away in transit.
  await request.post(`${BASE}/__test/drop-next-push`);
  await page.getByRole("button", { name: "Confirm" }).click();

  // The client cannot tell a lost response from a lost request, so it retries
  // the identical envelope — and the server answers DUPLICATE.
  await expect
    .poll(async () => (await serverState(request)).push_count, { timeout: 30_000 })
    .toBeGreaterThanOrEqual(2);
  await expect(page.getByText(/changes waiting/)).toBeHidden({ timeout: 30_000 });

  const state = await serverState(request);
  // Two pushes, one record: that is the whole guarantee.
  expect(state.records).toHaveLength(1);
  expect(state.operations).toHaveLength(1);

  await context.close();
});

test("A-SYN-7 — a two-device collision becomes a durable issue that is never re-sent", async ({
  request,
}) => {
  const context = await openBrowser();
  const page = await context.newPage();
  await signIn(page);

  const { customers } = await serverState(request);
  const first = customers[0];

  await context.setOffline(true);
  await confirmCurrent(page); // customer 01, queued on this device
  await confirmCurrent(page); // customer 02, will apply normally
  await expect(page.getByText("2 changes waiting")).toBeVisible();

  // Meanwhile, another device records customer 01 for the same date.
  await request.post(`${BASE}/__test/record-elsewhere`, {
    data: { customer_id: first.id, quantity: "9" },
  });

  await context.setOffline(false);
  await expect(attentionChip(page)).toBeVisible({ timeout: 30_000 });

  // One applied, one parked. The queue is empty either way.
  await expect(page.getByText(/changes waiting/)).toBeHidden();
  const afterSync = await serverState(request);
  // The other device's record, and customer 02's. The conflicting operation
  // created nothing and was not merged into anything.
  expect(afterSync.records).toHaveLength(2);
  expect(afterSync.operations).toHaveLength(1);

  await attentionChip(page).click();
  await expect(page.getByRole("heading", { name: "Customer 01" })).toBeVisible();
  await expect(page.getByText(/already recorded/i)).toBeVisible();

  // A later sync of unrelated work succeeds, and the issue stays raised.
  const pushesBefore = (await serverState(request)).push_count;
  await page.goto(`${BASE}/today`);
  await expect(page.getByRole("button", { name: "Confirm" })).toBeVisible();
  await page.getByRole("button", { name: "Confirm" }).click();
  await expect
    .poll(async () => (await serverState(request)).push_count, { timeout: 30_000 })
    .toBeGreaterThan(pushesBefore);
  await expect(page.getByText(/changes waiting/)).toBeHidden({ timeout: 30_000 });

  const afterUnrelated = await serverState(request);
  expect(afterUnrelated.operations).toHaveLength(2);
  await expect(attentionChip(page)).toBeVisible();

  await context.close();
});

test("A-SYN-12 — a rejection and a conflict both outlive a browser restart", async ({
  request,
}) => {
  let context = await openBrowser();
  let page = await context.newPage();
  await signIn(page);

  const { customers } = await serverState(request);
  const [first, second] = customers;

  // Customer 01 will conflict; customer 02 will be rejected.
  await request.post(`${BASE}/__test/record-elsewhere`, { data: { customer_id: first.id } });
  await request.post(`${BASE}/__test/reject-customer`, { data: { customer_id: second.id } });

  await context.setOffline(true);
  await confirmCurrent(page);
  await confirmCurrent(page);
  await expect(page.getByText("2 changes waiting")).toBeVisible();

  await context.setOffline(false);
  await expect(attentionChip(page)).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(/changes waiting/)).toBeHidden();

  const pushesBefore = (await serverState(request)).push_count;

  await context.close();
  context = await openBrowser();
  page = await context.newPage();
  await page.goto(`${BASE}/attention`);

  // Both are still there, still unresolved, and neither was re-sent on the sync
  // that ran when the app came back up.
  await expect(page.getByRole("heading", { name: "Customer 01" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Customer 02" })).toBeVisible();
  await expect(attentionChip(page)).toBeVisible();
  await expect(page.getByText(/changes waiting/)).toBeHidden();

  const after = await serverState(request);
  expect(after.operations).toHaveLength(0);
  expect(after.push_count).toBe(pushesBefore);

  // Reviewing them is the only way they leave.
  const reviewed = page.getByRole("button", { name: "I have reviewed this" });
  await reviewed.first().click();
  await reviewed.first().click();
  await expect(attentionChip(page)).toBeHidden();

  await context.close();
});

test("the built app shell opens offline after one online load", async () => {
  let context = await openBrowser();
  let page = await context.newPage();
  await signIn(page);

  // Wait for the Service Worker to take control of this page. `clientsClaim`
  // means the first worker claims it as soon as it activates, so one online load
  // is genuinely enough — which is the claim being tested.
  await page.waitForFunction(() => navigator.serviceWorker.controller !== null, null, {
    timeout: 30_000,
  });

  await context.close();
  context = await openBrowser();
  page = await context.newPage();
  await context.setOffline(true);

  // No network at all: the shell comes from the Service Worker's cache and the
  // round comes from IndexedDB.
  await page.goto(`${BASE}/today`);
  await expect(page.getByRole("heading", { name: "Customer 01" })).toBeVisible();
  // The status says Offline because the server could not be reached, whatever
  // `navigator.onLine` claims — it reports "there is an interface", not "the
  // server answered", and under network emulation it stays `true`.
  await expect(page.locator(".sync-chip").first()).toHaveText("Offline");

  await context.close();
});
