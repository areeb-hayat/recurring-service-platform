import { describe, expect, it } from "vitest";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { App } from "@/App";
import {
  customer,
  errorBody,
  renderApp,
  serviceRecord,
  SETTINGS,
  signedIn,
} from "@/test/fixtures";
import { requests, requestsTo, stub } from "@/test/http";

const DAY_PATH = `/api/v1/service/day/${SETTINGS.business_date}`;
const AYESHA = customer();
const BILAL = customer({
  id: "dddddddd-dddd-7ddd-8ddd-dddddddddddd",
  code: "C-002",
  name: "Bilal Ahmed",
  default_quantity: "1.5",
});

function stubRegister(options: { customers?: unknown[]; records?: unknown[] } = {}) {
  stub("GET", "/api/v1/tenant/settings", { body: SETTINGS });
  stub("GET", "/api/v1/customers", { body: { items: options.customers ?? [AYESHA, BILAL] } });
  stub("GET", DAY_PATH, {
    body: {
      service_date: SETTINGS.business_date,
      business_date: SETTINGS.business_date,
      items: options.records ?? [],
    },
  });
}

async function openRegister() {
  signedIn();
  renderApp(<App />, "/today");
  return screen.findByRole("heading", { name: AYESHA.name });
}

describe("the daily register", () => {
  it("asks the server for the day rather than deciding what today is", async () => {
    stubRegister();
    await openRegister();

    // The only dated read is for the business date the server named, and it was
    // made after that read — the client never picks a date of its own.
    const dated = requests.filter((r) => r.path.startsWith("/api/v1/service/day/"));
    expect(dated.map((r) => r.path)).toEqual([DAY_PATH]);
    expect(requests.indexOf(dated[0]!)).toBeGreaterThan(
      requests.findIndex((r) => r.path === "/api/v1/tenant/settings"),
    );
  });

  it("shows the first pending customer with their usual quantity", async () => {
    stubRegister();
    await openRegister();

    expect(screen.getByLabelText("Quantity in bottle")).toHaveValue("2.000");
    expect(screen.getByText("0 of 2 recorded")).toBeInTheDocument();
  });

  it("separates who is still to do from who is done", async () => {
    stubRegister({ records: [serviceRecord()] });
    signedIn();
    renderApp(<App />, "/today");

    expect(await screen.findByText("1 of 2 recorded")).toBeInTheDocument();

    const doneSection = screen.getByRole("heading", { name: "Done (1)" }).parentElement!;
    expect(within(doneSection).getByText(AYESHA.name)).toBeInTheDocument();
    expect(within(doneSection).getByText("2 bottle")).toBeInTheDocument();

    const todoSection = screen.getByRole("heading", { name: "Still to do (1)" }).parentElement!;
    expect(within(todoSection).getByText(BILAL.name)).toBeInTheDocument();
  });
});

describe("the quantity control", () => {
  it("steps by whole units and stops at zero", async () => {
    stubRegister();
    await openRegister();
    const field = screen.getByLabelText("Quantity in bottle");

    await userEvent.click(screen.getByRole("button", { name: "Increase bottle" }));
    expect(field).toHaveValue("3");

    await userEvent.click(screen.getByRole("button", { name: "Decrease bottle" }));
    await userEvent.click(screen.getByRole("button", { name: "Decrease bottle" }));
    expect(field).toHaveValue("1");
  });

  it("accepts a decimal quantity and sends it as typed", async () => {
    stubRegister();
    stub("POST", "/api/v1/service/records", {
      status: 201,
      body: { status: "APPLIED", entity: serviceRecord({ quantity: "1.5" }) },
    });
    await openRegister();

    const field = screen.getByLabelText("Quantity in bottle");
    await userEvent.clear(field);
    await userEvent.type(field, "1.5");
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));

    const sent = requestsTo("POST", "/api/v1/service/records")[0]?.body as Record<string, unknown>;
    expect(sent.quantity).toBe("1.5");
    expect(typeof sent.quantity).toBe("string");
  });

  it("refuses more precision than the server stores", async () => {
    stubRegister();
    await openRegister();

    const field = screen.getByLabelText("Quantity in bottle");
    await userEvent.clear(field);
    await userEvent.type(field, "1.2345");

    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("button", { name: "Confirm" })).toBeDisabled();
  });
});

describe("confirming service", () => {
  it("posts the customer, the quantity and no service date", async () => {
    stubRegister();
    stub("POST", "/api/v1/service/records", {
      status: 201,
      body: { status: "APPLIED", entity: serviceRecord() },
    });
    await openRegister();

    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));

    const sent = requestsTo("POST", "/api/v1/service/records")[0]?.body as Record<string, unknown>;
    expect(sent.customer_id).toBe(AYESHA.id);
    expect(sent.kind).toBe("SERVICE");
    expect(sent.quantity).toBe("2.000");
    expect(sent.input_method).toBe("BUTTON");
    // Omitted on purpose: only the server may resolve the tenant's today.
    expect(sent).not.toHaveProperty("service_date");
    expect(sent).not.toHaveProperty("tenant_id");
    expect(typeof sent.operation_id).toBe("string");
  });

  it("confirms what was recorded", async () => {
    stubRegister();
    stub("POST", "/api/v1/service/records", {
      status: 201,
      body: { status: "APPLIED", entity: serviceRecord() },
    });
    await openRegister();

    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));

    expect(await screen.findByText("Recorded 2 bottle.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next customer" })).toBeInTheDocument();
  });

  it("moves to the next customer when asked", async () => {
    stubRegister();
    stub("POST", "/api/v1/service/records", {
      status: 201,
      body: { status: "APPLIED", entity: serviceRecord() },
    });
    await openRegister();

    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await userEvent.click(await screen.findByRole("button", { name: "Next customer" }));

    expect(await screen.findByRole("heading", { name: BILAL.name })).toBeInTheDocument();
    expect(screen.getByLabelText("Quantity in bottle")).toHaveValue("1.5");
  });
});

