import { describe, expect, it } from "vitest";
import { screen, within } from "@testing-library/react";

import { App } from "@/App";
import { CUSTOMER_PAGE_SIZE, listAllCustomers } from "@/api/customers";
import { changesResponse, customer, renderApp, SETTINGS, signedIn } from "@/test/fixtures";
import { requests, requestsTo, stub } from "@/test/http";

/**
 * The daily register must contain everyone.
 *
 * `GET /customers` pages: `limit` defaults to 100 and is capped at 500, `offset`
 * walks, and the response is `{items}` with **no total and no cursor** — so a
 * client that issues one request and stops cannot tell whether it got everybody.
 * Before the P4 review the register did exactly that, and a tenant with more than
 * 500 active customers would have had a silently short round.
 *
 * The invariant under test: N eligible active customers on the server → N
 * customers represented by the register, before subtracting those already
 * recorded for the business day.
 *
 * P5 moves the walk into the sync engine's seed, and the register then reads the
 * snapshot — so the same guarantee has to hold one layer earlier: the snapshot
 * must contain everyone, or the round is short offline as well as online.
 */

const DAY_PATH = `/api/v1/service/day/${SETTINGS.business_date}`;

/** The feed head the seed continues from; the seed itself uses the read routes. */
function stubFeed() {
  stub("GET", "/api/v1/sync/changes", { body: changesResponse() });
}

/** A synthetic page-server that honours `limit` and `offset` like the backend. */
function stubCustomerPages(total: number, options: { repeatFirstRow?: boolean } = {}) {
  const all = Array.from({ length: total }, (_, i) =>
    customer({
      id: `11111111-1111-7111-8111-${i.toString().padStart(12, "0")}`,
      code: `C-${i.toString().padStart(4, "0")}`,
      name: `Customer ${i.toString().padStart(4, "0")}`,
    }),
  );

  stub("GET", "/api/v1/customers", (request) => {
    const params = new URL(request.url, "http://localhost").searchParams;
    const limit = Number(params.get("limit") ?? 100);
    const offset = Number(params.get("offset") ?? 0);
    const items = all.slice(offset, offset + limit);
    // A server that repeats a boundary row: the client must not double-count it.
    if (options.repeatFirstRow && offset > 0 && items.length > 0) {
      items[0] = all[offset - 1]!;
    }
    return { body: { items } };
  });

  return all;
}

function offsetsRequested(): number[] {
  return requestsTo("GET", "/api/v1/customers").map((r) =>
    Number(new URL(r.url, "http://localhost").searchParams.get("offset")),
  );
}

describe("listAllCustomers follows the pagination contract to its end", () => {
  it("pages past the maximum page size until a short page arrives", async () => {
    const all = stubCustomerPages(1200);

    const loaded = await listAllCustomers({ status: "ACTIVE" });

    expect(loaded).toHaveLength(1200);
    expect(loaded.map((c) => c.id)).toEqual(all.map((c) => c.id));
    expect(offsetsRequested()).toEqual([0, 500, 1000]);
  });

  it("asks for one more page when the total is an exact multiple", async () => {
    stubCustomerPages(CUSTOMER_PAGE_SIZE);

    const loaded = await listAllCustomers();

    // 500 is a full page, so the client cannot know it was the last one until an
    // empty page comes back. Stopping on a full page would be the truncation bug.
    expect(loaded).toHaveLength(500);
    expect(offsetsRequested()).toEqual([0, 500]);
  });

  it("stops after one request when everybody fits on a page", async () => {
    stubCustomerPages(3);

    expect(await listAllCustomers()).toHaveLength(3);
    expect(offsetsRequested()).toEqual([0]);
  });

  it("requests the maximum page size and an explicit offset every time", async () => {
    stubCustomerPages(700);

    await listAllCustomers({ status: "ACTIVE" });

    for (const request of requestsTo("GET", "/api/v1/customers")) {
      const params = new URL(request.url, "http://localhost").searchParams;
      expect(params.get("limit")).toBe("500");
      expect(params.get("offset")).not.toBeNull();
      expect(params.get("status")).toBe("ACTIVE");
    }
  });

  it("counts a row repeated across a page boundary only once", async () => {
    stubCustomerPages(1200, { repeatFirstRow: true });

    const loaded = await listAllCustomers();

    expect(new Set(loaded.map((c) => c.id)).size).toBe(loaded.length);
  });

  it("propagates a failure rather than returning a short list", async () => {
    stub("GET", "/api/v1/customers", (_request, attempt) =>
      attempt === 0
        ? { body: { items: Array.from({ length: 500 }, (_, i) => customer({ id: `x${i}` })) } }
        : { networkError: true },
    );

    // A partial round presented as complete is the failure mode this whole test
    // file exists to prevent; the read must fail loudly instead.
    await expect(listAllCustomers()).rejects.toThrow();
  });
});

