import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { App } from "@/App";
import type { CostItem, CostLine, CostSummary } from "@/api/types";
import { customer, renderApp, signedIn, stubServer } from "@/test/fixtures";
import { requestsTo, stub } from "@/test/http";

/**
 * Running costs (P6).
 *
 * The four things worth proving about this screen:
 *
 * 1. **Estimated, invoiced and the difference are three separate figures**, and
 *    a missing one shows as a dash rather than a zero — an invoice that has not
 *    arrived is not an invoice for nothing.
 * 2. **The rate is data.** The prices on screen came from the API and the form
 *    can change them; nothing is hard-coded, and the vendor names below live
 *    only in this fixture.
 * 3. **The scenario calculator is planning**, priced on the server from the
 *    configured rate, and writes nothing.
 * 4. **It never mixes with commission** — there is none on this screen — and it
 *    is honest about being online-only.
 */

const ITEM_ID = "77777777-7777-7777-8777-777777777777";
const RATE_ID = "88888888-8888-7888-8888-888888888888";

const RATE = {
  id: RATE_ID,
  cost_item_id: ITEM_ID,
  effective_from: "2026-01-01",
  effective_to: null,
  unit: "audio_hour",
  // $0.22 an hour, as a row in the database — never a constant in the app.
  unit_price_minor: 22,
  fixed_amount_minor: null,
  fixed_recurrence: null,
  currency: "USD",
  currency_exponent: 2,
  source_note: null,
  created_at: "2026-01-01T00:00:00+00:00",
};

const ITEM: CostItem = {
  id: ITEM_ID,
  code: "VOICE_STT",
  name: "Speech to text",
  description: null,
  status: "ACTIVE",
  created_at: "2026-01-01T00:00:00+00:00",
  rates: [RATE],
};

function line(overrides: Partial<CostLine> = {}): CostLine {
  return {
    cost_item_id: ITEM_ID,
    code: "VOICE_STT",
    name: "Speech to text",
    period_month: "2026-09-01",
    currency: "USD",
    currency_exponent: 2,
    rate: RATE,
    usage_quantity: "20.833333",
    usage_unit: "audio_hour",
    usage_inputs: { commands_per_day: 500, seconds_per_command: "5" },
    estimated_amount_minor: 458,
    actual_amount_minor: 471,
    variance_minor: 13,
    actual_invoice_reference: "INV-1",
    usage_id: "aaaa1111-1111-7111-8111-111111111111",
    actual_id: "bbbb2222-2222-7222-8222-222222222222",
    ...overrides,
  };
}

function summary(overrides: Partial<CostSummary> = {}): CostSummary {
  return {
    period_month: "2026-09-01",
    lines: [line()],
    totals: [
      {
        currency: "USD",
        estimated_minor: 458,
        actual_minor: 471,
        variance_minor: 13,
      },
    ],
    ...overrides,
  };
}

function stubCosts(body: CostSummary = summary(), items: CostItem[] = [ITEM]) {
  stub("GET", "/api/v1/operating-costs/summary", { body });
  stub("GET", "/api/v1/operating-costs/items", { body: { items } });
  stub("GET", "/api/v1/operating-costs/history", {
    body: {
      from_month: "2025-10-01",
      to_month: "2026-09-01",
      months: [
        {
          period_month: "2026-08-01",
          totals: [
            {
              currency: "USD",
              estimated_minor: 400,
              actual_minor: 390,
              variance_minor: -10,
            },
          ],
        },
        { period_month: "2026-09-01", totals: body.totals },
      ],
      range_totals: [
        {
          currency: "USD",
          estimated_minor: 858,
          actual_minor: 861,
          variance_minor: 3,
        },
      ],
    },
  });
}

