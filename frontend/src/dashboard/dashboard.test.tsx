import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { App } from "@/App";
import {
  customer,
  dashboardSummary,
  payment,
  renderApp,
  signedIn,
  statement,
  stubServer,
} from "@/test/fixtures";
import { requestsTo, stub } from "@/test/http";

/**
 * The owner-facing screens (P6).
 *
 * The through-line of every case here is the same one: **the client renders what
 * the server returned**. So the tests assert that figures appear exactly as the
 * fixture supplied them, that nothing on screen is a sum the client produced,
 * and — the cases that would actually cost money — that a payment cannot be
 * recorded offline and that an issued statement offers no way to change it.
 */

const AYESHA = customer();

function stubDashboard(summary = dashboardSummary(), outstanding?: unknown) {
  stub("GET", "/api/v1/dashboard/summary", { body: summary });
  stub("GET", "/api/v1/dashboard/outstanding", {
    body:
      outstanding ?? {
        currency: "PKR",
        currency_exponent: 2,
        items: [
          {
            customer_id: AYESHA.id,
            code: "C-001",
            name: "Ayesha Khan",
            area: "G-10",
            status: "ACTIVE",
            outstanding_minor: 70000,
          },
        ],
      },
  });
}

describe("the owner dashboard", () => {
  it("renders the server's figures and derives none of them", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });
    stubDashboard();

    renderApp(<App />, "/overview");

    // Scoped to the headline block: the same figure legitimately appears again
    // in the list below it, which is the point — one server answer, shown twice.
    const money = await screen.findByRole("region", { name: "Money" });
    // 70000 minor units at exponent 2, formatted for display only.
    expect(within(money).getByText("PKR 700.00")).toBeInTheDocument();
    // Sold this period and collected this period, both straight from the server.
    expect(within(money).getByText("PKR 1,000.00")).toBeInTheDocument();
    expect(within(money).getByText("PKR 300.00")).toBeInTheDocument();
    expect(within(money).getByText("1 customer with a balance")).toBeInTheDocument();
  });

  it("shows business generated unchanged when a payment has been reversed", async () => {
    // A-FIN-14 as the owner sees it: the void moved collections and outstanding
    // and left what the business sold exactly where it was. The client must
    // print the server's three answers and not attempt to reconcile them.
    signedIn();
    stubServer({ customers: [AYESHA] });
    stubDashboard(
      dashboardSummary({
        outstanding_minor: 100000,
        current_cycle: {
          business_generated_minor: 100000,
          billed_value_minor: 0,
          collected_minor: 0,
          outstanding_minor: 100000,
        },
        recent_payments: [
          {
            id: "dddddddd-dddd-7ddd-8ddd-dddddddddddd",
            customer_id: AYESHA.id,
            customer_name: "Ayesha Khan",
            customer_code: "C-001",
            amount_minor: 30000,
            method: "CASH",
            received_on: "2026-09-02",
            status: "VOIDED",
            reference: null,
            recorded_at: "2026-09-02T06:00:00+00:00",
          },
        ],
      }),
    );

    renderApp(<App />, "/overview");

    const money = await screen.findByRole("region", { name: "Money" });
    // Two tiles read PKR 1,000.00 — "Owed to you" and "Sold this period" — and
    // that they agree is exactly the point: the void put the balance back to the
    // full amount sold without touching what was sold.
    expect(within(money).getAllByText("PKR 1,000.00")).toHaveLength(2);
    // Collected fell to nothing, from the same ledger, by adjustment origin.
    expect(within(money).getByText("PKR 0.00")).toBeInTheDocument();
    // And the reversal itself is visible in the activity list, not hidden.
    const activity = await screen.findByRole("region", { name: /Recent payments/ });
    expect(within(activity).getByText(/· reversed/)).toBeInTheDocument();
  });

  it("says the figures are unavailable offline when none has ever been received", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });
    stubDashboard();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (navigator as any).__defineGetter__?.("onLine", () => false);
    Object.defineProperty(navigator, "onLine", { value: false, configurable: true });

    renderApp(<App />, "/overview");

    expect(await screen.findByText(/Unavailable offline/)).toBeInTheDocument();
    // And it did not go and ask, because it knows it cannot.
    expect(requestsTo("GET", "/api/v1/dashboard/summary")).toHaveLength(0);

    Object.defineProperty(navigator, "onLine", { value: true, configurable: true });
  });

  it("shows the last figures it was given, stamped, once it goes offline", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });
    stubDashboard();

    const view = renderApp(<App />, "/overview");
    await within(
      await screen.findByRole("region", { name: "Money" }),
    ).findByText("PKR 700.00");
    view.unmount();

    Object.defineProperty(navigator, "onLine", { value: false, configurable: true });
    renderApp(<App />, "/overview");

    // The cached summary, and an honest note about when it was read.
    const money = await screen.findByRole("region", { name: "Money" });
    expect(within(money).getByText("PKR 700.00")).toBeInTheDocument();
    expect(await screen.findByText(/Offline — showing the figures from/)).toBeInTheDocument();

    Object.defineProperty(navigator, "onLine", { value: true, configurable: true });
  });

  it("shows no commission or operating-cost figure anywhere", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });
    stubDashboard();

    renderApp(<App />, "/overview");
    await within(
      await screen.findByRole("region", { name: "Money" }),
    ).findByText("PKR 700.00");

    expect(screen.queryByText(/commission/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/estimated/i)).not.toBeInTheDocument();
  });
});

