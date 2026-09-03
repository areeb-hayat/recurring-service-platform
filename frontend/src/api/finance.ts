/**
 * The owner's financial reads and the two manual payment writes.
 *
 * Reads here are **online** reads. Where a screen must also work offline it
 * reads the same rows out of the P5 snapshot instead (`sync/useLocalData`), and
 * the two never disagree because both hold the server's own serialization.
 *
 * Writes here are **online-only**, by V1 scope (PAY-8): recording a payment and
 * voiding one are not accepted sync operations and are never queued into the
 * outbox. They still carry an `operation_id` generated once at the moment of
 * intent, so a retry after a lost response replays rather than double-pays.
 */

import { request } from "./client";
import type { OperationEnvelope } from "./operation";
import type {
  DashboardSummary,
  ListResponse,
  OperationResult,
  OutstandingResponse,
  Payment,
  PaymentMethod,
  ServiceRecord,
  Statement,
} from "./types";

// --- dashboard ---------------------------------------------------------------

export function getDashboardSummary(): Promise<DashboardSummary> {
  return request<DashboardSummary>("/dashboard/summary");
}

export function getOutstanding(limit = 100): Promise<OutstandingResponse> {
  return request<OutstandingResponse>(`/dashboard/outstanding?limit=${limit}`);
}

// --- statements --------------------------------------------------------------

export const STATEMENT_PAGE_SIZE = 500;

/** One page of the tenant-wide statement list, newest period first. */
export function listStatements(
  limit = STATEMENT_PAGE_SIZE,
  offset = 0,
): Promise<ListResponse<Statement>> {
  return request<ListResponse<Statement>>(
    `/statements?limit=${limit}&offset=${offset}`,
  );
}

/**
 * Every statement, following the pagination contract to its end.
 *
 * The response carries no total and no cursor, so a short page is the only
 * end-of-list signal — the same shape, and the same reasoning, as
 * `listAllCustomers`. Used to seed the snapshot, where a silent truncation
 * would mean statements that simply never appear offline.
 */
export async function listAllStatements(): Promise<Statement[]> {
  const all: Statement[] = [];
  const seen = new Set<string>();
  for (let offset = 0; ; offset += STATEMENT_PAGE_SIZE) {
    const page = await listStatements(STATEMENT_PAGE_SIZE, offset);
    for (const row of page.items) {
      if (seen.has(row.id)) continue;
      seen.add(row.id);
      all.push(row);
    }
    if (page.items.length < STATEMENT_PAGE_SIZE) return all;
  }
}

export function getStatement(id: string): Promise<Statement> {
  return request<Statement>(`/statements/${id}`);
}

export function listCustomerStatements(
  customerId: string,
): Promise<ListResponse<Statement>> {
  return request<ListResponse<Statement>>(`/customers/${customerId}/statements`);
}

// --- payments ----------------------------------------------------------------

export const PAYMENT_PAGE_SIZE = 500;

export function listPayments(
  limit = PAYMENT_PAGE_SIZE,
  offset = 0,
): Promise<ListResponse<Payment>> {
  return request<ListResponse<Payment>>(
    `/payments?limit=${limit}&offset=${offset}`,
  );
}

export async function listAllPayments(): Promise<Payment[]> {
  const all: Payment[] = [];
  const seen = new Set<string>();
  for (let offset = 0; ; offset += PAYMENT_PAGE_SIZE) {
    const page = await listPayments(PAYMENT_PAGE_SIZE, offset);
    for (const row of page.items) {
      if (seen.has(row.id)) continue;
      seen.add(row.id);
      all.push(row);
    }
    if (page.items.length < PAYMENT_PAGE_SIZE) return all;
  }
}

export function listCustomerPayments(
  customerId: string,
): Promise<ListResponse<Payment>> {
  return request<ListResponse<Payment>>(`/customers/${customerId}/payments`);
}

export function listCustomerHistory(
  customerId: string,
): Promise<ListResponse<ServiceRecord>> {
  return request<ListResponse<ServiceRecord>>(`/customers/${customerId}/history`);
}

/** The body `RecordPaymentRequest` accepts, minus `operation_id`. */
export interface PaymentDraft {
  customer_id: string;
  /** Integer minor units. The client formats money; it never computes it. */
  amount_minor: number;
  method: PaymentMethod;
  /** Omit for "today": the server resolves the tenant's business date (R4). */
  received_on?: string;
  reference?: string | null;
  note?: string | null;
}

export function recordPayment(
  envelope: OperationEnvelope<PaymentDraft>,
): Promise<OperationResult<Payment>> {
  return request<OperationResult<Payment>>("/payments", {
    method: "POST",
    body: { operation_id: envelope.operation_id, ...envelope.payload },
  });
}

export interface VoidPaymentDraft {
  /** AUD-6: mandatory. There is no void without a stated reason. */
  reason: string;
}

export function voidPayment(
  paymentId: string,
  envelope: OperationEnvelope<VoidPaymentDraft>,
): Promise<OperationResult<Payment>> {
  return request<OperationResult<Payment>>(`/payments/${paymentId}/void`, {
    method: "POST",
    body: { operation_id: envelope.operation_id, ...envelope.payload },
  });
}
