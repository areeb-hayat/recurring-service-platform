/** Render helpers and canned server payloads, mirroring the real serializers. */

import type { ReactElement, ReactNode } from "react";
import { render, type RenderResult } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { AuthProvider } from "@/auth/AuthContext";
import { saveSession } from "@/auth/session";
import { SyncProvider } from "@/sync/SyncProvider";
import { engineFor } from "@/sync/engine";
import type {
  Customer,
  DashboardSummary,
  DayResponse,
  Payment,
  ServiceRecord,
  Statement,
  TenantSettings,
} from "@/api/types";
import { stub, type RecordedRequest, type StubbedResponse } from "./http";

export const TENANT_ID = "11111111-1111-7111-8111-111111111111";

export function signedIn(): void {
  saveSession({
    access_token: "access-token",
    refresh_token: "refresh-token",
    role: "OWNER_ADMIN",
    scope: "TENANT",
    tenant_id: TENANT_ID,
  });
}

/** The engine for the signed-in tenant, for tests that inspect the stores. */
export function testEngine() {
  return engineFor(TENANT_ID);
}

function testQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
    },
  });
}

export function renderApp(ui: ReactElement, initialPath = "/"): RenderResult {
  const client = testQueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <MemoryRouter initialEntries={[initialPath]}>
      <QueryClientProvider client={client}>
        <AuthProvider>
          <SyncProvider>{children}</SyncProvider>
        </AuthProvider>
      </QueryClientProvider>
    </MemoryRouter>
  );
  return render(ui, { wrapper });
}

export const SETTINGS: TenantSettings = {
  name: "Alpha Business",
  currency: "PKR",
  currency_exponent: 2,
  unit_label: "bottle",
  timezone: "Asia/Karachi",
  business_date: "2026-09-03",
  default_quantity: "1.000",
  default_unit_price_minor: 25000,
};

export function customer(overrides: Partial<Customer> = {}): Customer {
  return {
    id: "aaaaaaaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa",
    code: "C-001",
    name: "Ayesha Khan",
    phone_e164: "+923001234567",
    whatsapp_e164: null,
    address: null,
    area: "G-10",
    default_quantity: "2.000",
    unit_price_minor: 25000,
    status: "ACTIVE",
    row_version: 41,
    unit_label: "bottle",
    currency: "PKR",
    currency_exponent: 2,
    ...overrides,
  };
}

export function serviceRecord(overrides: Partial<ServiceRecord> = {}): ServiceRecord {
  return {
    id: "bbbbbbbb-bbbb-7bbb-8bbb-bbbbbbbbbbbb",
    customer_id: "aaaaaaaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa",
    service_date: "2026-09-03",
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
    source: "SYNC",
    input_method: "BUTTON",
    operation_id: "cccccccc-cccc-7ccc-8ccc-cccccccccccc",
    recorded_at: "2026-09-03T05:00:00+00:00",
    row_version: 99,
    currency: "PKR",
    currency_exponent: 2,
    ...overrides,
  };
}

export function payment(overrides: Partial<Payment> = {}): Payment {
  return {
    id: "dddddddd-dddd-7ddd-8ddd-dddddddddddd",
    customer_id: "aaaaaaaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa",
    amount_minor: 30000,
    method: "CASH",
    received_on: "2026-09-02",
    reference: null,
    note: null,
    status: "RECORDED",
    voided_reason: null,
    voided_at: null,
    operation_id: "eeeeeeee-eeee-7eee-8eee-eeeeeeeeeeee",
    source: "ONLINE",
    recorded_at: "2026-09-02T06:00:00+00:00",
    row_version: 120,
    currency: "PKR",
    currency_exponent: 2,
    ...overrides,
  };
}

