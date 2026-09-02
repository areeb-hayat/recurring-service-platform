/**
 * The daily register's data.
 *
 * Three server reads, joined for display only:
 *
 *  - `GET /tenant/settings` — the tenant's business date and unit label;
 *  - `GET /customers?status=ACTIVE` — who is on the round, paged to the end;
 *  - `GET /service/day/{business_date}` — what has already been recorded.
 *
 * The join produces "done" and "pending"; it derives no money, no balance and no
 * due state. Every amount shown anywhere came from the server as an integer.
 *
 * **Which date.** The client never decides. It cannot: the tenant's timezone
 * lives on the tenant row, so a browser in another timezone — or one sitting
 * across midnight — would file work under the wrong business day. The day query
 * waits for the settings read and then asks for exactly the date the server
 * named (P0 R4).
 */

import { useQuery } from "@tanstack/react-query";

import { listAllCustomers } from "@/api/customers";
import { getDay } from "@/api/service";
import { getTenantSettings } from "@/api/tenant";
import type { Customer, DayResponse, ServiceRecord } from "@/api/types";

export const tenantSettingsQuery = {
  queryKey: ["tenant", "settings"] as const,
  queryFn: getTenantSettings,
  // Currency, unit label and entry defaults change rarely; the business date
  // changes once a day. Neither is worth refetching on every screen mount.
  staleTime: 5 * 60_000,
};

export function useTenantSettingsQuery() {
  return useQuery(tenantSettingsQuery);
}

export function useDayQuery(businessDate: string | undefined) {
  return useQuery({
    queryKey: ["day", businessDate ?? ""],
    queryFn: () => getDay(businessDate!),
    enabled: Boolean(businessDate),
  });
}

export function useCustomersQuery() {
  return useQuery({
    queryKey: ["customers", { status: "ACTIVE" }],
    // Every active customer, not the first page: a round that stops at the
    // backend's page cap would quietly leave people unserved.
    queryFn: () => listAllCustomers({ status: "ACTIVE" }),
  });
}

export interface RegisterEntry {
  customer: Customer;
  record: ServiceRecord | null;
}

export interface Register {
  businessDate: string;
  entries: RegisterEntry[];
  pending: RegisterEntry[];
  done: RegisterEntry[];
}

export function buildRegister(customers: Customer[], day: DayResponse): Register {
  const byCustomer = new Map<string, ServiceRecord>();
  for (const record of day.items) byCustomer.set(record.customer_id, record);

  const entries: RegisterEntry[] = customers.map((customer) => ({
    customer,
    record: byCustomer.get(customer.id) ?? null,
  }));

  return {
    businessDate: day.business_date,
    entries,
    pending: entries.filter((e) => e.record === null),
    done: entries.filter((e) => e.record !== null),
  };
}
