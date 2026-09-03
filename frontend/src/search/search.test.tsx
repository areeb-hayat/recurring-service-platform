import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { App } from "@/App";
import type { Customer, CustomerMatch } from "@/api/types";
import { customer, renderApp, signedIn, stubServer } from "@/test/fixtures";
import { requests, requestsTo, stub } from "@/test/http";
import {
  looksLikePhone,
  normalizePhone,
  normalizeText,
  normalizeTokens,
  phoneSuffix,
} from "./normalize";
import { resolveLocalCustomer, searchLocalCustomers } from "./local";

/**
 * P8 on the client — finding a customer, and refusing to guess which one.
 *
 * Three separable things, and it matters which is which:
 *
 *  * **normalization** — the client's mirror of the server's one comparison
 *    rule, used offline and nowhere else;
 *  * **offline matching** — what the device can find in its own synchronised
 *    copy, and what it correctly cannot;
 *  * **the screens** — that online search really is the server's, that an
 *    ambiguous reference produces a question rather than a customer, and that a
 *    dropped connection degrades to the device honestly instead of reporting
 *    that nobody exists.
 */

const SEARCH_PATH = "/api/v1/search/customers";
const RESOLVE_PATH = "/api/v1/search/customers/resolve";

const AHMED_KHAN = customer({
  id: "11111111-1111-7111-8111-111111111111",
  code: "C-001",
  name: "Muhammad Ahmed Khan",
  aliases: ["Ahmed bhai", "Chacha Ahmed"],
  area: "F-10",
  phone_e164: "+923001234567",
});
const AHMED_ALI = customer({
  id: "22222222-2222-7222-8222-222222222222",
  code: "C-002",
  name: "Ahmed Ali",
  aliases: ["Ahmed bhai"],
  area: "G-11",
  phone_e164: null,
});
const AYESHA = customer({
  id: "33333333-3333-7333-8333-333333333333",
  code: "C-003",
  name: "Ayesha Siddiqui",
  aliases: [],
  area: "G-10",
  phone_e164: null,
});
const BOOK = [AHMED_KHAN, AHMED_ALI, AYESHA];

function setOnline(value: boolean): void {
  Object.defineProperty(navigator, "onLine", { value, configurable: true });
  window.dispatchEvent(new Event(value ? "online" : "offline"));
}

/** A server match, built from a customer so the two can never drift apart. */
function match(source: Customer, overrides: Partial<CustomerMatch> = {}): CustomerMatch {
  return {
    customer_id: source.id,
    code: source.code,
    name: source.name,
    area: source.area,
    phone_e164: source.phone_e164,
    whatsapp_e164: source.whatsapp_e164,
    status: source.status,
    aliases: source.aliases ?? [],
    outstanding_minor: 0,
    matched_on: "NAME",
    matched_value: source.name,
    match_strength: "STRONG",
    currency: "PKR",
    currency_exponent: 2,
    ...overrides,
  };
}

function searchBody(...items: CustomerMatch[]) {
  return { items, limit: 20, offset: 0, possibly_truncated: false };
}

// --- 1. normalization -------------------------------------------------------

describe("the client's normalization mirror", () => {
  it.each([
    ["  Ahmed   Khan  ", "ahmed khan"],
    ["AHMED-BHAI", "ahmed bhai"],
    ["Ahmed_bhai", "ahmed bhai"],
    ["Áyesha", "ayesha"],
    ["", ""],
    ["!!!", ""],
  ])("normalizes %o to %o", (raw, expected) => {
    expect(normalizeText(raw)).toBe(expected);
  });

  it("gives four spellings of one nickname the same key", () => {
    const keys = ["Ahmed bhai", "ahmed  BHAI", "Ahmed-bhai", " Ahmed_Bhai "].map(
      normalizeText,
    );
    expect(new Set(keys).size).toBe(1);
  });

  it("keeps a non-Latin script intact rather than transliterating it", () => {
    // No romanisation, deliberately: a machine-invented spelling of somebody's
    // name is a machine-invented identity. Aliases are how the two meet.
    expect(normalizeText("احمد")).toBe("احمد");
    expect(normalizeText("احمد")).not.toBe(normalizeText("ahmed"));
  });

  it("preserves token order but will not require it downstream", () => {
    expect(normalizeTokens("Ahmed bhai")).toEqual(["ahmed", "bhai"]);
  });

  it.each([
    ["+92 300 123-4567", "923001234567"],
    ["0300-1234567", "03001234567"],
  ])("reduces %o to its digits", (raw, expected) => {
    expect(normalizePhone(raw)).toBe(expected);
  });

  it("makes the national and international forms of a number meet", () => {
    expect(phoneSuffix("0300-1234567")).toBe(phoneSuffix("+923001234567"));
  });

  it("refuses a digit string too short to identify anybody", () => {
    expect(phoneSuffix("12345")).toBe("");
  });

  it.each([
    ["+92 300 1234567", true],
    ["Ahmed 3", false],
    ["3001234567", true],
    ["123", false],
  ])("decides whether %o looks like a phone number", (raw, expected) => {
    expect(looksLikePhone(raw)).toBe(expected);
  });

  it("never rewrites what is shown", () => {
    // The key is derived; the display text is untouched, always.
    const shown = AHMED_KHAN.name;
    expect(normalizeText(shown)).toBe("muhammad ahmed khan");
    expect(shown).toBe("Muhammad Ahmed Khan");
  });
});