describe("statements", () => {
  it("lists issued statements and opens one, from the snapshot", async () => {
    signedIn();
    stubServer({ customers: [AYESHA], statements: [statement()] });

    renderApp(<App />, "/statements");

    const row = await screen.findByRole("button", { name: /Ayesha Khan/ });
    expect(screen.getByText("PKR 700.00")).toBeInTheDocument();

    await userEvent.click(row);

    const detail = await screen.findByRole("region", {
      name: /Statement for Ayesha Khan/,
    });
    // The five movement figures, split by origin, as the server sent them.
    expect(within(detail).getByText("Brought forward")).toBeInTheDocument();
    expect(within(detail).getByText("PKR 0.00")).toBeInTheDocument();
    expect(within(detail).getByText("PKR 1,000.00")).toBeInTheDocument();
    expect(within(detail).getByText("− PKR 300.00")).toBeInTheDocument();
    expect(within(detail).getByText("PKR 700.00")).toBeInTheDocument();
  });

  it("offers no way to change or delete an issued statement", async () => {
    signedIn();
    stubServer({ customers: [AYESHA], statements: [statement()] });

    renderApp(<App />, "/statements");
    await userEvent.click(await screen.findByRole("button", { name: /Ayesha Khan/ }));
    await screen.findByRole("region", { name: /Statement for Ayesha Khan/ });

    for (const label of [/edit/i, /delete/i, /remove/i, /recalculate/i, /reissue/i]) {
      expect(screen.queryByRole("button", { name: label })).not.toBeInTheDocument();
    }
    expect(screen.getByText(/It cannot be changed/)).toBeInTheDocument();
  });

  it("does not invent a way to close a period or issue a statement", async () => {
    signedIn();
    stubServer({ customers: [AYESHA], statements: [] });

    renderApp(<App />, "/statements");
    await screen.findByText(/No statements yet/);

    expect(
      screen.queryByRole("button", { name: /close (the )?period/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /issue statement/i }),
    ).not.toBeInTheDocument();
  });

  it("says statements are unavailable offline when nothing has synchronised", async () => {
    signedIn();
    // No stubServer: the device has never held a snapshot.
    stub("GET", "/api/v1/tenant/settings", { status: 500, body: {} });
    stub("GET", "/api/v1/sync/changes", { status: 500, body: {} });

    renderApp(<App />, "/statements");

    expect(await screen.findByText(/Unavailable offline/)).toBeInTheDocument();
  });
});

