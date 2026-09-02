import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { App } from "@/App";
import { loadSession } from "@/auth/session";
import { errorBody, renderApp, SETTINGS, signedIn } from "@/test/fixtures";
import { requestsTo, stub } from "@/test/http";

const TOKENS = {
  access_token: "new-access",
  refresh_token: "new-refresh",
  token_type: "bearer",
  expires_in: 3600,
  role: "OWNER_ADMIN",
  scope: "TENANT",
  tenant_id: "11111111-1111-7111-8111-111111111111",
};

function stubEmptyRegister() {
  stub("GET", "/api/v1/tenant/settings", { body: SETTINGS });
  stub("GET", "/api/v1/customers", { body: { items: [] } });
  stub("GET", `/api/v1/service/day/${SETTINGS.business_date}`, {
    body: { service_date: SETTINGS.business_date, business_date: SETTINGS.business_date, items: [] },
  });
}

describe("signing in", () => {
  it("stores the session and lands on today's round", async () => {
    stub("POST", "/api/v1/auth/login", { body: TOKENS });
    stubEmptyRegister();

    renderApp(<App />, "/login");
    await userEvent.type(screen.getByLabelText("Email"), "owner@alpha.test");
    await userEvent.type(screen.getByLabelText("Password"), "correct horse");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("navigation", { name: "Main" })).toBeInTheDocument();
    expect(loadSession()?.access_token).toBe("new-access");
  });

  it("never sends a tenant_id — the token decides the scope", async () => {
    stub("POST", "/api/v1/auth/login", { body: TOKENS });
    stubEmptyRegister();

    renderApp(<App />, "/login");
    await userEvent.type(screen.getByLabelText("Email"), "owner@alpha.test");
    await userEvent.type(screen.getByLabelText("Password"), "correct horse");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await screen.findByRole("navigation", { name: "Main" });
    const body = requestsTo("POST", "/api/v1/auth/login")[0]?.body as Record<string, unknown>;
    expect(body).not.toHaveProperty("tenant_id");
  });

  it("shows a plain message on bad credentials and keeps no session", async () => {
    stub("POST", "/api/v1/auth/login", {
      status: 401,
      body: errorBody("UNAUTHENTICATED", "invalid email or password"),
    });

    renderApp(<App />, "/login");
    await userEvent.type(screen.getByLabelText("Email"), "owner@alpha.test");
    await userEvent.type(screen.getByLabelText("Password"), "wrong");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Email or password is not correct.");
    expect(alert).not.toHaveTextContent("invalid email or password");
    expect(loadSession()).toBeNull();
  });
});

describe("authenticated routing", () => {
  it("sends an anonymous visitor to the login screen", async () => {
    renderApp(<App />, "/today");
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(requestsTo("GET", "/api/v1/tenant/settings")).toHaveLength(0);
  });

  it("lets a signed-in owner reach the register", async () => {
    signedIn();
    stubEmptyRegister();

    renderApp(<App />, "/today");
    expect(await screen.findByRole("navigation", { name: "Main" })).toBeInTheDocument();
  });

  it("attaches the bearer token to every authenticated request", async () => {
    signedIn();
    stubEmptyRegister();

    renderApp(<App />, "/today");
    await screen.findByRole("navigation", { name: "Main" });
    const settings = requestsTo("GET", "/api/v1/tenant/settings")[0];
    expect(settings?.headers.authorization).toBe("Bearer access-token");
  });
});

describe("session expiry", () => {
  it("refreshes once on a 401 and replays the original request", async () => {
    signedIn();
    stub("GET", "/api/v1/tenant/settings", (_request, attempt) =>
      attempt === 0
        ? { status: 401, body: errorBody("UNAUTHENTICATED", "expired") }
        : { body: SETTINGS },
    );
    stub("POST", "/api/v1/auth/refresh", { body: TOKENS });
    stub("GET", "/api/v1/customers", { body: { items: [] } });
    stub("GET", `/api/v1/service/day/${SETTINGS.business_date}`, {
      body: { service_date: SETTINGS.business_date, business_date: SETTINGS.business_date, items: [] },
    });

    renderApp(<App />, "/today");
    await screen.findByRole("navigation", { name: "Main" });

    expect(requestsTo("GET", "/api/v1/tenant/settings")).toHaveLength(2);
    expect(requestsTo("GET", "/api/v1/tenant/settings")[1]?.headers.authorization).toBe(
      "Bearer new-access",
    );
  });

  it("falls back to the login screen when the refresh also fails", async () => {
    signedIn();
    stub("GET", "/api/v1/tenant/settings", {
      status: 401,
      body: errorBody("UNAUTHENTICATED", "expired"),
    });
    stub("POST", "/api/v1/auth/refresh", {
      status: 401,
      body: errorBody("UNAUTHENTICATED", "invalid refresh token"),
    });
    stub("GET", "/api/v1/customers", { body: { items: [] } });

    renderApp(<App />, "/today");

    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Your session has ended. Please sign in again.",
    );
    await waitFor(() => expect(loadSession()).toBeNull());
  });
});

describe("signing out", () => {
  it("clears the session, revokes the refresh token and returns to login", async () => {
    signedIn();
    stubEmptyRegister();
    stub("POST", "/api/v1/auth/logout", { status: 204 });

    renderApp(<App />, "/today");
    await screen.findByRole("navigation", { name: "Main" });
    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));

    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(loadSession()).toBeNull();

    // The revocation still carries the credential it needs, even though local
    // state was cleared first: the refresh token is read out of storage *before*
    // the clear and passed in explicitly. The request is deliberately anonymous —
    // it does not reach back for an access token that no longer exists — and the
    // backend route authenticates on the body's refresh token alone.
    await waitFor(() =>
      expect(requestsTo("POST", "/api/v1/auth/logout")[0]?.body).toEqual({
        refresh_token: "refresh-token",
      }),
    );
    expect(requestsTo("POST", "/api/v1/auth/logout")[0]?.headers.authorization).toBeUndefined();
  });

  it("still returns the person to login when revocation fails", async () => {
    signedIn();
    stubEmptyRegister();
    stub("POST", "/api/v1/auth/logout", { networkError: true });

    renderApp(<App />, "/today");
    await screen.findByRole("navigation", { name: "Main" });
    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));

    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(loadSession()).toBeNull();
  });
});