// --- 2. offline matching ----------------------------------------------------

describe("searching the device's own copy", () => {
  const find = (query: string) => searchLocalCustomers(BOOK, query);
  const names = (query: string) => find(query).map((c) => c.name);

  it("finds a customer by an alias the round actually uses", () => {
    expect(names("Chacha Ahmed")).toEqual(["Muhammad Ahmed Khan"]);
  });

  it("does not care about case, spacing or punctuation", () => {
    for (const spelling of ["chacha ahmed", "CHACHA  AHMED", "chacha-ahmed"]) {
      expect(names(spelling)).toEqual(["Muhammad Ahmed Khan"]);
    }
  });

  it("does not care about word order", () => {
    expect(names("Ahmed Chacha")).toEqual(["Muhammad Ahmed Khan"]);
  });

  it("finds an exact customer code", () => {
    const [top] = find("c-002");
    expect(top?.name).toBe("Ahmed Ali");
    expect(top?.matched_on).toBe("CODE");
  });

  it("finds a phone number typed in the national form", () => {
    const [top] = find("0300 123 4567");
    expect(top?.name).toBe("Muhammad Ahmed Khan");
    expect(top?.matched_on).toBe("PHONE");
  });

  it("returns everyone who could be meant, not the first one", () => {
    expect(names("Ahmed").sort()).toEqual(["Ahmed Ali", "Muhammad Ahmed Khan"]);
  });

  it("ranks a stronger match above a looser one, repeatably", () => {
    const first = find("Ahmed Ali").map((c) => c.customer_id);
    const second = find("Ahmed Ali").map((c) => c.customer_id);
    expect(first).toEqual(second);
    expect(first[0]).toBe(AHMED_ALI.id);
  });

  it("marks a prefix as a possible match rather than an identification", () => {
    const [top] = find("Ayes");
    expect(top?.name).toBe("Ayesha Siddiqui");
    expect(top?.match_strength).toBe("WEAK");
  });

  it("leaves inactive customers out unless they are asked for", () => {
    const gone = [customer({ id: "z", code: "C-009", name: "Zulfiqar", status: "INACTIVE" })];
    expect(searchLocalCustomers(gone, "Zulfiqar")).toHaveLength(0);
    expect(
      searchLocalCustomers(gone, "Zulfiqar", { includeInactive: true }),
    ).toHaveLength(1);
  });

  it("shows no balance for anything it found, because it has none", () => {
    // FIN-4 / SYN-9: outstanding is the server's. Offline the field is null and
    // the row prints nothing — never a zero standing in for a real figure.
    for (const candidate of find("Ahmed")) {
      expect(candidate.outstanding_minor).toBeNull();
      expect(candidate.currency).toBeNull();
    }
  });

  it("still finds a customer whose row predates the aliases field", () => {
    const { aliases: _dropped, ...withoutAliases } = AHMED_KHAN;
    expect(searchLocalCustomers([withoutAliases as Customer], "Ahmed Khan")).toHaveLength(1);
  });

  it("finds nothing for an empty query rather than everything", () => {
    expect(find("   ")).toHaveLength(0);
  });
});

// --- 3. offline identification ---------------------------------------------