describe("recording a payment", () => {
  const CUSTOMER_DETAIL = {
    ...AYESHA,
    outstanding_minor: 70000,
    payment_status: "PARTIALLY_PAID",
  };

  function stubCustomerDetail() {
    stub("GET", `/api/v1/customers/${AYESHA.id}`, { body: CUSTOMER_DETAIL });
    stub("GET", `/api/v1/customers/${AYESHA.id}/payments`, { body: { items: [] } });
    stub("GET", `/api/v1/customers/${AYESHA.id}/statements`, { body: { items: [] } });
    stub("GET", `/api/v1/customers/${AYESHA.id}/history`, { body: { items: [] } });
  }

  it("sends a partial payment in minor units with one operation_id", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });
    stubCustomerDetail();
    stub("POST", "/api/v1/payments", {
      status: 201,
      body: { status: "APPLIED", entity: payment({ amount_minor: 25050 }) },
    });

    renderApp(<App />, `/customers/${AYESHA.id}/pay`);

    await userEvent.type(
      await screen.findByLabelText(/Amount received/),
      "250.50",
    );
    await userEvent.click(screen.getByRole("button", { name: "Record payment" }));

    await waitFor(() =>
      expect(requestsTo("POST", "/api/v1/payments")).toHaveLength(1),
    );
    const body = requestsTo("POST", "/api/v1/payments")[0]!.body as Record<
      string,
      unknown
    >;
    // Parsed by string surgery, never `250.50 * 100`.
    expect(body.amount_minor).toBe(25050);
    expect(body.method).toBe("CASH");
    expect(body.customer_id).toBe(AYESHA.id);
    expect(typeof body.operation_id).toBe("string");
    // SEC-3: the client never names a tenant.
    expect(body).not.toHaveProperty("tenant_id");
  });

  it("accepts a full payment and each of the three methods", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });
    stubCustomerDetail();
    stub("POST", "/api/v1/payments", {
      status: 201,
      body: { status: "APPLIED", entity: payment({ amount_minor: 70000 }) },
    });

    renderApp(<App />, `/customers/${AYESHA.id}/pay`);

    for (const label of ["Cash", "Bank transfer", "Other"]) {
      expect(await screen.findByRole("radio", { name: label })).toBeInTheDocument();
    }

    await userEvent.type(await screen.findByLabelText(/Amount received/), "700");
    await userEvent.click(screen.getByRole("radio", { name: "Bank transfer" }));
    await userEvent.click(screen.getByRole("button", { name: "Record payment" }));

    await waitFor(() =>
      expect(requestsTo("POST", "/api/v1/payments")).toHaveLength(1),
    );
    const body = requestsTo("POST", "/api/v1/payments")[0]!.body as Record<
      string,
      unknown
    >;
    expect(body.amount_minor).toBe(70000);
    expect(body.method).toBe("BANK_TRANSFER");
    expect(await screen.findByText(/Recorded PKR 700.00/)).toBeInTheDocument();
  });

  it("allows an overpayment and warns rather than forbidding it", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });
    stubCustomerDetail();
    stub("POST", "/api/v1/payments", {
      status: 201,
      body: { status: "APPLIED", entity: payment({ amount_minor: 100000 }) },
    });

    renderApp(<App />, `/customers/${AYESHA.id}/pay`);
    await userEvent.type(await screen.findByLabelText(/Amount received/), "1000");

    expect(await screen.findByText(/kept as credit/)).toBeInTheDocument();
    const button = screen.getByRole("button", { name: "Record payment" });
    expect(button).toBeEnabled();

    await userEvent.click(button);
    await waitFor(() =>
      expect(requestsTo("POST", "/api/v1/payments")).toHaveLength(1),
    );
    expect(
      (requestsTo("POST", "/api/v1/payments")[0]!.body as Record<string, unknown>)
        .amount_minor,
    ).toBe(100000);
  });

  it("never computes the resulting balance itself", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });
    stubCustomerDetail();
    stub("POST", "/api/v1/payments", {
      status: 201,
      body: { status: "APPLIED", entity: payment({ amount_minor: 25000 }) },
    });

    renderApp(<App />, `/customers/${AYESHA.id}/pay`);
    await userEvent.type(await screen.findByLabelText(/Amount received/), "250");
    await userEvent.click(screen.getByRole("button", { name: "Record payment" }));

    await screen.findByText(/Recorded PKR 250.00/);
    // 700 − 250 = 450 would be the client doing arithmetic. It must still show
    // the server's own outstanding figure, unchanged until the server re-states it.
    expect(screen.queryByText("PKR 450.00")).not.toBeInTheDocument();
    expect(screen.getByText(/owes PKR 700.00/)).toBeInTheDocument();
  });

  it("blocks recording offline and says why, sending nothing", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });
    stubCustomerDetail();
    stub("POST", "/api/v1/payments", { status: 201, body: {} });
    Object.defineProperty(navigator, "onLine", { value: false, configurable: true });

    renderApp(<App />, `/customers/${AYESHA.id}/pay`);

    expect(await screen.findByText(/You are offline/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Record payment" })).toBeDisabled();
    expect(screen.getByLabelText(/Amount received/)).toBeDisabled();
    expect(requestsTo("POST", "/api/v1/payments")).toHaveLength(0);

    Object.defineProperty(navigator, "onLine", { value: true, configurable: true });
  });

  it("retries a transport failure with the same operation_id", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });
    stubCustomerDetail();
    stub("POST", "/api/v1/payments", (_request, callIndex) =>
      callIndex === 0
        ? { networkError: true }
        : { status: 201, body: { status: "APPLIED", entity: payment() } },
    );

    renderApp(<App />, `/customers/${AYESHA.id}/pay`);
    await userEvent.type(await screen.findByLabelText(/Amount received/), "300");
    await userEvent.click(screen.getByRole("button", { name: "Record payment" }));

    await screen.findByText(/We are not sure this reached the server/);
    // The amount is locked: editing it and retrying would be a different request
    // under the same operation_id, which the server refuses (SYN-14).
    expect(screen.getByLabelText(/Amount received/)).toBeDisabled();

    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() =>
      expect(requestsTo("POST", "/api/v1/payments")).toHaveLength(2),
    );
    const [first, second] = requestsTo("POST", "/api/v1/payments");
    expect((first!.body as Record<string, unknown>).operation_id).toBe(
      (second!.body as Record<string, unknown>).operation_id,
    );
  });
});

