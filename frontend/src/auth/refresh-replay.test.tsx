import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { App } from "@/App";
import { createOperation } from "@/api/operation";
import { recordService, type ServiceIntent } from "@/api/service";
import { loadSession } from "@/auth/session";
import { customer, renderApp, serviceRecord, SETTINGS, signedIn } from "@/test/fixtures";
import { requestsTo, stub } from "@/test/http";

/**
 * Refresh-and-replay, for a **mutation**.
 *
 * An expiring access token in the middle of a round is ordinary, not exceptional:
 * the token lives 60 minutes and a round can outlast it. So the interesting case
 * is not a 401 on a read, it is a 401 on the POST that records a delivery.
 *
 * The danger is obvious once stated. If the client responded to a 401 by
 * re-deriving the request — or if any layer regenerated the envelope — the replay
 * would carry a *new* `operation_id`, the server would see two unrelated
 * operations, and the customer would be billed twice for one delivery. The
 * `operation_id` is what makes the replay safe, so it must survive the refresh
 * untouched.
 */

const DAY_PATH = `/api/v1/service/day/${SETTINGS.business_date}`;
const AYESHA = customer();

const TOKENS = {
  access_token: "new-access",
  refresh_token: "new-refresh",
  token_type: "bearer",
  expires_in: 3600,
  role: "OWNER_ADMIN",
  scope: "TENANT",
  tenant_id: "11111111-1111-7111-8111-111111111111",
};

function stubRegister() {
  stub("GET", "/api/v1/tenant/settings", { body: SETTINGS });
  stub("GET", "/api/v1/customers", { body: { items: [AYESHA] } });
  stub("GET", DAY_PATH, {
    body: {
      service_date: SETTINGS.business_date,
      business_date: SETTINGS.business_date,
      items: [],
    },
  });
}

const unauthenticated = {
  status: 401,
  body: { error: { code: "UNAUTHENTICATED", detail: "access token has expired" } },
};

describe("a mutation that meets an expired token", () => {
  it("refreshes once and replays the identical body, operation_id included", async () => {
    signedIn();
    stubRegister();
    stub("POST", "/api/v1/auth/refresh", { body: TOKENS });
    stub("POST", "/api/v1/service/records", (_request, attempt) =>
      attempt === 0
        ? unauthenticated
        : // The server has already applied the first attempt's operation, so the
          // replay is answered DUPLICATE with the original result (P0 §7.6).
          { status: 201, body: { status: "DUPLICATE", entity: serviceRecord() } },
    );

    renderApp(<App />, "/today");
    await screen.findByRole("heading", { name: AYESHA.name });
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));

    const sent = requestsTo("POST", "/api/v1/service/records");
    expect(sent).toHaveLength(2);

    const first = sent[0]!.body as Record<string, unknown>;
    const second = sent[1]!.body as Record<string, unknown>;

    // operation_id X went out, operation_id X came back.
    expect(first.operation_id).toBe(second.operation_id);
    expect(typeof first.operation_id).toBe("string");
    // …and nothing else about the request changed either.
    expect(second).toEqual(first);

    // Exactly one refresh, and the replay used the token it produced.
    expect(requestsTo("POST", "/api/v1/auth/refresh")).toHaveLength(1);
    expect(sent[0]!.headers.authorization).toBe("Bearer access-token");
    expect(sent[1]!.headers.authorization).toBe("Bearer new-access");

    // One logical business operation: one success, nothing queued, no second try.
    expect(await screen.findByText("Recorded 2 bottle.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(requestsTo("POST", "/api/v1/service/records")).toHaveLength(2);
  });

  it("does not retry a second time when the replay also 401s", async () => {
    signedIn();
    stubRegister();
    stub("POST", "/api/v1/auth/refresh", { body: TOKENS });
    stub("POST", "/api/v1/service/records", unauthenticated);

    renderApp(<App />, "/today");
    await screen.findByRole("heading", { name: AYESHA.name });
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));

    // One original, one replay, then it stops and the session ends. A loop here
    // would be a mutation storm against a server that keeps refusing.
    expect(requestsTo("POST", "/api/v1/service/records")).toHaveLength(2);
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(loadSession()).toBeNull();
  });

  it("does not replay when the refresh itself fails", async () => {
    signedIn();
    stubRegister();
    stub("POST", "/api/v1/auth/refresh", unauthenticated);
    stub("POST", "/api/v1/service/records", unauthenticated);

    renderApp(<App />, "/today");
    await screen.findByRole("heading", { name: AYESHA.name });
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));

    expect(requestsTo("POST", "/api/v1/service/records")).toHaveLength(1);
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
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
