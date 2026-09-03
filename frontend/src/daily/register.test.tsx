import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { App } from "@/App";
import {
  customer,
  pushResults,
  renderApp,
  serviceRecord,
  SETTINGS,
  signedIn,
  stubServer,
  testEngine,
} from "@/test/fixtures";
import { requestsTo, stub } from "@/test/http";

/**
 * The Daily Register, offline-first.
 *
 * Everything the screen renders comes from the device's snapshot of what the
 * server said, and every write leaves through the outbox. The two things worth
 * testing hardest are therefore:
 *
 *  1. the queue is durable **before** the network is touched (SYN-5);
 *  2. a queued entry is never described as recorded — "waiting to sync" and
 *     "Recorded" are statements about two different parties.
 */

const AYESHA = customer({ name: "Ayesha Khan" });
const BILAL = customer({
  id: "dddddddd-dddd-7ddd-8ddd-dddddddddddd",
  code: "C-002",
  name: "Bilal Ahmed",
  default_quantity: "1.000",
});
const SYNC_PATH = "/api/v1/sync/operations";

function opsOf(body: unknown): Array<Record<string, unknown>> {
  return (body as { operations: Array<Record<string, unknown>> }).operations;
}

function payloadOf(body: unknown): Record<string, unknown> {
  return opsOf(body)[0]!.payload as Record<string, unknown>;
}

/** Answer every pushed operation APPLIED, echoing back a record. */
const applyAll = (request: { body: unknown }) =>
  pushResults(
    ...opsOf(request.body).map((operation) => ({
      operation_id: operation.operation_id as string,
      status: "APPLIED",
      entity: serviceRecord({
        customer_id: (operation.payload as { customer_id: string }).customer_id,
        kind: (operation.payload as { kind: "SERVICE" | "SKIP" }).kind,
        quantity: (operation.payload as { quantity?: string }).quantity ?? "0.000",
      }),
    })),
  );

describe("the daily register", () => {
  it("shows the business date the server stated, never one it worked out", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });

    renderApp(<App />, "/today");
    // 2026-09-03 is SETTINGS.business_date, from the tenant's own timezone.
    const heading = await screen.findByRole("heading", { name: /September/ });
    expect(heading).toHaveTextContent("2026");
    expect(requestsTo("GET", `/api/v1/service/day/${SETTINGS.business_date}`)).toHaveLength(1);
  });

  it("shows the first pending customer with their usual quantity", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });

    renderApp(<App />, "/today");
    expect(await screen.findByRole("heading", { name: "Ayesha Khan" })).toBeInTheDocument();
    expect(screen.getByLabelText("Quantity in bottle")).toHaveValue("2.000");
  });

  it("separates who is still to do from who is done", async () => {
    signedIn();
    stubServer({
      customers: [AYESHA, BILAL],
      day: {
        service_date: SETTINGS.business_date,
        business_date: SETTINGS.business_date,
        items: [serviceRecord({ customer_id: AYESHA.id })],
      },
    });

    renderApp(<App />, "/today");
    expect(await screen.findByText("Still to do (1)")).toBeInTheDocument();
    expect(screen.getByText("Done (1)")).toBeInTheDocument();
    expect(screen.getByText(/1 of 2 recorded/)).toBeInTheDocument();
  });
});

describe("confirming service", () => {
  it("queues the operation before any network call is made", async () => {
    signedIn();
    // No push stub: the request throws "no stub", i.e. the network is a wall.
    stubServer({ customers: [AYESHA] });

    renderApp(<App />, "/today");
    await screen.findByRole("heading", { name: "Ayesha Khan" });
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));

    const db = await testEngine().db();
    await waitFor(async () => expect(await db.count("outbox")).toBe(1));
    const [entry] = await db.getAll("outbox");
    expect(entry!.envelope.op_type).toBe("service.record");
    expect(entry!.envelope.payload.quantity).toBe("2.000");
    expect(entry!.context.customer_name).toBe("Ayesha Khan");
  });

  it("sends the customer, the quantity and the server's own business date", async () => {
    signedIn();
    stubServer({ customers: [AYESHA], push: applyAll });

    renderApp(<App />, "/today");
    await screen.findByRole("heading", { name: "Ayesha Khan" });
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));

    await waitFor(() => expect(requestsTo("POST", SYNC_PATH)).not.toHaveLength(0));
    const body = requestsTo("POST", SYNC_PATH)[0]!.body;
    expect(opsOf(body)[0]!.op_type).toBe("service.record");
    expect(payloadOf(body)).toEqual({
      customer_id: AYESHA.id,
      kind: "SERVICE",
      quantity: "2.000",
      // The queued intent carries the business date it was made for, so a round
      // recorded on one day and synchronised on the next is not refiled.
      service_date: SETTINGS.business_date,
      input_method: "BUTTON",
    });
    expect(body).not.toHaveProperty("tenant_id");
  });

  it("says saved on this device, not recorded, until the server answers", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });

    renderApp(<App />, "/today");
    await screen.findByRole("heading", { name: "Ayesha Khan" });
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));

    expect(
      await screen.findByText(/saved on this device — waiting to sync/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/^Recorded /)).not.toBeInTheDocument();
  });

  it("says recorded once the server has accepted it", async () => {
    signedIn();
    stubServer({ customers: [AYESHA], push: applyAll });

    renderApp(<App />, "/today");
    await screen.findByRole("heading", { name: "Ayesha Khan" });
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));

    expect(await screen.findByText("Recorded 2.000 bottle.")).toBeInTheDocument();
  });

  it("moves to the next customer when asked", async () => {
    signedIn();
    stubServer({ customers: [AYESHA, BILAL], push: applyAll });

    renderApp(<App />, "/today");
    await screen.findByRole("heading", { name: "Ayesha Khan" });
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await screen.findByRole("button", { name: "Next customer" });
    await userEvent.click(screen.getByRole("button", { name: "Next customer" }));

    expect(await screen.findByRole("heading", { name: "Bilal Ahmed" })).toBeInTheDocument();
  });
});