describe("identifying somebody offline", () => {
  const resolve = (reference: string) => resolveLocalCustomer(BOOK, reference);

  it("resolves one strong match to an authoritative id", () => {
    const result = resolve("Ayesha Siddiqui");
    expect(result.status).toBe("RESOLVED");
    expect(result.customer?.customer_id).toBe(AYESHA.id);
  });

  it("resolves an alias nobody else answers to", () => {
    expect(resolve("Chacha Ahmed").customer?.customer_id).toBe(AHMED_KHAN.id);
  });

  it("refuses to choose between two Ahmeds", () => {
    const result = resolve("Ahmed");
    expect(result.status).toBe("AMBIGUOUS");
    expect(result.customer).toBeNull();
    expect(result.candidates.map((c) => c.customer_id).sort()).toEqual(
      [AHMED_ALI.id, AHMED_KHAN.id].sort(),
    );
  });

  it("refuses to choose between two customers sharing a nickname", () => {
    // Both brothers are "Ahmed bhai". That is a question, not a ranking.
    expect(resolve("Ahmed bhai").status).toBe("AMBIGUOUS");
  });

  it("lets an exact name beat a partial one", () => {
    expect(resolve("Ahmed Ali").customer?.customer_id).toBe(AHMED_ALI.id);
  });

  it("never resolves on a prefix alone, however far ahead it is", () => {
    expect(resolve("Ayes").status).toBe("AMBIGUOUS");
  });

  it("says NOT_FOUND rather than offering the nearest thing", () => {
    expect(resolve("Zulfiqar").status).toBe("NOT_FOUND");
    expect(resolve("   ").status).toBe("NOT_FOUND");
  });
});

// --- 4. the customer list ---------------------------------------------------

describe("searching from the customer list", () => {
  it("asks the server, and renders what it returned", async () => {
    signedIn();
    stubServer({ customers: BOOK });
    stub("POST", SEARCH_PATH, {
      body: searchBody(
        match(AHMED_KHAN, { matched_on: "ALIAS", matched_value: "Ahmed bhai" }),
      ),
    });

    renderApp(<App />, "/customers");
    await screen.findByText("Ayesha Siddiqui");

    await userEvent.type(screen.getByLabelText("Find a customer"), "Ahmed bhai");

    await screen.findByText("Muhammad Ahmed Khan");
    const sent = await waitFor(() => {
      const calls = requestsTo("POST", SEARCH_PATH);
      expect(calls.length).toBeGreaterThan(0);
      return calls.at(-1)!;
    });
    expect(sent.body).toMatchObject({ query_text: "Ahmed bhai" });
    // The tenant is the token's, never the client's (SEC-3).
    expect(JSON.stringify(sent.body)).not.toContain("tenant_id");
    expect(screen.getByText(/Searching everyone on the books/)).toBeInTheDocument();
  });

  it("shows the alias that matched, so two Ahmeds can be told apart", async () => {
    signedIn();
    stubServer({ customers: BOOK });
    stub("POST", SEARCH_PATH, {
      body: searchBody(
        match(AHMED_KHAN, { matched_on: "ALIAS", matched_value: "Ahmed bhai" }),
        match(AHMED_ALI, { matched_on: "ALIAS", matched_value: "Ahmed bhai" }),
      ),
    });

    renderApp(<App />, "/customers");
    await screen.findByText("Ayesha Siddiqui");
    await userEvent.type(screen.getByLabelText("Find a customer"), "Ahmed bhai");

    await screen.findByText("Muhammad Ahmed Khan");
    expect(screen.getByText(/“Ahmed bhai” · C-001 · F-10/)).toBeInTheDocument();
    expect(screen.getByText(/“Ahmed bhai” · C-002 · G-11/)).toBeInTheDocument();
  });

  it("searches this device instead when the network fails, and says so", async () => {
    signedIn();
    stubServer({ customers: BOOK });
    stub("POST", SEARCH_PATH, { networkError: true });

    renderApp(<App />, "/customers");
    await screen.findByText("Ayesha Siddiqui");
    await userEvent.type(screen.getByLabelText("Find a customer"), "Chacha Ahmed");

    // An empty list would read as "no such person" — which a round would act on.
    expect(await screen.findByText("Muhammad Ahmed Khan")).toBeInTheDocument();
    expect(
      screen.getByText(/could not reach the server, so this searched the customers already on this device/i),
    ).toBeInTheDocument();
  });

  it("searches only the device when offline, and never calls out", async () => {
    signedIn();
    stubServer({ customers: BOOK });
    renderApp(<App />, "/customers");
    await screen.findByText("Ayesha Siddiqui");

    setOnline(false);
    const before = requests.length;
    await userEvent.type(screen.getByLabelText("Find a customer"), "Chacha Ahmed");

    expect(await screen.findByText("Muhammad Ahmed Khan")).toBeInTheDocument();
    expect(screen.getByText(/Offline — searching the customers already on this device/)).toBeInTheDocument();
    expect(requests.slice(before).filter((r) => r.path === SEARCH_PATH)).toHaveLength(0);
    setOnline(true);
  });
});

// --- 5. the daily register jump --------------------------------------------