describe("payment history and reversal", () => {
  it("shows voided payments rather than hiding them, and offers no delete", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });
    stub("GET", `/api/v1/customers/${AYESHA.id}`, {
      body: { ...AYESHA, outstanding_minor: 100000, payment_status: "UNPAID" },
    });
    stub("GET", `/api/v1/customers/${AYESHA.id}/payments`, {
      body: {
        items: [
          payment({ status: "VOIDED", voided_reason: "entered twice" }),
        ],
      },
    });
    stub("GET", `/api/v1/customers/${AYESHA.id}/statements`, { body: { items: [] } });
    stub("GET", `/api/v1/customers/${AYESHA.id}/history`, { body: { items: [] } });

    renderApp(<App />, `/customers/${AYESHA.id}`);

    expect(await screen.findByText(/reversed/)).toBeInTheDocument();
    expect(screen.getByText(/entered twice/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /delete/i })).not.toBeInTheDocument();
    // A payment that is already reversed cannot be reversed again.
    expect(screen.queryByRole("button", { name: "Reverse" })).not.toBeInTheDocument();
  });

  it("requires a reason to reverse, and sends it with one operation_id", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });
    stub("GET", `/api/v1/customers/${AYESHA.id}`, {
      body: { ...AYESHA, outstanding_minor: 70000, payment_status: "PARTIALLY_PAID" },
    });
    stub("GET", `/api/v1/customers/${AYESHA.id}/payments`, {
      body: { items: [payment()] },
    });
    stub("GET", `/api/v1/customers/${AYESHA.id}/statements`, { body: { items: [] } });
    stub("GET", `/api/v1/customers/${AYESHA.id}/history`, { body: { items: [] } });
    stub("POST", `/api/v1/payments/${payment().id}/void`, {
      body: { status: "APPLIED", entity: payment({ status: "VOIDED" }) },
    });

    renderApp(<App />, `/customers/${AYESHA.id}`);
    await userEvent.click(await screen.findByRole("button", { name: "Reverse" }));

    const confirm = await screen.findByRole("button", { name: "Reverse payment" });
    expect(confirm).toBeDisabled();

    await userEvent.type(screen.getByLabelText(/Why is it being reversed/), "duplicate");
    await userEvent.click(screen.getByRole("button", { name: "Reverse payment" }));

    await waitFor(() =>
      expect(
        requestsTo("POST", `/api/v1/payments/${payment().id}/void`),
      ).toHaveLength(1),
    );
    const body = requestsTo("POST", `/api/v1/payments/${payment().id}/void`)[0]!
      .body as Record<string, unknown>;
    expect(body.reason).toBe("duplicate");
    expect(typeof body.operation_id).toBe("string");
  });

  it("reads synchronised payments and statements offline", async () => {
    signedIn();
    stubServer({
      customers: [AYESHA],
      payments: [payment()],
      statements: [statement()],
    });

    // One online load to seed, then unmount and go offline.
    const view = renderApp(<App />, "/statements");
    await screen.findByRole("button", { name: /Ayesha Khan/ });
    view.unmount();

    Object.defineProperty(navigator, "onLine", { value: false, configurable: true });
    stub("GET", `/api/v1/customers/${AYESHA.id}`, { status: 500, body: {} });

    renderApp(<App />, "/statements");
    // Straight out of IndexedDB, with no network call behind it.
    expect(await screen.findByRole("button", { name: /Ayesha Khan/ })).toBeInTheDocument();
    expect(screen.getByText("PKR 700.00")).toBeInTheDocument();

    Object.defineProperty(navigator, "onLine", { value: true, configurable: true });
  });
});