describe("the operating cost summary", () => {
  it("shows estimated, invoiced and the difference from the server", async () => {
    signedIn();
    stubServer({ customers: [customer()] });
    stubCosts();

    renderApp(<App />, "/operating-costs");

    const totals = await screen.findByRole("region", { name: /Totals in USD/ });
    expect(within(totals).getByText("USD 4.58")).toBeInTheDocument();
    expect(within(totals).getByText("USD 4.71")).toBeInTheDocument();
    // variance = actual − estimated, computed on the server and printed signed.
    expect(within(totals).getByText("+USD 0.13")).toBeInTheDocument();
  });

  it("shows the usage that produced the estimate, and the rate behind it", async () => {
    signedIn();
    stubServer({ customers: [customer()] });
    stubCosts();

    renderApp(<App />, "/operating-costs");

    expect(await screen.findByText(/Measured: 20.833333 audio_hour/)).toBeInTheDocument();
    expect(screen.getByText("USD 0.22 per audio_hour")).toBeInTheDocument();
  });

  it("shows a dash for a month with no invoice, never a zero", async () => {
    signedIn();
    stubServer({ customers: [customer()] });
    stubCosts(
      summary({
        lines: [
          line({
            actual_amount_minor: null,
            actual_invoice_reference: null,
            actual_id: null,
            variance_minor: null,
          }),
        ],
        totals: [
          {
            currency: "USD",
            estimated_minor: 458,
            actual_minor: 0,
            variance_minor: 0,
          },
        ],
      }),
    );

    renderApp(<App />, "/operating-costs");

    // Scoped to the breakdown: the provider is also named in the scenario
    // calculator's picker further down the page.
    const breakdown = await screen.findByRole("region", { name: "By provider" });
    const item = within(breakdown).getByText("Speech to text").closest("li")!;
    const figures = within(item).getAllByText("—");
    // Both the invoice and the difference are blank: one is unknown, and the
    // other cannot be worked out without it.
    expect(figures).toHaveLength(2);
    expect(within(item).getByText("USD 4.58")).toBeInTheDocument();
  });

  it("shows a dash for a usage-priced month with no usage entered", async () => {
    signedIn();
    stubServer({ customers: [customer()] });
    stubCosts(
      summary({
        lines: [
          line({
            usage_quantity: null,
            usage_inputs: null,
            estimated_amount_minor: null,
            actual_amount_minor: null,
            actual_id: null,
            usage_id: null,
            variance_minor: null,
          }),
        ],
        totals: [],
      }),
    );

    renderApp(<App />, "/operating-costs");

    const breakdown = await screen.findByRole("region", { name: "By provider" });
    const item = within(breakdown).getByText("Speech to text").closest("li")!;
    expect(within(item).getAllByText("—")).toHaveLength(3);
    expect(screen.getByText(/Nothing recorded for this month yet/)).toBeInTheDocument();
  });

  it("keeps totals separated by currency and says nothing is converted", async () => {
    signedIn();
    stubServer({ customers: [customer()] });
    stubCosts(
      summary({
        totals: [
          { currency: "PKR", estimated_minor: 500000, actual_minor: null, variance_minor: null },
          { currency: "USD", estimated_minor: 458, actual_minor: 471, variance_minor: 13 },
        ],
      }),
    );

    renderApp(<App />, "/operating-costs");

    expect(await screen.findByRole("region", { name: /Totals in PKR/ })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: /Totals in USD/ })).toBeInTheDocument();
    expect(screen.getByText(/Nothing is converted/)).toBeInTheDocument();
  });

  it("shows the month-by-month history", async () => {
    signedIn();
    stubServer({ customers: [customer()] });
    stubCosts();

    renderApp(<App />, "/operating-costs");

    const history = await screen.findByRole("region", { name: /Month by month/ });
    expect(within(history).getByText("September 2026")).toBeInTheDocument();
    expect(within(history).getByText("August 2026")).toBeInTheDocument();
    expect(within(history).getByText(/USD 8.58 estimated/)).toBeInTheDocument();
  });

  it("never mentions commission", async () => {
    signedIn();
    stubServer({ customers: [customer()] });
    stubCosts();

    renderApp(<App />, "/operating-costs");
    await screen.findByRole("region", { name: /Totals in USD/ });

    expect(screen.queryByText(/commission/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/outstanding/i)).not.toBeInTheDocument();
  });

  it("says it is unavailable offline rather than showing stale figures", async () => {
    signedIn();
    stubServer({ customers: [customer()] });
    stubCosts();
    Object.defineProperty(navigator, "onLine", { value: false, configurable: true });

    renderApp(<App />, "/operating-costs");

    expect(await screen.findByText(/Unavailable offline/)).toBeInTheDocument();
    expect(requestsTo("GET", "/api/v1/operating-costs/summary")).toHaveLength(0);

    Object.defineProperty(navigator, "onLine", { value: true, configurable: true });
  });
});