describe("jumping to a customer on the round", () => {
  it("opens the card when the server identifies one customer", async () => {
    signedIn();
    stubServer({ customers: BOOK });
    stub("POST", SEARCH_PATH, { body: searchBody() });
    stub("POST", RESOLVE_PATH, {
      body: {
        status: "RESOLVED",
        query: "Chacha Ahmed",
        customer: match(AHMED_KHAN, { matched_on: "ALIAS", matched_value: "Chacha Ahmed" }),
        candidates: [match(AHMED_KHAN)],
      },
    });

    renderApp(<App />, "/today");
    // The round opens on the alphabetically first customer, not on Ahmed Khan.
    await screen.findByRole("heading", { name: "Ahmed Ali" });

    await userEvent.type(
      screen.getByLabelText("Jump to a customer"),
      "Chacha Ahmed{Enter}",
    );

    expect(
      await screen.findByRole("heading", { name: "Muhammad Ahmed Khan" }),
    ).toBeInTheDocument();
  });

  it("asks which one when the server will not choose", async () => {
    signedIn();
    stubServer({ customers: BOOK });
    stub("POST", SEARCH_PATH, { body: searchBody() });
    stub("POST", RESOLVE_PATH, {
      body: {
        status: "AMBIGUOUS",
        query: "Ahmed bhai",
        customer: null,
        candidates: [
          match(AHMED_KHAN, { matched_on: "ALIAS", matched_value: "Ahmed bhai" }),
          match(AHMED_ALI, { matched_on: "ALIAS", matched_value: "Ahmed bhai" }),
        ],
      },
    });

    renderApp(<App />, "/today");
    await screen.findByRole("heading", { name: "Ahmed Ali" });

    await userEvent.type(
      screen.getByLabelText("Jump to a customer"),
      "Ahmed bhai{Enter}",
    );

    const question = await screen.findByRole("alert");
    expect(question).toHaveTextContent(/Which one\?/);
    // Nothing was chosen for the operator: the candidates are offered, and one
    // of them only opens because a person tapped it.
    const choices = question.parentElement!;
    await userEvent.click(
      within(choices).getByRole("button", { name: /Muhammad Ahmed Khan/ }),
    );
    expect(
      await screen.findByRole("heading", { name: "Muhammad Ahmed Khan" }),
    ).toBeInTheDocument();
  });

  it("says plainly when nobody matches", async () => {
    signedIn();
    stubServer({ customers: BOOK });
    stub("POST", SEARCH_PATH, { body: searchBody() });
    stub("POST", RESOLVE_PATH, {
      body: { status: "NOT_FOUND", query: "Zulfiqar", customer: null, candidates: [] },
    });

    renderApp(<App />, "/today");
    await screen.findByRole("heading", { name: "Ahmed Ali" });

    await userEvent.type(screen.getByLabelText("Jump to a customer"), "Zulfiqar{Enter}");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /No customer matches “Zulfiqar”/,
    );
  });

  it("identifies from the device when there is no network, with the same rule", async () => {
    signedIn();
    stubServer({ customers: BOOK });
    renderApp(<App />, "/today");
    await screen.findByRole("heading", { name: "Ahmed Ali" });

    setOnline(false);
    const before = requests.length;
    await userEvent.type(
      screen.getByLabelText("Jump to a customer"),
      "Chacha Ahmed{Enter}",
    );

    expect(
      await screen.findByRole("heading", { name: "Muhammad Ahmed Khan" }),
    ).toBeInTheDocument();
    expect(requests.slice(before).filter((r) => r.path === RESOLVE_PATH)).toHaveLength(0);
    setOnline(true);
  });

  it("refuses to guess offline too", async () => {
    signedIn();
    stubServer({ customers: BOOK });
    renderApp(<App />, "/today");
    await screen.findByRole("heading", { name: "Ahmed Ali" });

    setOnline(false);
    await userEvent.type(
      screen.getByLabelText("Jump to a customer"),
      "Ahmed bhai{Enter}",
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(/Which one\?/);
    setOnline(true);
  });

  it("puts the customer's financial view one tap away", async () => {
    signedIn();
    stubServer({ customers: BOOK });
    stub("POST", SEARCH_PATH, { body: searchBody() });
    stub("POST", RESOLVE_PATH, {
      body: {
        status: "RESOLVED",
        query: "Chacha Ahmed",
        customer: match(AHMED_KHAN),
        candidates: [match(AHMED_KHAN)],
      },
    });

    renderApp(<App />, "/today");
    await screen.findByRole("heading", { name: "Ahmed Ali" });
    await userEvent.type(
      screen.getByLabelText("Jump to a customer"),
      "Chacha Ahmed{Enter}",
    );
    await screen.findByRole("heading", { name: "Muhammad Ahmed Khan" });

    // P6's view, reached from the round — not a second one built here.
    expect(
      screen.getByRole("link", { name: /Muhammad Ahmed Khan.s financials/ }),
    ).toHaveAttribute("href", `/customers/${AHMED_KHAN.id}`);
  });

  it("does not disturb the round's own states", async () => {
    signedIn();
    stubServer({ customers: BOOK });
    stub("POST", SEARCH_PATH, { body: searchBody(match(AHMED_KHAN)) });

    renderApp(<App />, "/today");
    await screen.findByRole("heading", { name: "Ahmed Ali" });
    expect(screen.getByText("Still to do (3)")).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText("Jump to a customer"), "Ahmed");
    await screen.findByText(/Searching everyone on the books/);

    // Searching is not recording: nothing moved between the three lists, and no
    // operation was queued.
    expect(screen.getByText("Still to do (3)")).toBeInTheDocument();
    expect(requestsTo("POST", "/api/v1/sync/operations")).toHaveLength(0);
  });
});