export function statement(overrides: Partial<Statement> = {}): Statement {
  return {
    id: "ffffffff-ffff-7fff-8fff-ffffffffffff",
    customer_id: "aaaaaaaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa",
    cycle_id: "99999999-9999-7999-8999-999999999999",
    issued_at: "2026-09-01T00:00:00+00:00",
    opening_balance_minor: 0,
    charges_minor: 100000,
    service_adjustments_minor: 0,
    payments_minor: 30000,
    payment_reversals_minor: 0,
    closing_balance_minor: 70000,
    service_days: 4,
    total_quantity: "8.000",
    unit_label: "bottle",
    currency: "PKR",
    currency_exponent: 2,
    row_version: 130,
    ...overrides,
  };
}

export function dashboardSummary(
  overrides: Partial<DashboardSummary> = {},
): DashboardSummary {
  return {
    business_date: "2026-09-03",
    currency: "PKR",
    currency_exponent: 2,
    unit_label: "bottle",
    open_cycle: {
      id: "99999999-9999-7999-8999-999999999999",
      period_start: "2026-09-01",
      period_end: "2026-09-30",
      status: "OPEN",
      closed_at: null,
    },
    outstanding_minor: 70000,
    all_time: {
      business_generated_minor: 100000,
      billed_value_minor: 0,
      collected_minor: 30000,
      outstanding_minor: 70000,
    },
    current_cycle: {
      business_generated_minor: 100000,
      billed_value_minor: 0,
      collected_minor: 30000,
      outstanding_minor: 70000,
    },
    customers: { total: 1, active: 1, with_balance_due: 1, in_credit: 0 },
    recent_payments: [
      {
        id: "dddddddd-dddd-7ddd-8ddd-dddddddddddd",
        customer_id: "aaaaaaaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa",
        customer_name: "Ayesha Khan",
        customer_code: "C-001",
        amount_minor: 30000,
        method: "CASH",
        received_on: "2026-09-02",
        status: "RECORDED",
        reference: null,
        recorded_at: "2026-09-02T06:00:00+00:00",
      },
    ],
    ...overrides,
  };
}

export function errorBody(code: string, detail = "failed", extra: object = {}) {
  return { error: { code, detail, ...extra } };
}

export function changesResponse(overrides: Record<string, unknown> = {}) {
  return {
    since: 0,
    cursor: 0,
    has_more: false,
    head: 0,
    feed_version: 2,
    entities: [
      "tenant",
      "customer",
      "daily_service_record",
      "payment",
      "statement",
    ],
    changes: [],
    ...overrides,
  };
}

export interface ServerFixture {
  settings?: TenantSettings;
  customers?: Customer[];
  day?: DayResponse;
  /** P6 seed reads: the tenant's payments and issued statements. */
  payments?: Payment[];
  statements?: Statement[];
  /** Answer for POST /sync/operations; omit to leave it unstubbed. */
  push?: StubbedResponse | ((request: RecordedRequest, callIndex: number) => StubbedResponse);
  changes?: StubbedResponse;
}

/**
 * Stub the endpoints a signed-in device talks to.
 *
 * These are exactly the calls the sync engine makes: the tenant's settings, the
 * feed head, the seed reads — customers, today's round, and from P6 the payment
 * and statement history the new screens render — and the push. Nothing else
 * leaves the app.
 */
export function stubServer(fixture: ServerFixture = {}): void {
  const settings = fixture.settings ?? SETTINGS;
  const customers = fixture.customers ?? [];
  const day = fixture.day ?? {
    service_date: settings.business_date,
    business_date: settings.business_date,
    items: [],
  };

  stub("GET", "/api/v1/tenant/settings", { body: settings });
  stub("GET", "/api/v1/sync/changes", fixture.changes ?? { body: changesResponse() });
  stub("GET", "/api/v1/customers", { body: { items: customers } });
  stub("GET", `/api/v1/service/day/${settings.business_date}`, { body: day });
  stub("GET", "/api/v1/payments", { body: { items: fixture.payments ?? [] } });
  stub("GET", "/api/v1/statements", { body: { items: fixture.statements ?? [] } });
  if (fixture.push) stub("POST", "/api/v1/sync/operations", fixture.push);
}

/** A `results` body for POST /sync/operations. */
export function pushResults(
  ...results: Array<Record<string, unknown>>
): StubbedResponse {
  return { body: { results } };
}
