import { describe, expect, it } from "vitest";
import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { App } from "@/App";
import {
  changesResponse,
  customer,
  errorBody,
  pushResults,
  renderApp,
  serviceRecord,
  SETTINGS,
  signedIn,
  stubServer,
  TENANT_ID,
  testEngine,
} from "@/test/fixtures";
import { requestsTo, stub } from "@/test/http";
import { engineFor, resetEngines } from "./engine";
import { resetSyncDbCache } from "./db";
import { openSyncDb } from "./db";

/**
 * The offline guarantees, tested where they actually live: IndexedDB.
 *
 * These are the invariants a person's day depends on. An entry that reaches the
 * outbox is never lost by anything short of the browser's storage being wiped —
 * not by a dropped connection, not by a 500, not by an expired token, not by
 * closing the tab, and not by the operation being refused. A refusal moves it to
 * Needs Attention, which is a different place, not a smaller one.
 */

const AYESHA = customer({ name: "Ayesha Khan" });
const BILAL = customer({
  id: "dddddddd-dddd-7ddd-8ddd-dddddddddddd",
  code: "C-002",
  name: "Bilal Ahmed",
});
const SYNC_PATH = "/api/v1/sync/operations";

function opsOf(body: unknown): Array<Record<string, unknown>> {
  return (body as { operations: Array<Record<string, unknown>> }).operations;
}

function setOnline(value: boolean): void {
  Object.defineProperty(navigator, "onLine", { value, configurable: true });
  window.dispatchEvent(new Event(value ? "online" : "offline"));
}

/** Close the tab and open it again: same storage, brand-new engine and handles. */
function reopenBrowser(): void {
  cleanup();
  resetEngines();
  resetSyncDbCache();
}

async function db() {
  return openSyncDb(TENANT_ID);
}

async function confirmFirstCustomer(): Promise<void> {
  await screen.findByRole("heading", { name: "Ayesha Khan" });
  await userEvent.click(screen.getByRole("button", { name: "Confirm" }));
}

describe("the outbox is written before the network", () => {
  it("stores the envelope even when every request fails", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });
    stub("POST", SYNC_PATH, { networkError: true });

    renderApp(<App />, "/today");
    await confirmFirstCustomer();

    const store = await db();
    await waitFor(async () => expect(await store.count("outbox")).toBe(1));
    const [entry] = await store.getAll("outbox");
    expect(entry!.envelope.operation_id).toMatch(/^[0-9a-f-]{36}$/);
    expect(entry!.envelope.payload.service_date).toBe(SETTINGS.business_date);
  });

  it("keeps the entry when the request never reaches the server", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });
    stub("POST", SYNC_PATH, { networkError: true });

    renderApp(<App />, "/today");
    await confirmFirstCustomer();

    const store = await db();
    await waitFor(() => expect(requestsTo("POST", SYNC_PATH)).not.toHaveLength(0));
    expect(await store.count("outbox")).toBe(1);
    expect(await store.count("issues")).toBe(0);
  });

  it("keeps the entry on a 5xx, which is not a verdict", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });
    stub("POST", SYNC_PATH, { status: 500, body: { detail: "boom" } });

    renderApp(<App />, "/today");
    await confirmFirstCustomer();

    const store = await db();
    await waitFor(() => expect(requestsTo("POST", SYNC_PATH)).not.toHaveLength(0));
    await waitFor(async () => {
      const [entry] = await store.getAll("outbox");
      expect(entry?.attempt_count).toBeGreaterThan(0);
    });
    expect(await store.count("outbox")).toBe(1);
    expect(await store.count("issues")).toBe(0);
  });

  it("survives closing and reopening the browser", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });
    stub("POST", SYNC_PATH, { networkError: true });

    renderApp(<App />, "/today");
    await confirmFirstCustomer();
    await waitFor(async () => expect(await (await db()).count("outbox")).toBe(1));

    reopenBrowser();
    const store = await db();
    expect(await store.count("outbox")).toBe(1);
    const [entry] = await store.getAll("outbox");
    expect(entry!.context.customer_name).toBe("Ayesha Khan");
  });

  it("retries with the same operation_id it started with", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });
    stub("POST", SYNC_PATH, (request, attempt) =>
      attempt === 0
        ? { networkError: true }
        : pushResults({
            operation_id: opsOf(request.body)[0]!.operation_id as string,
            status: "APPLIED",
            entity: serviceRecord(),
          }),
    );

    renderApp(<App />, "/today");
    await confirmFirstCustomer();
    await waitFor(() => expect(requestsTo("POST", SYNC_PATH)).not.toHaveLength(0));

    const first = opsOf(requestsTo("POST", SYNC_PATH)[0]!.body)[0]!;

    // Let the backoff elapse rather than hammering: the entry is due again.
    const store = await db();
    for (const entry of await store.getAll("outbox")) {
      await store.put("outbox", { ...entry, next_attempt_at: 0 });
    }
    await testEngine().syncNow();
    await waitFor(() => expect(requestsTo("POST", SYNC_PATH).length).toBeGreaterThan(1));
    const second = opsOf(requestsTo("POST", SYNC_PATH)[1]!.body)[0]!;

    expect(second.operation_id).toBe(first.operation_id);
    expect(second).toEqual(first);
  });
});

