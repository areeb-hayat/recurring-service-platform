import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { App } from "@/App";
import type {
  Reminder,
  ReminderOverview,
  ReminderRow,
  ReminderStatus,
} from "@/api/types";
import { customer, renderApp, signedIn, stubServer } from "@/test/fixtures";
import { requestsTo, stub } from "@/test/http";

/**
 * Reminders (P7).
 *
 * Five things worth proving about this screen, and they are all the same thing
 * from different angles: **the server decides and the client shows**.
 *
 * 1. The amount rendered is the server's live outstanding, not the amount the
 *    reminder was generated with — those differ after a payment, and showing the
 *    stale one would contradict what the next reminder would actually say.
 * 2. The schedule days come from the response. Nothing here knows 1/4/8/12/15.
 * 3. There is no "send reminders" control. The only write is a re-attempt of one
 *    failed delivery, and it carries a generated-once `operation_id`.
 * 4. Status and filtering read a value the server derived; the client never
 *    works out who is due.
 * 5. Offline it says so, rather than showing a stage it cannot vouch for.
 */

const CUSTOMER_ID = customer().id;

function reminder(overrides: Partial<Reminder> = {}): Reminder {
  return {
    id: "11111111-1111-7111-8111-111111111111",
    customer_id: CUSTOMER_ID,
    cycle_id: "22222222-2222-7222-8222-222222222222",
    schedule_day: 4,
    kind: "REMINDER",
    state: "SENT",
    amount_minor_at_generation: 100000,
    attempt_count: 1,
    last_error: null,
    generated_at: "2026-09-04T05:00:00+00:00",
    sent_at: "2026-09-04T05:00:00+00:00",
    cancelled_at: null,
    ...overrides,
  };
}

function row(overrides: Partial<ReminderRow> = {}): ReminderRow {
  const history = overrides.history ?? [reminder()];
  return {
    customer_id: CUSTOMER_ID,
    code: "C-001",
    name: "Ayesha Khan",
    area: "G-10",
    customer_status: "ACTIVE",
    outstanding_minor: 70000,
    status: "WAITING" as ReminderStatus,
    has_contact: true,
    cycle: {
      cycle_id: "22222222-2222-7222-8222-222222222222",
      statement_id: "33333333-3333-7333-8333-333333333333",
      period_start: "2026-08-01",
      period_end: "2026-08-31",
      statement_closing_balance_minor: 100000,
    },
    latest: history[history.length - 1] ?? null,
    next_stage: { day: 8, kind: "REMINDER" },
    owner_alert: null,
    ...overrides,
    history,
  };
}

function overview(overrides: Partial<ReminderOverview> = {}): ReminderOverview {
  const items = overrides.items ?? [row()];
  return {
    business_date: "2026-09-05",
    currency: "PKR",
    currency_exponent: 2,
    schedule: [
      { day: 1, kind: "STATEMENT" },
      { day: 4, kind: "REMINDER" },
      { day: 8, kind: "REMINDER" },
      { day: 12, kind: "REMINDER" },
      { day: 15, kind: "FINAL" },
    ],
    due_stage: { day: 4, kind: "REMINDER" },
    counts: { total: items.length, due: 0, attention: 0, settled: 0 },
    ...overrides,
    items,
  };
}

function stubReminders(body: ReminderOverview): void {
  stubServer({ customers: [customer()] });
  stub("GET", "/api/v1/reminders", { body });
}

async function openReminders(): Promise<void> {
  signedIn();
  renderApp(<App />, "/reminders");
  await screen.findByRole("heading", { name: "Reminders" });
}