describe("the quantity control", () => {
  it("steps by whole units and stops at zero", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });

    renderApp(<App />, "/today");
    await screen.findByRole("heading", { name: "Ayesha Khan" });
    const field = screen.getByLabelText("Quantity in bottle");

    await userEvent.click(screen.getByRole("button", { name: "Decrease bottle" }));
    expect(field).toHaveValue("1");
    await userEvent.click(screen.getByRole("button", { name: "Decrease bottle" }));
    await userEvent.click(screen.getByRole("button", { name: "Decrease bottle" }));
    expect(field).toHaveValue("0");
    expect(screen.getByRole("button", { name: "Confirm" })).toBeDisabled();
  });

  it("accepts a decimal quantity and sends it as typed", async () => {
    signedIn();
    stubServer({ customers: [AYESHA], push: applyAll });

    renderApp(<App />, "/today");
    await screen.findByRole("heading", { name: "Ayesha Khan" });
    const field = screen.getByLabelText("Quantity in bottle");
    await userEvent.clear(field);
    await userEvent.type(field, "1.5");
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));

    await waitFor(() => expect(requestsTo("POST", SYNC_PATH)).not.toHaveLength(0));
    expect(payloadOf(requestsTo("POST", SYNC_PATH)[0]!.body).quantity).toBe("1.5");
  });

  it("refuses more precision than the server stores", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });

    renderApp(<App />, "/today");
    await screen.findByRole("heading", { name: "Ayesha Khan" });
    const field = screen.getByLabelText("Quantity in bottle");
    await userEvent.clear(field);
    await userEvent.type(field, "1.2345");

    expect(screen.getByRole("button", { name: "Confirm" })).toBeDisabled();
    expect(field).toHaveAttribute("aria-invalid", "true");
  });
});

describe("skipping a day", () => {
  it("queues a SKIP with no quantity, under its own op type", async () => {
    signedIn();
    stubServer({ customers: [AYESHA], push: applyAll });

    renderApp(<App />, "/today");
    await screen.findByRole("heading", { name: "Ayesha Khan" });
    await userEvent.click(screen.getByRole("button", { name: "Skip today" }));

    await waitFor(() => expect(requestsTo("POST", SYNC_PATH)).not.toHaveLength(0));
    const body = requestsTo("POST", SYNC_PATH)[0]!.body;
    expect(opsOf(body)[0]!.op_type).toBe("service.skip");
    expect(payloadOf(body)).toEqual({
      customer_id: AYESHA.id,
      kind: "SKIP",
      service_date: SETTINGS.business_date,
      input_method: "BUTTON",
    });
    expect(payloadOf(body)).not.toHaveProperty("quantity");
  });
});

describe("leaving a customer for later", () => {
  it("writes nothing at all and only moves the round on", async () => {
    signedIn();
    stubServer({ customers: [AYESHA, BILAL] });

    renderApp(<App />, "/today");
    await screen.findByRole("heading", { name: "Ayesha Khan" });
    await userEvent.click(screen.getByRole("button", { name: "Leave for later" }));

    expect(await screen.findByRole("heading", { name: "Bilal Ahmed" })).toBeInTheDocument();
    const db = await testEngine().db();
    expect(await db.count("outbox")).toBe(0);
    expect(requestsTo("POST", SYNC_PATH)).toHaveLength(0);
    // Still on the round, still pending, not skipped.
    const stillToDo = screen.getByText("Still to do (2)").closest("section")!;
    expect(within(stillToDo).getByText("Ayesha Khan")).toBeInTheDocument();
  });
});

describe("when the device has never synchronised", () => {
  it("says so instead of inventing a round", async () => {
    signedIn();
    stub("GET", "/api/v1/tenant/settings", { networkError: true });
    stub("GET", "/api/v1/sync/changes", { networkError: true });

    renderApp(<App />, "/today");
    // Nothing is invented: no customers, no date, no round — just the truth that
    // this device has never been given one.
    expect(await screen.findByText(/unavailable offline/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Confirm" })).not.toBeInTheDocument();
  });
});