describe("verdicts", () => {
  it("APPLIED drains the entry", async () => {
    signedIn();
    stubServer({
      customers: [AYESHA],
      push: (request) =>
        pushResults({
          operation_id: opsOf(request.body)[0]!.operation_id as string,
          status: "APPLIED",
          entity: serviceRecord(),
        }),
    });

    renderApp(<App />, "/today");
    await confirmFirstCustomer();

    const store = await db();
    await waitFor(async () => expect(await store.count("outbox")).toBe(0));
    expect(await store.count("issues")).toBe(0);
  });

  it("DUPLICATE drains the entry, and records nothing twice", async () => {
    signedIn();
    stubServer({
      customers: [AYESHA],
      push: (request) =>
        pushResults({
          operation_id: opsOf(request.body)[0]!.operation_id as string,
          status: "DUPLICATE",
          entity: serviceRecord(),
        }),
    });

    renderApp(<App />, "/today");
    await confirmFirstCustomer();

    const store = await db();
    await waitFor(async () => expect(await store.count("outbox")).toBe(0));
    expect(await store.count("issues")).toBe(0);
    expect(await screen.findByText("Recorded 2 bottle.")).toBeInTheDocument();
  });

  it("REJECTED leaves the outbox and lands in issues, never in neither", async () => {
    signedIn();
    stubServer({
      customers: [AYESHA],
      push: (request) =>
        pushResults({
          operation_id: opsOf(request.body)[0]!.operation_id as string,
          status: "REJECTED",
          error: { code: "VALIDATION", detail: "quantity must be positive" },
        }),
    });

    renderApp(<App />, "/today");
    await confirmFirstCustomer();

    const store = await db();
    await waitFor(async () => expect(await store.count("issues")).toBe(1));
    expect(await store.count("outbox")).toBe(0);
    const [issue] = await store.getAll("issues");
    expect(issue!.verdict).toBe("REJECTED");
    expect(issue!.resolved_at).toBeNull();
    expect(issue!.context.customer_name).toBe("Ayesha Khan");
  });

  it("CONFLICT lands in issues with the server's own state attached", async () => {
    const theirs = serviceRecord({ quantity: "5.000" });
    signedIn();
    stubServer({
      customers: [AYESHA],
      push: (request) =>
        pushResults({
          operation_id: opsOf(request.body)[0]!.operation_id as string,
          status: "CONFLICT",
          error: { code: "SERVICE_ALREADY_RECORDED", detail: "already recorded" },
          server_state: theirs,
        }),
    });

    renderApp(<App />, "/today");
    await confirmFirstCustomer();

    const store = await db();
    await waitFor(async () => expect(await store.count("issues")).toBe(1));
    expect(await store.count("outbox")).toBe(0);
    const [issue] = await store.getAll("issues");
    expect(issue!.verdict).toBe("CONFLICT");
    expect(issue!.server_state?.quantity).toBe("5.000");
    // The other device's record is the truth about that customer and date.
    expect(await store.get("snapshot", `daily_service_record:${theirs.id}`)).toBeTruthy();
  });
});