describe("the daily register represents every active customer", () => {
  it("shows all 1200 across three pages, not the first 500", async () => {
    stubCustomerPages(1200);
    stubFeed();
    stub("GET", "/api/v1/tenant/settings", { body: SETTINGS });
    stub("GET", DAY_PATH, {
      body: {
        service_date: SETTINGS.business_date,
        business_date: SETTINGS.business_date,
        items: [],
      },
    });
    signedIn();

    renderApp(<App />, "/today");

    expect(
      await screen.findByText("0 of 1200 recorded", {}, { timeout: 15_000 }),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Still to do (1200)" })).toBeInTheDocument();

    // Somebody from the third page is genuinely on the round, not just counted.
    const todo = screen.getByRole("heading", { name: "Still to do (1200)" }).parentElement!;
    expect(within(todo).getByText("Customer 1199")).toBeInTheDocument();
  });

  it("subtracts only those already recorded for the business day", async () => {
    const all = stubCustomerPages(600);
    stubFeed();
    stub("GET", "/api/v1/tenant/settings", { body: SETTINGS });
    stub("GET", DAY_PATH, {
      body: {
        service_date: SETTINGS.business_date,
        business_date: SETTINGS.business_date,
        items: [
          {
            id: "bbbbbbbb-bbbb-7bbb-8bbb-bbbbbbbbbbbb",
            customer_id: all[550]!.id,
            service_date: SETTINGS.business_date,
            quantity: "2",
            unit_price_minor: 25000,
            unit_label: "bottle",
            charge_minor: 50000,
            kind: "SERVICE",
            status: "ACTIVE",
            corrects_id: null,
            superseded_by_id: null,
            adjustment_minor: 0,
            reason: null,
            source: "ONLINE",
            input_method: "BUTTON",
            operation_id: "cccccccc-cccc-7ccc-8ccc-cccccccccccc",
            recorded_at: "2026-09-03T05:00:00+00:00",
            row_version: 99,
            currency: "PKR",
            currency_exponent: 2,
          },
        ],
      },
    });
    signedIn();

    renderApp(<App />, "/today");

    // The recorded customer is on the *second* page: the join could only find
    // them because paging brought them back at all.
    expect(
      await screen.findByText("1 of 600 recorded", {}, { timeout: 15_000 }),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Still to do (599)" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Done (1)" })).toBeInTheDocument();
  });
});

describe("the customer list is loaded in full too", () => {
  it("pages before filtering, so the filter is over everyone", async () => {
    stubCustomerPages(1200);
    stubFeed();
    stub("GET", "/api/v1/tenant/settings", { body: SETTINGS });
    stub("GET", DAY_PATH, {
      body: {
        service_date: SETTINGS.business_date,
        business_date: SETTINGS.business_date,
        items: [],
      },
    });
    signedIn();

    renderApp(<App />, "/customers");

    expect(
      await screen.findByText("Customer 0000", {}, { timeout: 15_000 }),
    ).toBeInTheDocument();
    expect(screen.getByText("Customer 1199")).toBeInTheDocument();
    expect(requests.filter((r) => r.path === "/api/v1/customers")).toHaveLength(3);
  });
});
