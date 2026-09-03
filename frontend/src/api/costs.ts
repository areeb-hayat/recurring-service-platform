/**
 * Operating costs — `/api/v1/operating-costs/*` (P6).
 *
 * Every one of these is an **online** call. Operating costs are not in the sync
 * feed and never enter the outbox: the screen says "unavailable offline" rather
 * than showing a figure it cannot vouch for (P6 §19, SYN-9).
 *
 * Nothing here computes money. The estimate, the variance and every total come
 * back already computed from a configured rate — the client sends a usage
 * quantity as a *string* and renders integer minor units, exactly as it does
 * everywhere else.
 */

import { request } from "./client";
import type { OperationEnvelope } from "./operation";
import type {
  CostHistory,
  CostItem,
  CostItemsResponse,
  CostRate,
  CostRecurrence,
  CostScenarioResponse,
  CostSummary,
  OperationResult,
} from "./types";

export function listCostItems(): Promise<CostItemsResponse> {
  return request<CostItemsResponse>("/operating-costs/items");
}

/** `month` is a month-start date, e.g. `2026-03-01`. Omit for this month. */
export function getCostSummary(month?: string): Promise<CostSummary> {
  const query = month ? `?month=${month}` : "";
  return request<CostSummary>(`/operating-costs/summary${query}`);
}

export function getCostHistory(months = 12, month?: string): Promise<CostHistory> {
  const params = new URLSearchParams({ months: String(months) });
  if (month) params.set("month", month);
  return request<CostHistory>(`/operating-costs/history?${params.toString()}`);
}

export interface CostItemDraft {
  code: string;
  name: string;
  description?: string | null;
}

export function createCostItem(
  envelope: OperationEnvelope<CostItemDraft>,
): Promise<OperationResult<CostItem>> {
  return request<OperationResult<CostItem>>("/operating-costs/items", {
    method: "POST",
    body: { operation_id: envelope.operation_id, ...envelope.payload },
  });
}

/**
 * A new rate. Exactly one pricing shape — priced per unit of usage, or fixed.
 *
 * There is no `effective_to`: a rate is open-ended and is closed by its
 * successor, so no request can leave a gap with no rate in force.
 */
export interface CostRateDraft {
  effective_from: string;
  unit?: string | null;
  unit_price_minor?: number | null;
  fixed_amount_minor?: number | null;
  fixed_recurrence?: CostRecurrence | null;
  currency?: string | null;
  currency_exponent?: number | null;
  source_note?: string | null;
}

export function createCostRate(
  costItemId: string,
  envelope: OperationEnvelope<CostRateDraft>,
): Promise<OperationResult<CostRate>> {
  return request<OperationResult<CostRate>>(
    `/operating-costs/items/${costItemId}/rates`,
    {
      method: "POST",
      body: { operation_id: envelope.operation_id, ...envelope.payload },
    },
  );
}

export interface CostUsageDraft {
  cost_item_id: string;
  period_month: string;
  /** A decimal string, never a JS number: this is a measured quantity. */
  usage_quantity: string;
  inputs?: Record<string, unknown> | null;
  note?: string | null;
  /** Required only when replacing a month that already has a figure. */
  correction_reason?: string | null;
}

export function recordCostUsage(
  envelope: OperationEnvelope<CostUsageDraft>,
): Promise<OperationResult<unknown>> {
  return request<OperationResult<unknown>>("/operating-costs/usage", {
    method: "POST",
    body: { operation_id: envelope.operation_id, ...envelope.payload },
  });
}

export interface CostActualDraft {
  cost_item_id: string;
  period_month: string;
  amount_minor: number;
  currency?: string | null;
  currency_exponent?: number | null;
  invoice_reference?: string | null;
  note?: string | null;
  correction_reason?: string | null;
}

export function recordCostActual(
  envelope: OperationEnvelope<CostActualDraft>,
): Promise<OperationResult<unknown>> {
  return request<OperationResult<unknown>>("/operating-costs/actuals", {
    method: "POST",
    body: { operation_id: envelope.operation_id, ...envelope.payload },
  });
}

export interface ScenarioEntry {
  label?: string;
  cost_item_id: string;
  usage_quantity?: string;
  events_per_day?: number;
  seconds_per_event?: string;
  days?: number;
}

/**
 * Price a few planning cases. Writes nothing, so it carries no `operation_id`.
 *
 * The events/seconds/days conversion happens **on the server**, with the rate:
 * the client would have to divide to produce hours, and dividing on the client
 * is how a planning figure quietly stops matching the one the server records.
 */
export function priceScenarios(
  scenarios: ScenarioEntry[],
  periodMonth?: string,
): Promise<CostScenarioResponse> {
  return request<CostScenarioResponse>("/operating-costs/scenarios", {
    method: "POST",
    body: { period_month: periodMonth, scenarios },
  });
}