describe("issues are durable", () => {
  async function raiseAnIssue(): Promise<void> {
    signedIn();
    stubServer({
      customers: [AYESHA, BILAL],
      push: (request) =>
        pushResults({
          operation_id: opsOf(request.body)[0]!.operation_id as string,
          status: "CONFLICT",
          error: { code: "SERVICE_ALREADY_RECORDED", detail: "already recorded" },
          server_state: serviceRecord(),
        }),
    });

    renderApp(<App />, "/today");
    await confirmFirstCustomer();
    await waitFor(async () => expect(await (await db()).count("issues")).toBe(1));
  }

  it("survives a browser restart with the outbox already empty", async () => {
    await raiseAnIssue();
    reopenBrowser();

    const store = await db();
    expect(await store.count("issues")).toBe(1);
    expect(await store.count("outbox")).toBe(0);
  });

  it("is never re-sent by a later sync", async () => {
    await raiseAnIssue();
    const sentSoFar = requestsTo("POST", SYNC_PATH).length;

    await testEngine().syncNow();
    await testEngine().syncNow();

    expect(requestsTo("POST", SYNC_PATH)).toHaveLength(sentSoFar);
  });

  it("stays raised while unrelated work syncs successfully", async () => {
    await raiseAnIssue();

    // A second, different operation that the server accepts.
    stub("POST", SYNC_PATH, (request) =>
      pushResults({
        operation_id: opsOf(request.body)[0]!.operation_id as string,
        status: "APPLIED",
        entity: serviceRecord({ customer_id: BILAL.id, id: "eeeeeeee-eeee-7eee-8eee-eeeeeeeeeeee" }),
      }),
    );
    await userEvent.click(screen.getByRole("button", { name: /Bilal Ahmed/ }));
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));

    const store = await db();
    await waitFor(async () => expect(await store.count("outbox")).toBe(0));
    expect(await store.count("issues")).toBe(1);
    expect(await screen.findByText("Needs Attention")).toBeInTheDocument();
  });

  it("leaves only when a person says they have reviewed it", async () => {
    await raiseAnIssue();

    await userEvent.click(await screen.findByText("Needs Attention"));
    await userEvent.click(
      await screen.findByRole("button", { name: "I have reviewed this" }),
    );

    const store = await db();
    await waitFor(async () => {
      const [issue] = await store.getAll("issues");
      expect(issue!.resolved_at).not.toBeNull();
    });
    // Kept, not deleted: a reviewed conflict is still something that happened.
    expect(await store.count("issues")).toBe(1);
    await waitFor(() =>
      expect(screen.queryByText("Needs Attention")).not.toBeInTheDocument(),
    );
  });
});

describe("the visible sync state", () => {
  it("counts the outbox as changes waiting", async () => {
    signedIn();
    stubServer({ customers: [AYESHA, BILAL] });
    stub("POST", SYNC_PATH, { networkError: true });

    renderApp(<App />, "/today");
    await confirmFirstCustomer();
    expect(await screen.findByText(/1 change waiting/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Next customer" }));
    await userEvent.click(await screen.findByRole("button", { name: "Confirm" }));
    expect(await screen.findByText(/2 changes waiting/)).toBeInTheDocument();
  });

  it("says Offline when the browser has no network", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });

    renderApp(<App />, "/today");
    await screen.findByRole("heading", { name: "Ayesha Khan" });
    setOnline(false);

    expect(await screen.findByText("Offline")).toBeInTheDocument();
    setOnline(true);
  });

  it("says Synced with the time of the last sync when the queue is empty", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });

    renderApp(<App />, "/today");
    expect(await screen.findByText(/Synced · Last synced/)).toBeInTheDocument();
  });
});