// --- 6. aliases -------------------------------------------------------------

describe("managing the names a customer is called", () => {
  const detail = {
    ...AHMED_KHAN,
    outstanding_minor: 0,
    payment_status: "PAID",
  };
  const aliasPath = `/api/v1/customers/${AHMED_KHAN.id}/aliases`;

  function stubDetail(items: Array<Record<string, unknown>>) {
    stub("GET", `/api/v1/customers/${AHMED_KHAN.id}`, { body: detail });
    stub("GET", aliasPath, { body: { items } });
    stub("GET", `/api/v1/customers/${AHMED_KHAN.id}/payments`, { body: { items: [] } });
    stub("GET", `/api/v1/customers/${AHMED_KHAN.id}/statements`, { body: { items: [] } });
    stub("GET", `/api/v1/customers/${AHMED_KHAN.id}/reminders`, { body: { items: [] } });
  }

  const ALIAS_ROW = {
    id: "44444444-4444-7444-8444-444444444444",
    customer_id: AHMED_KHAN.id,
    alias: "Ahmed bhai",
    status: "ACTIVE",
    created_at: "2026-09-01T00:00:00+00:00",
    updated_at: "2026-09-01T00:00:00+00:00",
  };

  it("lists the names the owner recorded", async () => {
    signedIn();
    stubServer({ customers: BOOK });
    stubDetail([ALIAS_ROW]);

    renderApp(<App />, `/customers/${AHMED_KHAN.id}`);

    expect(await screen.findByText("Also known as")).toBeInTheDocument();
    expect(await screen.findByText("Ahmed bhai")).toBeInTheDocument();
  });

  it("adds one under an operation id, exactly as every other write does", async () => {
    signedIn();
    stubServer({ customers: BOOK });
    stubDetail([]);
    stub("POST", aliasPath, { status: 201, body: { status: "APPLIED", entity: ALIAS_ROW } });

    renderApp(<App />, `/customers/${AHMED_KHAN.id}`);
    await screen.findByText("Also known as");

    await userEvent.type(screen.getByLabelText("Add another name"), "Ahmed bhai");
    await userEvent.click(screen.getByRole("button", { name: "Add name" }));

    const sent = await waitFor(() => {
      const calls = requestsTo("POST", aliasPath);
      expect(calls).toHaveLength(1);
      return calls[0]!;
    });
    expect(sent.body).toMatchObject({ alias: "Ahmed bhai" });
    expect((sent.body as { operation_id: string }).operation_id).toMatch(/^[0-9a-f-]{36}$/);
    // The client sends what was typed; the comparison key is the server's.
    expect(sent.body).not.toHaveProperty("normalized");
  });

  it("retires a name instead of deleting it", async () => {
    signedIn();
    stubServer({ customers: BOOK });
    stubDetail([ALIAS_ROW]);
    stub("POST", `${aliasPath}/${ALIAS_ROW.id}/deactivate`, {
      body: { status: "APPLIED", entity: { ...ALIAS_ROW, status: "INACTIVE" } },
    });

    renderApp(<App />, `/customers/${AHMED_KHAN.id}`);
    await screen.findByText("Ahmed bhai");

    await userEvent.click(screen.getByRole("button", { name: "Retire" }));

    await waitFor(() =>
      expect(requestsTo("POST", `${aliasPath}/${ALIAS_ROW.id}/deactivate`)).toHaveLength(1),
    );
    // AUD-1 in spirit: there is no delete verb anywhere in this flow.
    expect(requests.every((r) => r.method !== "DELETE")).toBe(true);
  });
});
