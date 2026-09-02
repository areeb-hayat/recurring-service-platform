/** Render helpers and canned server payloads, mirroring the real serializers. */

import type { ReactElement, ReactNode } from "react";
import { render, type RenderResult } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { AuthProvider } from "@/auth/AuthContext";
import { saveSession } from "@/auth/session";
import type { Customer, ServiceRecord, TenantSettings } from "@/api/types";

export function signedIn(): void {
  saveSession({
    access_token: "access-token",
    refresh_token: "refresh-token",
    role: "OWNER_ADMIN",
    scope: "TENANT",
    tenant_id: "11111111-1111-7111-8111-111111111111",
  });
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
        <AuthProvider>{children}</AuthProvider>
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
    source: "ONLINE",
    input_method: "BUTTON",
    operation_id: "cccccccc-cccc-7ccc-8ccc-cccccccccccc",
    recorded_at: "2026-09-03T05:00:00+00:00",
    row_version: 99,
    currency: "PKR",
    currency_exponent: 2,
    ...overrides,
  };
}

export function errorBody(code: string, detail = "failed", extra: object = {}) {
  return { error: { code, detail, ...extra } };
}