describe("the reminders screen", () => {
  it("shows the live outstanding, not the amount the reminder was generated with", async () => {
    // Generated at 1000.00; the customer has since paid 300.00.
    stubReminders(
      overview({
        items: [
          row({ status: "DUE", outstanding_minor: 70000, history: [reminder()] }),
        ],
        counts: { total: 1, due: 1, attention: 0, settled: 0 },
      }),
    );
    await openReminders();

    expect(await screen.findByText("PKR 700.00")).toBeInTheDocument();
    expect(screen.queryByText("PKR 1,000.00")).not.toBeInTheDocument();
  });

  it("renders the schedule the server sent rather than a hard-coded one", async () => {
    stubReminders(
      overview({
        schedule: [
          { day: 2, kind: "STATEMENT" },
          { day: 20, kind: "FINAL" },
        ],
        due_stage: { day: 2, kind: "STATEMENT" },
      }),
    );
    await openReminders();

    const list = await screen.findByRole("list", { name: "Reminder schedule" });
    const days = within(list)
      .getAllByRole("listitem")
      .map((li) => li.textContent);
    expect(days).toEqual(["Day 2 · statement", "Day 20 · final notice"]);
    expect(screen.queryByText(/Day 12/)).not.toBeInTheDocument();
  });

  it("has no control that sends reminders", async () => {
    stubReminders(overview());
    await openReminders();

    for (const label of [/send reminders/i, /send all/i, /run reminders/i]) {
      expect(screen.queryByRole("button", { name: label })).not.toBeInTheDocument();
    }
  });

  it("shows an empty state when no customer has been billed yet", async () => {
    stubReminders(overview({ items: [], counts: { total: 0, due: 0, attention: 0, settled: 0 } }));
    await openReminders();

    expect(
      await screen.findByText(/Reminders begin once a billing period has been closed/i),
    ).toBeInTheDocument();
  });

  it("filters on the status the server derived", async () => {
    const due = row({
      customer_id: "aaaa0001-0000-7000-8000-000000000001",
      code: "C-002",
      name: "Bilal Ahmed",
      status: "DUE",
      history: [],
      latest: null,
      outstanding_minor: 50000,
    });
    const settled = row({
      customer_id: "aaaa0002-0000-7000-8000-000000000002",
      code: "C-003",
      name: "Sana Malik",
      status: "SETTLED",
      outstanding_minor: 0,
      history: [],
      latest: null,
      next_stage: null,
    });
    stubReminders(
      overview({
        items: [due, settled],
        counts: { total: 2, due: 1, attention: 0, settled: 1 },
      }),
    );
    await openReminders();

    // "Needs action" is the default, and it is the server's status that decides.
    expect(await screen.findByText("Bilal Ahmed")).toBeInTheDocument();
    expect(screen.queryByText("Sana Malik")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Everyone" }));
    expect(await screen.findByText("Sana Malik")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Settled" }));
    expect(await screen.findByText("Sana Malik")).toBeInTheDocument();
    expect(screen.queryByText("Bilal Ahmed")).not.toBeInTheDocument();
  });

  it("says a customer who has paid is settled, and offers no next step", async () => {
    stubReminders(
      overview({
        items: [
          row({
            status: "SETTLED",
            outstanding_minor: 0,
            next_stage: null,
            history: [reminder({ state: "CANCELLED", cancelled_at: "2026-09-05T05:00:00+00:00" })],
          }),
        ],
        counts: { total: 1, due: 0, attention: 0, settled: 1 },
      }),
    );
    await openReminders();
    await userEvent.click(screen.getByRole("button", { name: "Everyone" }));

    // "Paid up" is also a heading on the counts row, so match the row's own badge.
    await userEvent.click(await screen.findByRole("button", { name: /Ayesha Khan/ }));
    expect(screen.getByText("Paid up", { selector: ".badge" })).toBeInTheDocument();
    expect(screen.getByText("stopped")).toBeInTheDocument();
    expect(screen.queryByText(/^Next:/)).not.toBeInTheDocument();
  });

  it("shows why nothing is sent to a customer with no statement", async () => {
    stubReminders(
      overview({
        items: [
          row({ status: "NO_STATEMENT", cycle: null, history: [], latest: null, next_stage: null }),
        ],
      }),
    );
    await openReminders();
    await userEvent.click(screen.getByRole("button", { name: "Everyone" }));

    expect(await screen.findByText(/no statement issued yet/i)).toBeInTheDocument();
  });

  it("surfaces a customer with no phone number rather than hiding them", async () => {
    stubReminders(
      overview({
        items: [row({ status: "ATTENTION", has_contact: false, outstanding_minor: 70000 })],
        counts: { total: 1, due: 0, attention: 1, settled: 0 },
      }),
    );
    await openReminders();

    expect(await screen.findByText(/no phone number on file/i)).toBeInTheDocument();
  });
});

describe("re-attempting a failed delivery", () => {
  const failed = reminder({
    state: "FAILED",
    sent_at: null,
    attempt_count: 1,
    last_error: "provider unreachable",
  });

  function stubFailed(): void {
    stubReminders(
      overview({
        items: [row({ status: "ATTENTION", history: [failed] })],
        counts: { total: 1, due: 0, attention: 1, settled: 0 },
      }),
    );
  }

  it("sends the reminder id with a generated operation_id and nothing else", async () => {
    stubFailed();
    stub("POST", `/api/v1/reminders/${failed.id}/send`, {
      body: { status: "APPLIED", entity: { ...failed, state: "SENT" } },
    });
    stub("GET", `/api/v1/reminders/${failed.id}`, {
      body: {
        ...failed,
        state: "SENT",
        is_outstanding_reminder: true,
        outstanding_minor: 70000,
        currency: "PKR",
        currency_exponent: 2,
        attempts: [],
      },
    });
    await openReminders();

    await userEvent.click(screen.getByRole("button", { name: /Ayesha Khan/ }));
    expect(await screen.findByText(/provider unreachable/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Try sending again/i }));

    await waitFor(() =>
      expect(requestsTo("POST", `/api/v1/reminders/${failed.id}/send`)).toHaveLength(1),
    );
    const sent = requestsTo("POST", `/api/v1/reminders/${failed.id}/send`)[0]!;
    const body = sent.body as Record<string, unknown>;
    // The client names an existing stage and nothing else: no amount, no
    // recipient, no stage number, and never a tenant_id.
    expect(Object.keys(body)).toEqual(["operation_id"]);
    expect(body.operation_id).toMatch(/^[0-9a-f-]{36}$/);
    expect(JSON.stringify(sent)).not.toContain("tenant_id");

    expect(await screen.findByText("Sent.")).toBeInTheDocument();
  });

  it("reports honestly when the server cancelled instead of sending", async () => {
    stubFailed();
    const cancelled = {
      ...failed,
      state: "CANCELLED",
      cancelled_at: "2026-09-05T06:00:00+00:00",
    };
    stub("POST", `/api/v1/reminders/${failed.id}/send`, {
      body: { status: "APPLIED", entity: cancelled },
    });
    stub("GET", `/api/v1/reminders/${failed.id}`, {
      body: {
        ...cancelled,
        is_outstanding_reminder: true,
        outstanding_minor: 0,
        currency: "PKR",
        currency_exponent: 2,
        attempts: [],
      },
    });
    await openReminders();

    await userEvent.click(screen.getByRole("button", { name: /Ayesha Khan/ }));
    await userEvent.click(screen.getByRole("button", { name: /Try sending again/i }));

    expect(
      await screen.findByText(/no longer owes anything/i),
    ).toBeInTheDocument();
  });

  it("does not claim success when the retry also fails", async () => {
    stubFailed();
    stub("POST", `/api/v1/reminders/${failed.id}/send`, {
      status: 500,
      body: {},
    });
    await openReminders();

    await userEvent.click(screen.getByRole("button", { name: /Ayesha Khan/ }));
    await userEvent.click(screen.getByRole("button", { name: /Try sending again/i }));

    // Two alerts now: the original failure, and the refusal of this attempt.
    await waitFor(() => expect(screen.getAllByRole("alert")).toHaveLength(2));
    expect(screen.queryByText("Sent.")).not.toBeInTheDocument();
  });

  it("offers no retry for a reminder that was delivered", async () => {
    stubReminders(overview());
    await openReminders();
    await userEvent.click(screen.getByRole("button", { name: "Everyone" }));

    await userEvent.click(screen.getByRole("button", { name: /Ayesha Khan/ }));
    expect(
      screen.queryByRole("button", { name: /Try sending again/i }),
    ).not.toBeInTheDocument();
  });
});

describe("offline and authentication", () => {
  it("says reminders are unavailable offline rather than showing a stale one", async () => {
    const wasOnline = navigator.onLine;
    Object.defineProperty(navigator, "onLine", { value: false, configurable: true });
    try {
      stubReminders(overview());
      await openReminders();
      expect(
        await screen.findByText(/Unavailable offline/i),
      ).toBeInTheDocument();
      expect(requestsTo("GET", "/api/v1/reminders")).toHaveLength(0);
    } finally {
      Object.defineProperty(navigator, "onLine", {
        value: wasOnline,
        configurable: true,
      });
    }
  });

  it("never sends a tenant_id when reading the list", async () => {
    stubReminders(overview());
    await openReminders();

    await waitFor(() => expect(requestsTo("GET", "/api/v1/reminders")).toHaveLength(1));
    const read = requestsTo("GET", "/api/v1/reminders")[0]!;
    expect(read.url).not.toContain("tenant");
    expect(read.headers.authorization).toBe("Bearer access-token");
  });

  it("shows the server's message when the read is refused", async () => {
    stubServer({ customers: [customer()] });
    stub("GET", "/api/v1/reminders", {
      status: 403,
      body: { error: { code: "PERMISSION_DENIED", detail: "no" } },
    });
    await openReminders();

    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });
});