describe("working offline", () => {
  it("renders the round from the snapshot with no network at all", async () => {
    signedIn();
    stubServer({ customers: [AYESHA, BILAL] });

    renderApp(<App />, "/today");
    await screen.findByRole("heading", { name: "Ayesha Khan" });

    // Reopen with every request failing: nothing may be fetched from now on.
    reopenBrowser();
    setOnline(false);
    stub("GET", "/api/v1/tenant/settings", { networkError: true });
    stub("GET", "/api/v1/sync/changes", { networkError: true });
    stub("GET", "/api/v1/customers", { networkError: true });

    renderApp(<App />, "/today");
    expect(await screen.findByRole("heading", { name: "Ayesha Khan" })).toBeInTheDocument();
    expect(screen.getByText("Still to do (2)")).toBeInTheDocument();
    setOnline(true);
  });

  it("queues a confirm offline and pushes it on reconnect", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });
    renderApp(<App />, "/today");
    await screen.findByRole("heading", { name: "Ayesha Khan" });

    setOnline(false);
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));
    const store = await db();
    await waitFor(async () => expect(await store.count("outbox")).toBe(1));
    expect(requestsTo("POST", SYNC_PATH)).toHaveLength(0);

    stub("POST", SYNC_PATH, (request) =>
      pushResults({
        operation_id: opsOf(request.body)[0]!.operation_id as string,
        status: "APPLIED",
        entity: serviceRecord(),
      }),
    );
    setOnline(true);

    await waitFor(async () => expect(await store.count("outbox")).toBe(0));
  });

  it("says unavailable offline rather than inventing a customer list", async () => {
    signedIn();
    setOnline(false);
    stub("GET", "/api/v1/tenant/settings", { networkError: true });
    stub("GET", "/api/v1/sync/changes", { networkError: true });

    renderApp(<App />, "/customers");
    expect(await screen.findByRole("alert")).toHaveTextContent(/unavailable offline/i);
    setOnline(true);
  });
});

describe("two tenants on one browser", () => {
  it("keeps each tenant's data in its own database, and loses neither", async () => {
    const OTHER_TENANT = "22222222-2222-7222-8222-222222222222";

    signedIn();
    stubServer({ customers: [AYESHA] });
    stub("POST", SYNC_PATH, { networkError: true });

    renderApp(<App />, "/today");
    await confirmFirstCustomer();
    await waitFor(async () => expect(await (await db()).count("outbox")).toBe(1));

    // Sign out, then in as somebody else. Signing out must not delete work.
    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
    await screen.findByRole("heading", { name: "Sign in" });
    reopenBrowser();

    stub("POST", "/api/v1/auth/login", {
      body: {
        access_token: "b-access",
        refresh_token: "b-refresh",
        token_type: "bearer",
        expires_in: 3600,
        role: "OWNER_ADMIN",
        scope: "TENANT",
        tenant_id: OTHER_TENANT,
      },
    });
    stubServer({ customers: [] });

    renderApp(<App />, "/login");
    await userEvent.type(screen.getByLabelText("Email"), "owner@bravo.test");
    await userEvent.type(screen.getByLabelText("Password"), "correct horse");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByRole("navigation", { name: "Main" });

    const theirs = await openSyncDb(OTHER_TENANT);
    // Wait for B's own first sync to land, not merely for its engine to start:
    // `meta` gets a row before the pull finishes.
    await waitFor(async () => expect(await theirs.count("snapshot")).toBe(1));
    // Tenant B sees none of tenant A's customers — only its own settings row.
    expect(await theirs.count("outbox")).toBe(0);

    // …and tenant A's queued round is exactly where it was left.
    const ours = await openSyncDb(TENANT_ID);
    expect(await ours.count("outbox")).toBe(1);
  });
});