describe("leaving a customer for later", () => {
  it("writes nothing at all and only moves the round on", async () => {
    stubRegister();
    await openRegister();

    await userEvent.click(screen.getByRole("button", { name: "Leave for later" }));

    // Different customer on screen…
    expect(await screen.findByRole("heading", { name: BILAL.name })).toBeInTheDocument();

    // …and not a single request left the app. No SERVICE record, no SKIP record,
    // no operation_id, no server mutation of any kind — so no ledger entry can
    // exist either. It is purely local progression through the round.
    expect(requestsTo("POST", "/api/v1/service/records")).toHaveLength(0);
    expect(requests.filter((r) => r.method !== "GET")).toHaveLength(0);
  });

  it("leaves the customer pending, not skipped", async () => {
    stubRegister();
    await openRegister();

    await userEvent.click(screen.getByRole("button", { name: "Leave for later" }));
    await screen.findByRole("heading", { name: BILAL.name });

    // Still nobody recorded, and both are still to do.
    expect(screen.getByText("0 of 2 recorded")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Still to do (2)" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /^Done/ })).not.toBeInTheDocument();
  });

  it("comes back round to the customer that was left", async () => {
    stubRegister();
    await openRegister();

    await userEvent.click(screen.getByRole("button", { name: "Leave for later" }));
    await screen.findByRole("heading", { name: BILAL.name });
    await userEvent.click(screen.getByRole("button", { name: "Leave for later" }));

    // Nobody is dropped from the round by being passed over.
    expect(await screen.findByRole("heading", { name: AYESHA.name })).toBeInTheDocument();
  });
});

describe("skipping a day", () => {
  it("records a SKIP with no quantity", async () => {
    stubRegister();
    stub("POST", "/api/v1/service/records", {
      status: 201,
      body: { status: "APPLIED", entity: serviceRecord({ kind: "SKIP", quantity: "0" }) },
    });
    await openRegister();

    await userEvent.click(screen.getByRole("button", { name: "Skip today" }));

    const sent = requestsTo("POST", "/api/v1/service/records")[0]?.body as Record<string, unknown>;
    expect(sent.kind).toBe("SKIP");
    expect(sent).not.toHaveProperty("quantity");
    expect(await screen.findByText("Skipped today.")).toBeInTheDocument();
  });
});

describe("failures", () => {
  it("keeps the same operation_id when a dropped request is retried", async () => {
    stubRegister();
    stub("POST", "/api/v1/service/records", (_request, attempt) =>
      attempt === 0
        ? { networkError: true }
        : { status: 201, body: { status: "DUPLICATE", entity: serviceRecord() } },
    );
    await openRegister();

    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not reach the server. Check your connection and try again.",
    );

    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByText("Recorded 2 bottle.");

    const sent = requestsTo("POST", "/api/v1/service/records");
    expect(sent).toHaveLength(2);
    expect(sent[0]?.body).toEqual(sent[1]?.body);
    expect((sent[0]?.body as { operation_id: string }).operation_id).toBe(
      (sent[1]?.body as { operation_id: string }).operation_id,
    );
  });

  it("locks the quantity while an operation is unresolved", async () => {
    stubRegister();
    stub("POST", "/api/v1/service/records", { networkError: true });
    await openRegister();

    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await screen.findByRole("alert");

    // Editing and retrying would send a different payload under the same
    // operation_id, which the server correctly refuses (SYN-14).
    expect(screen.getByLabelText("Quantity in bottle")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Increase bottle" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Confirm" })).toBeDisabled();
  });

  it("frees the control again once the operation is discarded", async () => {
    stubRegister();
    stub("POST", "/api/v1/service/records", { networkError: true });
    await openRegister();

    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await screen.findByRole("alert");
    await userEvent.click(screen.getByRole("button", { name: "Discard" }));

    expect(screen.getByLabelText("Quantity in bottle")).toBeEnabled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("explains a conflict without offering a silent overwrite", async () => {
    stubRegister();
    stub("POST", "/api/v1/service/records", {
      status: 409,
      body: errorBody("SERVICE_ALREADY_RECORDED", "an active service record already exists"),
    });
    await openRegister();

    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "Today is already recorded for this customer. Reload to see what was saved.",
    );
    // A verdict is terminal: there is nothing to retry with the same id.
    expect(within(alert).queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
  });

  it("asks for the cycle to be closed rather than showing a database message", async () => {
    stubRegister();
    stub("POST", "/api/v1/service/records", {
      status: 409,
      body: errorBody("CYCLE_ROLLOVER_REQUIRED", "the tenant's only OPEN cycle ended before today"),
    });
    await openRegister();

    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "The current billing period has ended. Close it before recording more work.",
    );
    expect(alert).not.toHaveTextContent("OPEN cycle");
  });

  it("offers a retry when the day cannot be loaded at all", async () => {
    stub("GET", "/api/v1/tenant/settings", { body: SETTINGS });
    stub("GET", "/api/v1/customers", { body: { items: [AYESHA] } });
    stub("GET", DAY_PATH, { status: 500, body: {} });
    signedIn();

    renderApp(<App />, "/today");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The server had a problem. Nothing was lost — try again.");
    expect(within(alert).getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});
