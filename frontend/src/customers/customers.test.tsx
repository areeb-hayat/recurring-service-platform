import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { App } from "@/App";
import {
  customer,
  errorBody,
  renderApp,
  signedIn,
  stubServer,
} from "@/test/fixtures";
import { requestsTo, stub } from "@/test/http";

const AYESHA = customer();
const DETAIL = { ...AYESHA, outstanding_minor: 125000, payment_status: "PARTIALLY_PAID" };

describe("the customer list", () => {
  it("lists customers with their code and area", async () => {
    signedIn();
    stubServer({ customers: [AYESHA] });

    renderApp(<App />, "/customers");

    expect(await screen.findByText("Ayesha Khan")).toBeInTheDocument();
    expect(screen.getByText("C-001 · G-10")).toBeInTheDocument();
  });

  it("filters the loaded rows without asking the server again", async () => {
    signedIn();
    stubServer({
      customers: [AYESHA, customer({ id: "x", code: "C-002", name: "Bilal Ahmed" })],
    });

    renderApp(<App />, "/customers");
    await screen.findByText("Ayesha Khan");

    await userEvent.type(screen.getByLabelText("Search this list"), "bilal");

    expect(screen.getByText("Bilal Ahmed")).toBeInTheDocument();
    expect(screen.queryByText("Ayesha Khan")).not.toBeInTheDocument();
    expect(requestsTo("GET", "/api/v1/customers")).toHaveLength(1);
  });

  it("says so when there is nobody yet", async () => {
    signedIn();
    stubServer();

    renderApp(<App />, "/customers");

    expect(await screen.findByText("No customers yet. Add the first one.")).toBeInTheDocument();
  });
});

