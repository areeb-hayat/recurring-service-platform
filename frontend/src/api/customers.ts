/** GET/POST /api/v1/customers, GET/PATCH /api/v1/customers/{id}. */

import { request } from "./client";
import type { OperationEnvelope } from "./operation";
import type {
  Customer,
  CustomerDetail,
  CustomerListResponse,
  CustomerStatus,
  OperationResult,
} from "./types";

export interface CustomerListQuery {
  area?: string;
  status?: CustomerStatus;
}

/**
 * The backend's maximum page size.
 *
 * `GET /customers` takes `limit` (default 100, capped at 500 both by the route's
 * `Query(le=500)` and again by `min(limit, 500)` in `list_customers`) and
 * `offset`. It returns `{items}` — no total, no cursor, no "has more" flag — so
 * the only way to know a page was the last one is that it came back short.
 */
export const CUSTOMER_PAGE_SIZE = 500;

/** One page. Prefer {@link listAllCustomers} anywhere completeness matters. */
export function listCustomers(
  query: CustomerListQuery & { limit?: number; offset?: number } = {},
): Promise<CustomerListResponse> {
  const params = new URLSearchParams();
  if (query.area) params.set("area", query.area);
  if (query.status) params.set("status", query.status);
  params.set("limit", String(query.limit ?? CUSTOMER_PAGE_SIZE));
  params.set("offset", String(query.offset ?? 0));
  return request<CustomerListResponse>(`/customers?${params.toString()}`);
}

/**
 * Every matching customer, following the pagination contract to its end.
 *
 * A single request would silently truncate at the 500th customer, and because
 * the response carries no total the truncation would be invisible: the daily
 * round would simply not contain some of the people on it. So this pages by
 * `offset` until a page comes back shorter than the page size, which is the only
 * end-of-list signal the endpoint gives.
 *
 * Rows are de-duplicated by id. Offset pagination is only sound over a *total*
 * order, and `list_customers` orders by `(name, id)` — the `id` tiebreaker was
 * added in the P4 review for exactly this reason, because `name` alone is not
 * unique and ties straddling a page boundary could otherwise be dropped or
 * repeated. The de-duplication is the belt to that braces: it costs nothing and
 * means a repeat can never become a double entry on the round.
 *
 * Termination is guaranteed: `offset` strictly increases, so against a finite
 * table a short page always arrives.
 */
export async function listAllCustomers(query: CustomerListQuery = {}): Promise<Customer[]> {
  const all: Customer[] = [];
  const seen = new Set<string>();

  for (let offset = 0; ; offset += CUSTOMER_PAGE_SIZE) {
    const page = await listCustomers({ ...query, limit: CUSTOMER_PAGE_SIZE, offset });
    for (const row of page.items) {
      if (seen.has(row.id)) continue;
      seen.add(row.id);
      all.push(row);
    }
    if (page.items.length < CUSTOMER_PAGE_SIZE) return all;
  }
}

export function getCustomer(id: string): Promise<CustomerDetail> {
  return request<CustomerDetail>(`/customers/${id}`);
}

/** The fields `CreateCustomerRequest` accepts, minus `operation_id`. */
export interface CustomerDraft {
  code: string;
  name: string;
  phone_e164: string | null;
  whatsapp_e164: string | null;
  address: string | null;
  area: string | null;
  default_quantity: string;
  unit_price_minor: number;
}

export function createCustomer(
  envelope: OperationEnvelope<CustomerDraft>,
): Promise<OperationResult<Customer>> {
  return request<OperationResult<Customer>>("/customers", {
    method: "POST",
    body: { operation_id: envelope.operation_id, ...envelope.payload },
  });
}

/** `UpdateCustomerRequest` is a partial: only the keys present are changed. */
export interface CustomerPatch extends Partial<Omit<CustomerDraft, "code">> {
  status?: CustomerStatus;
  /** Optimistic concurrency (ROW_VERSION_CONFLICT), read from the loaded row. */
  expected_row_version: number;
}

export function updateCustomer(
  id: string,
  envelope: OperationEnvelope<CustomerPatch>,
): Promise<OperationResult<Customer>> {
  return request<OperationResult<Customer>>(`/customers/${id}`, {
    method: "PATCH",
    body: { operation_id: envelope.operation_id, ...envelope.payload },
  });
}
