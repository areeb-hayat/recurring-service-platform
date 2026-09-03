import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { App } from "@/App";
import { createOperation } from "@/api/operation";
import { recordService, type ServiceIntent } from "@/api/service";
import { loadSession } from "@/auth/session";
import {
  customer,
  pushResults,
  renderApp,
  serviceRecord,
  signedIn,
  stubServer,
} from "@/test/fixtures";
import { requestsTo, stub } from "@/test/http";
import { engineFor } from "@/sync/engine";

/**
 * Refresh-and-replay, for a **mutation**.
 *
 * An expiring access token in the middle of a round is ordinary, not exceptional:
 * the token lives 60 minutes and a round can outlast it. So the interesting case
 * is not a 401 on a read, it is a 401 on the push that carries a delivery.
 *
 * The danger is obvious once stated. If the client responded to a 401 by
 * re-deriving the request — or if any layer regenerated the envelope — the replay
 * would carry a *new* `operation_id`, the server would see two unrelated
 * operations, and the customer would be billed twice for one delivery. The
 * `operation_id` is what makes the replay safe, so it must survive the refresh
 * untouched.
 *
 * P5 raises the stakes and lowers the risk at once: the envelope is in IndexedDB
 * before the first attempt, so an authentication failure cannot lose the entry
 * either — it stays queued for whoever signs in next.
 */

const AYESHA = customer();
const SYNC_PATH = "/api/v1/sync/operations";

const TOKENS = {
  access_token: "new-access",
  refresh_token: "new-refresh",
  token_type: "bearer",
  expires_in: 3600,
  role: "OWNER_ADMIN",
  scope: "TENANT",
  tenant_id: "11111111-1111-7111-8111-111111111111",
};

const unauthenticated = {
  status: 401,
  body: { error: { code: "UNAUTHENTICATED", detail: "access token has expired" } },
};

function testEngineFrom() {
  return engineFor("11111111-1111-7111-8111-111111111111");
}

function operationIdOf(body: unknown): string {
  const operations = (body as { operations: Array<{ operation_id: string }> }).operations;
  return operations[0]!.operation_id;
}

describe("a mutation that meets an expired token", () => {
  it("refreshes once and replays the identical envelope, operation_id included", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });
    stub("POST", "/api/v1/auth/refresh", { body: TOKENS });
    stub("POST", SYNC_PATH, (request, attempt) =>
      attempt === 0
        ? unauthenticated
        : // The server already applied the first attempt, so the replay is
          // answered DUPLICATE with the original result (P0 §7.6).
          pushResults({
            operation_id: operationIdOf(request.body),
            status: "DUPLICATE",
            entity: serviceRecord(),
          }),
    );

    renderApp(<App />, "/today");
    await screen.findByRole("heading", { name: AYESHA.name });
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));

    await waitFor(() => expect(requestsTo("POST", SYNC_PATH)).toHaveLength(2));
    const sent = requestsTo("POST", SYNC_PATH);

    // operation_id X went out, operation_id X came back.
    expect(operationIdOf(sent[0]!.body)).toBe(operationIdOf(sent[1]!.body));
    // …and nothing else about the request changed either.
    expect(sent[1]!.body).toEqual(sent[0]!.body);

    // Exactly one refresh, and the replay used the token it produced.
    expect(requestsTo("POST", "/api/v1/auth/refresh")).toHaveLength(1);
    expect(sent[0]!.headers.authorization).toBe("Bearer access-token");
    expect(sent[1]!.headers.authorization).toBe("Bearer new-access");

    // One logical business operation: the queue drains, nothing is left waiting.
    await waitFor(() =>
      expect(screen.queryByText(/waiting to sync/i)).not.toBeInTheDocument(),
    );
    expect(requestsTo("POST", SYNC_PATH)).toHaveLength(2);
  });

  it("does not retry a second time when the replay also 401s, and keeps the work", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });
    stub("POST", "/api/v1/auth/refresh", { body: TOKENS });
    stub("POST", SYNC_PATH, unauthenticated);

    renderApp(<App />, "/today");
    await screen.findByRole("heading", { name: AYESHA.name });
    const engine = testEngineFrom();
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));

    // One original, one replay, then it stops and the session ends. A loop here
    // would be a mutation storm against a server that keeps refusing.
    await waitFor(() => expect(requestsTo("POST", SYNC_PATH)).toHaveLength(2));
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(loadSession()).toBeNull();

    // Re-authentication must not cost the round: the entry is still queued.
    const db = await engine.db();
    expect(await db.count("outbox")).toBe(1);
  });

  it("does not replay when the refresh itself fails", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });
    stub("POST", "/api/v1/auth/refresh", unauthenticated);
    stub("POST", SYNC_PATH, unauthenticated);

    renderApp(<App />, "/today");
    await screen.findByRole("heading", { name: AYESHA.name });
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));

    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "Sign in" })).toBeInTheDocument(),
    );
    expect(requestsTo("POST", SYNC_PATH)).toHaveLength(1);
  });
});

describe("several requests meeting the same expired token", () => {
  it("shares one refresh instead of starting a storm", async () => {
    signedIn();
    stub("POST", "/api/v1/auth/refresh", { body: TOKENS });
    stub("POST", "/api/v1/service/records", (_request, attempt) =>
      attempt < 3
        ? unauthenticated
        : { status: 201, body: { status: "APPLIED", entity: serviceRecord() } },
    );

    const intents: ServiceIntent[] = [
      { customer_id: "c1", kind: "SERVICE", quantity: "1", input_method: "BUTTON" },
      { customer_id: "c2", kind: "SERVICE", quantity: "2", input_method: "BUTTON" },
      { customer_id: "c3", kind: "SERVICE", quantity: "3", input_method: "BUTTON" },
    ];
    const envelopes = intents.map((intent) => createOperation("service.record", intent));

    await Promise.all(envelopes.map((envelope) => recordService(envelope)));

    // Three 401s, one refresh — not three.
    expect(requestsTo("POST", "/api/v1/auth/refresh")).toHaveLength(1);

    const sent = requestsTo("POST", "/api/v1/service/records");
    expect(sent).toHaveLength(6);

    // Each operation was replayed under its own original id: three distinct ids,
    // each appearing exactly twice, and none newly generated for the replay.
    const ids = sent.map((r) => (r.body as { operation_id: string }).operation_id);
    const original = envelopes.map((e) => e.operation_id);
    expect(new Set(ids)).toEqual(new Set(original));
    for (const id of original) {
      expect(ids.filter((sentId) => sentId === id)).toHaveLength(2);
    }

    // Every replay carried the refreshed token.
    for (const request of sent.slice(3)) {
      expect(request.headers.authorization).toBe("Bearer new-access");
    }
  });

  it("stores the refreshed session once, not once per request", async () => {
    signedIn();
    stub("POST", "/api/v1/auth/refresh", { body: TOKENS });
    stub("POST", "/api/v1/service/records", (_request, attempt) =>
      attempt < 2
        ? unauthenticated
        : { status: 201, body: { status: "APPLIED", entity: serviceRecord() } },
    );

    await Promise.all([
      recordService(
        createOperation("service.record", {
          customer_id: "c1",
          kind: "SERVICE",
          quantity: "1",
          input_method: "BUTTON",
        }),
      ),
      recordService(
        createOperation("service.record", {
          customer_id: "c2",
          kind: "SERVICE",
          quantity: "2",
          input_method: "BUTTON",
        }),
      ),
    ]);

    expect(loadSession()?.access_token).toBe("new-access");
    expect(loadSession()?.refresh_token).toBe("new-refresh");
  });
});