describe("creating a customer", () => {
  it("starts from the tenant's configured defaults", async () => {
    signedIn();
    stubServer();

    renderApp(<App />, "/customers/new");

    expect(await screen.findByLabelText("Usual quantity (bottle)")).toHaveValue("1.000");
    expect(screen.getByLabelText("Price per bottle (PKR)")).toHaveValue("250.00");
  });

  it("sends minor units, a string quantity, an operation_id and no tenant", async () => {
    signedIn();
    stubServer();
    stub("POST", "/api/v1/customers", {
      status: 201,
      body: { status: "APPLIED", entity: AYESHA },
    });
    stub("GET", `/api/v1/customers/${AYESHA.id}`, { body: DETAIL });

    renderApp(<App />, "/customers/new");
    await screen.findByLabelText("Name");

    await userEvent.type(screen.getByLabelText("Name"), "Ayesha Khan");
    await userEvent.type(screen.getByLabelText("Customer code"), "C-001");
    await userEvent.clear(screen.getByLabelText("Price per bottle (PKR)"));
    await userEvent.type(screen.getByLabelText("Price per bottle (PKR)"), "250.50");
    await userEvent.click(screen.getByRole("button", { name: "Save customer" }));

    const sent = requestsTo("POST", "/api/v1/customers")[0]?.body as Record<string, unknown>;
    expect(sent.name).toBe("Ayesha Khan");
    expect(sent.code).toBe("C-001");
    expect(sent.unit_price_minor).toBe(25050);
    expect(sent.default_quantity).toBe("1.000");
    expect(typeof sent.operation_id).toBe("string");
    expect(sent).not.toHaveProperty("tenant_id");
  });

  it("names a duplicate code instead of showing the database error", async () => {
    signedIn();
    stubServer();
    stub("POST", "/api/v1/customers", {
      status: 409,
      body: errorBody("CUSTOMER_CODE_TAKEN", "a customer with code 'C-001' already exists"),
    });

    renderApp(<App />, "/customers/new");
    await screen.findByLabelText("Name");

    await userEvent.type(screen.getByLabelText("Name"), "Ayesha Khan");
    await userEvent.type(screen.getByLabelText("Customer code"), "C-001");
    await userEvent.click(screen.getByRole("button", { name: "Save customer" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That customer code is already in use. Choose another.",
    );
    expect(screen.getByText("That code is already in use")).toBeInTheDocument();
  });

  it("will not submit an unusable quantity", async () => {
    signedIn();
    stubServer();

    renderApp(<App />, "/customers/new");
    await screen.findByLabelText("Name");

    await userEvent.type(screen.getByLabelText("Name"), "Ayesha Khan");
    await userEvent.type(screen.getByLabelText("Customer code"), "C-001");
    const quantity = screen.getByLabelText("Usual quantity (bottle)");
    await userEvent.clear(quantity);
    await userEvent.type(quantity, "1.2345");
    await userEvent.click(screen.getByRole("button", { name: "Save customer" }));

    expect(requestsTo("POST", "/api/v1/customers")).toHaveLength(0);
    expect(
      screen.getByText("Use up to 3 decimal places, for example 2 or 1.5"),
    ).toBeInTheDocument();
  });
});

describe("viewing and editing a customer", () => {
  it("shows the server's outstanding balance and payment status", async () => {
    signedIn();
    stub("GET", `/api/v1/customers/${AYESHA.id}`, { body: DETAIL });

    renderApp(<App />, `/customers/${AYESHA.id}`);

    expect(await screen.findByText("PKR 1,250.00")).toBeInTheDocument();
    expect(screen.getByText("Partly paid")).toBeInTheDocument();
  });

  it("offers no way to delete anyone", async () => {
    signedIn();
    stub("GET", `/api/v1/customers/${AYESHA.id}`, { body: DETAIL });

    renderApp(<App />, `/customers/${AYESHA.id}`);
    await screen.findByRole("heading", { name: "Ayesha Khan" });

    expect(screen.queryByRole("button", { name: /delete|remove/i })).not.toBeInTheDocument();
  });

  it("patches only what changed and carries the row version", async () => {
    signedIn();
    stub("GET", `/api/v1/customers/${AYESHA.id}`, { body: DETAIL });
    stub("PATCH", `/api/v1/customers/${AYESHA.id}`, {
      body: { status: "APPLIED", entity: { ...AYESHA, name: "Ayesha K." } },
    });

    renderApp(<App />, `/customers/${AYESHA.id}`);
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));

    const name = screen.getByLabelText("Name");
    await userEvent.clear(name);
    await userEvent.type(name, "Ayesha K.");
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));

    const sent = requestsTo("PATCH", `/api/v1/customers/${AYESHA.id}`)[0]?.body as Record<
      string,
      unknown
    >;
    expect(sent.name).toBe("Ayesha K.");
    expect(sent.expected_row_version).toBe(41);
    expect(sent).not.toHaveProperty("code");
    expect(typeof sent.operation_id).toBe("string");
  });

  it("cannot change a customer code once it exists", async () => {
    signedIn();
    stub("GET", `/api/v1/customers/${AYESHA.id}`, { body: DETAIL });

    renderApp(<App />, `/customers/${AYESHA.id}`);
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));

    expect(screen.getByLabelText("Customer code")).toBeDisabled();
  });

  it("explains a concurrent edit rather than overwriting it", async () => {
    signedIn();
    stub("GET", `/api/v1/customers/${AYESHA.id}`, { body: DETAIL });
    stub("PATCH", `/api/v1/customers/${AYESHA.id}`, {
      status: 409,
      body: errorBody("ROW_VERSION_CONFLICT", "customer has been modified by someone else", {
        current_row_version: 42,
      }),
    });

    renderApp(<App />, `/customers/${AYESHA.id}`);
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Someone else updated this customer while you were editing. Reload and try again.",
    );
    // Still on the form: the change was not silently applied or thrown away.
    expect(screen.getByRole("button", { name: "Save changes" })).toBeInTheDocument();
  });

  it("keeps one operation_id when an update is retried after a dropped request", async () => {
    signedIn();
    stub("GET", `/api/v1/customers/${AYESHA.id}`, { body: DETAIL });
    stub("PATCH", `/api/v1/customers/${AYESHA.id}`, (_request, attempt) =>
      attempt === 0
        ? { networkError: true }
        : { body: { status: "DUPLICATE", entity: AYESHA } },
    );

    renderApp(<App />, `/customers/${AYESHA.id}`);
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await userEvent.click(await screen.findByRole("button", { name: "Retry" }));

    const sent = requestsTo("PATCH", `/api/v1/customers/${AYESHA.id}`);
    expect(sent).toHaveLength(2);
    expect(sent[0]?.body).toEqual(sent[1]?.body);
  });
});