describe("recording usage and invoices", () => {
  it("sends a usage quantity as a decimal string", async () => {
    signedIn();
    stubServer({ customers: [customer()] });
    stubCosts(summary({ lines: [line({ usage_id: null, usage_quantity: null })] }));
    stub("POST", "/api/v1/operating-costs/usage", {
      status: 201,
      body: { status: "APPLIED", entity: {} },
    });

    renderApp(<App />, "/operating-costs");
    await userEvent.click(await screen.findByRole("button", { name: "Enter usage" }));
    await userEvent.type(
      await screen.findByLabelText(/How much was used/),
      "41.666667",
    );
    await userEvent.click(screen.getByRole("button", { name: "Save usage" }));

    await waitFor(() =>
      expect(requestsTo("POST", "/api/v1/operating-costs/usage")).toHaveLength(1),
    );
    const body = requestsTo("POST", "/api/v1/operating-costs/usage")[0]!.body as
      Record<string, unknown>;
    // A string, so six decimal places survive intact — a JS number would not.
    expect(body.usage_quantity).toBe("41.666667");
    expect(body.period_month).toBe("2026-09-01");
    expect(typeof body.operation_id).toBe("string");
  });

  it("asks for a reason before replacing a figure that already exists", async () => {
    signedIn();
    stubServer({ customers: [customer()] });
    stubCosts();
    stub("POST", "/api/v1/operating-costs/actuals", {
      status: 201,
      body: { status: "APPLIED", entity: {} },
    });

    renderApp(<App />, "/operating-costs");
    await userEvent.click(await screen.findByRole("button", { name: "Change invoice" }));

    await userEvent.type(await screen.findByLabelText(/Amount invoiced/), "4.81");
    // Not yet: the earlier amount is being replaced and that needs saying why.
    expect(screen.getByRole("button", { name: "Replace invoice" })).toBeDisabled();

    await userEvent.type(
      screen.getByLabelText(/Why is the earlier amount being replaced/),
      "provider reissued it",
    );
    await userEvent.click(screen.getByRole("button", { name: "Replace invoice" }));

    await waitFor(() =>
      expect(requestsTo("POST", "/api/v1/operating-costs/actuals")).toHaveLength(1),
    );
    const body = requestsTo("POST", "/api/v1/operating-costs/actuals")[0]!.body as
      Record<string, unknown>;
    expect(body.amount_minor).toBe(481);
    expect(body.correction_reason).toBe("provider reissued it");
  });

  it("saves a new rate as data, with no code change involved", async () => {
    signedIn();
    stubServer({ customers: [customer()] });
    stubCosts();
    stub(`POST`, `/api/v1/operating-costs/items/${ITEM_ID}/rates`, {
      status: 201,
      body: { status: "APPLIED", entity: RATE },
    });

    renderApp(<App />, "/operating-costs");
    await userEvent.click(await screen.findByRole("button", { name: "New rate" }));

    await userEvent.type(await screen.findByLabelText("Starting from"), "2026-10-01");
    await userEvent.clear(screen.getByLabelText("Charged per"));
    await userEvent.type(screen.getByLabelText("Charged per"), "audio_hour");
    await userEvent.type(screen.getByLabelText("Price per unit"), "0.30");
    await userEvent.click(screen.getByRole("button", { name: "Save rate" }));

    await waitFor(() =>
      expect(
        requestsTo("POST", `/api/v1/operating-costs/items/${ITEM_ID}/rates`),
      ).toHaveLength(1),
    );
    const body = requestsTo(
      "POST",
      `/api/v1/operating-costs/items/${ITEM_ID}/rates`,
    )[0]!.body as Record<string, unknown>;
    expect(body.unit_price_minor).toBe(30);
    expect(body.unit).toBe("audio_hour");
    expect(body.effective_from).toBe("2026-10-01");
    // No end date is sent: a rate is closed by its successor, never by hand.
    expect(body).not.toHaveProperty("effective_to");
  });
});