describe("the change feed", () => {
  it("continues from the stored cursor rather than starting again", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });
    stub("GET", "/api/v1/sync/changes", { body: changesResponse({ head: 4100 }) });

    renderApp(<App />, "/today");
    await screen.findByRole("heading", { name: "Ayesha Khan" });

    const store = await db();
    await waitFor(async () =>
      expect((await store.get("meta", "sync_cursor"))?.value).toBe(4100),
    );

    stub("GET", "/api/v1/sync/changes", {
      body: changesResponse({
        since: 4100,
        cursor: 4200,
        head: 4200,
        changes: [
          {
            entity: "customer",
            id: BILAL.id,
            row_version: 4200,
            data: BILAL,
          },
        ],
      }),
    });
    await testEngine().syncNow();

    const asked = requestsTo("GET", "/api/v1/sync/changes").map((r) =>
      new URL(r.url, "http://localhost").searchParams.get("since"),
    );
    expect(asked).toContain("4100");
    expect(await screen.findByText("Still to do (2)")).toBeInTheDocument();
  });

  it("never advances the cursor past a page it did not receive", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });
    stub("GET", "/api/v1/sync/changes", { body: changesResponse({ head: 10 }) });

    renderApp(<App />, "/today");
    await screen.findByRole("heading", { name: "Ayesha Khan" });
    const store = await db();
    await waitFor(async () =>
      expect((await store.get("meta", "sync_cursor"))?.value).toBe(10),
    );

    // A page arrives, then the connection dies before the next one.
    let call = 0;
    stub("GET", "/api/v1/sync/changes", () => {
      call += 1;
      return call === 1
        ? {
            body: changesResponse({
              since: 10,
              cursor: 20,
              has_more: true,
              head: 40,
              changes: [
                { entity: "customer", id: BILAL.id, row_version: 20, data: BILAL },
              ],
            }),
          }
        : { networkError: true };
    });
    await engineFor(TENANT_ID).syncNow();

    // The cursor sits on the last row actually applied, not on the head.
    expect((await store.get("meta", "sync_cursor"))?.value).toBe(20);
  });

  it("stores only the current business date's service records", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });
    stub("GET", "/api/v1/sync/changes", { body: changesResponse({ head: 700 }) });

    renderApp(<App />, "/today");
    await screen.findByRole("heading", { name: "Ayesha Khan" });
    const store = await db();
    await waitFor(async () =>
      expect((await store.get("meta", "sync_cursor"))?.value).toBe(700),
    );

    const today = serviceRecord({ id: "t0000000-0000-7000-8000-000000000001" });
    const otherDay = serviceRecord({
      id: "t0000000-0000-7000-8000-000000000002",
      service_date: "2026-08-30",
    });
    stub("GET", "/api/v1/sync/changes", {
      body: changesResponse({
        since: 700,
        cursor: 720,
        head: 720,
        changes: [
          { entity: "daily_service_record", id: today.id, row_version: 710, data: today },
          { entity: "daily_service_record", id: otherDay.id, row_version: 720, data: otherDay },
        ],
      }),
    });
    await testEngine().syncNow();

    // The other day's row was seen — the cursor moved past it — but not stored:
    // no screen renders it, so nothing pretends it is available offline.
    await waitFor(async () =>
      expect((await store.get("meta", "sync_cursor"))?.value).toBe(720),
    );
    expect(await store.get("snapshot", `daily_service_record:${today.id}`)).toBeTruthy();
    expect(await store.get("snapshot", `daily_service_record:${otherDay.id}`)).toBeUndefined();
  });

  it("cleaning the snapshot never touches queued work or unresolved issues", async () => {
    signedIn();
    stubServer({ customers: [AYESHA, BILAL] });
    stub("POST", SYNC_PATH, { networkError: true });

    renderApp(<App />, "/today");
    await confirmFirstCustomer();
    const store = await db();
    await waitFor(async () => expect(await store.count("outbox")).toBe(1));

    // Park an issue beside it.
    await store.put("issues", {
      operation_id: "99999999-9999-7999-8999-999999999999",
      envelope: {
        operation_id: "99999999-9999-7999-8999-999999999999",
        op_type: "service.record",
        payload: {
          customer_id: BILAL.id,
          kind: "SERVICE",
          quantity: "1",
          input_method: "BUTTON",
        },
        client_created_at: "2026-09-03T05:00:00.000Z",
      },
      context: {
        customer_id: BILAL.id,
        customer_name: BILAL.name,
        service_date: "2026-08-30",
        kind: "SERVICE",
        quantity: "1",
        unit_label: "bottle",
      },
      verdict: "CONFLICT",
      error: { code: "SERVICE_ALREADY_RECORDED", detail: "already recorded" },
      server_state: null,
      created_at: "2026-09-03T05:00:00.000Z",
      resolved_at: null,
    });

    // The queued entry is waiting out its backoff, so the pull — and with it the
    // snapshot cleanup — runs.
    await testEngine().syncNow();

    expect(await store.count("outbox")).toBe(1);
    expect(await store.count("issues")).toBe(1);
    const [issue] = await store.getAll("issues");
    expect(issue!.resolved_at).toBeNull();
  });

  it("resynchronises from zero when the feed's version changes", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });
    stub("GET", "/api/v1/sync/changes", { body: changesResponse({ head: 900 }) });

    renderApp(<App />, "/today");
    await screen.findByRole("heading", { name: "Ayesha Khan" });
    const store = await db();
    await waitFor(async () =>
      expect((await store.get("meta", "feed_version"))?.value).toBe(1),
    );

    stub("GET", "/api/v1/sync/changes", { body: changesResponse({ feed_version: 2, head: 900 }) });
    await engineFor(TENANT_ID).syncNow();

    await waitFor(async () =>
      expect((await store.get("meta", "feed_version"))?.value).toBe(2),
    );
    // The queue and the issues store are not caches and are never cleared by this.
    expect(await store.count("outbox")).toBe(0);
    expect(await store.count("issues")).toBe(0);
  });
});

describe("what the client never does", () => {
  it("shows no money on the register, offline or on", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });

    renderApp(<App />, "/today");
    await screen.findByRole("heading", { name: "Ayesha Khan" });
    expect(screen.queryByText(/PKR/)).not.toBeInTheDocument();
  });

  it("sends no tenant_id on a sync push", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });
    stub("POST", SYNC_PATH, { networkError: true });

    renderApp(<App />, "/today");
    await confirmFirstCustomer();
    await waitFor(() => expect(requestsTo("POST", SYNC_PATH)).not.toHaveLength(0));

    const body = requestsTo("POST", SYNC_PATH)[0]!.body as Record<string, unknown>;
    expect(body).not.toHaveProperty("tenant_id");
    expect(JSON.stringify(body)).not.toContain("tenant");
  });

  it("does not busy-loop against a server that keeps failing", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });
    stub("POST", SYNC_PATH, { networkError: true });

    renderApp(<App />, "/today");
    await confirmFirstCustomer();
    await waitFor(() => expect(requestsTo("POST", SYNC_PATH)).not.toHaveLength(0));

    // Three explicit syncs in a row: the backoff holds the entry back rather
    // than hammering, so no new request goes out.
    const before = requestsTo("POST", SYNC_PATH).length;
    await testEngine().syncNow();
    await testEngine().syncNow();
    expect(requestsTo("POST", SYNC_PATH)).toHaveLength(before);
  });
});

describe("errors the person is shown", () => {
  it("explains a rejection in one sentence, without the server's own words", async () => {
    signedIn();
    stubServer({
      customers: [AYESHA],
      push: (request) =>
        pushResults({
          operation_id: opsOf(request.body)[0]!.operation_id as string,
          status: "REJECTED",
          ...errorBody("VALIDATION", "quantity: value is not a valid decimal"),
        }),
    });

    renderApp(<App />, "/attention");
    // Queue something from the round first.
    cleanup();
    renderApp(<App />, "/today");
    await confirmFirstCustomer();

    await userEvent.click(await screen.findByText("Needs Attention"));
    expect(
      await screen.findByText("Please check the highlighted fields and try again."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/not a valid decimal/)).not.toBeInTheDocument();
  });
});