describe("the scenario calculator", () => {
  it("prices 100, 500 and 1000 a day on the server and writes nothing", async () => {
    signedIn();
    stubServer({ customers: [customer()] });
    stubCosts();
    stub("POST", "/api/v1/operating-costs/scenarios", {
      body: {
        period_month: "2026-09-01",
        results: [
          {
            label: "Starting",
            cost_item_id: ITEM_ID,
            code: "VOICE_STT",
            name: "Speech to text",
            period_month: "2026-09-01",
            usage_quantity: "4.166667",
            usage_unit: "audio_hour",
            derived_from: { events_per_day: 100, seconds_per_event: "5", days: 30 },
            estimated_amount_minor: 92,
            currency: "USD",
            currency_exponent: 2,
            rate: RATE,
          },
          {
            label: "Reasonable",
            cost_item_id: ITEM_ID,
            code: "VOICE_STT",
            name: "Speech to text",
            period_month: "2026-09-01",
            usage_quantity: "20.833333",
            usage_unit: "audio_hour",
            derived_from: { events_per_day: 500, seconds_per_event: "5", days: 30 },
            estimated_amount_minor: 458,
            currency: "USD",
            currency_exponent: 2,
            rate: RATE,
          },
          {
            label: "Larger",
            cost_item_id: ITEM_ID,
            code: "VOICE_STT",
            name: "Speech to text",
            period_month: "2026-09-01",
            usage_quantity: "41.666667",
            usage_unit: "audio_hour",
            derived_from: { events_per_day: 1000, seconds_per_event: "5", days: 30 },
            estimated_amount_minor: 917,
            currency: "USD",
            currency_exponent: 2,
            rate: RATE,
          },
        ],
        totals: [{ currency: "USD", estimated_minor: 1467 }],
      },
    });

    renderApp(<App />, "/operating-costs");
    await userEvent.click(await screen.findByRole("button", { name: "Work it out" }));

    const panel = await screen.findByRole("region", { name: /What if we used more/ });
    expect(await within(panel).findByText("USD 0.92")).toBeInTheDocument();
    expect(within(panel).getByText("USD 4.58")).toBeInTheDocument();
    expect(within(panel).getByText("USD 9.17")).toBeInTheDocument();
    expect(within(panel).getByText(/100 a day · 4.166667 audio_hour/)).toBeInTheDocument();

    // The three defaults, and the server did the conversion.
    const body = requestsTo("POST", "/api/v1/operating-costs/scenarios")[0]!.body as
      Record<string, unknown>;
    const scenarios = body.scenarios as Record<string, unknown>[];
    expect(scenarios.map((s) => s.events_per_day)).toEqual([100, 500, 1000]);
    expect(scenarios[0]!.seconds_per_event).toBe("5");

    // Planning only: no usage was recorded, and it is labelled as such.
    expect(requestsTo("POST", "/api/v1/operating-costs/usage")).toHaveLength(0);
    expect(within(panel).getByText(/nothing here is recorded/)).toBeInTheDocument();
  });

  it("lets the usage levels and the seconds per use be changed", async () => {
    signedIn();
    stubServer({ customers: [customer()] });
    stubCosts();
    stub("POST", "/api/v1/operating-costs/scenarios", {
      body: { period_month: "2026-09-01", results: [], totals: [] },
    });

    renderApp(<App />, "/operating-costs");

    const seconds = await screen.findByLabelText(/Seconds per use/);
    await userEvent.clear(seconds);
    await userEvent.type(seconds, "8");

    const starting = screen.getByLabelText(/Starting — uses per day/);
    await userEvent.clear(starting);
    await userEvent.type(starting, "250");

    await userEvent.click(screen.getByRole("button", { name: "Work it out" }));

    await waitFor(() =>
      expect(requestsTo("POST", "/api/v1/operating-costs/scenarios")).toHaveLength(1),
    );
    const scenarios = (
      requestsTo("POST", "/api/v1/operating-costs/scenarios")[0]!.body as
        Record<string, unknown>
    ).scenarios as Record<string, unknown>[];
    expect(scenarios[0]!.events_per_day).toBe(250);
    expect(scenarios[0]!.seconds_per_event).toBe("8");
  });
});
