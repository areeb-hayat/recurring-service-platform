/**
 * The API shapes the frontend consumes.
 *
 * Hand-written to mirror the backend serializers exactly, not invented: every
 * field below appears in `app/customers/commands.py::serialize_customer`,
 * `app/service/commands.py::serialize_record`, `app/payments/commands.py::
 * serialize_payment`, `app/billing/statements.py::serialize_statement`,
 * `app/billing/dashboard.py`, `app/costs/`, `app/api/schemas.py`, or a route
 * body in `app/api/routes.py`. Nothing speculative is declared — if a field is
 * not here, the backend does not send it.
 *
 * Money is `number` because it is an integer count of minor units and is only
 * ever displayed (P0 §5). Quantity is `string` because it is a decimal the
 * client must not put through binary floating point (see `lib/decimal.ts`).
 */

export type OperationStatus = "APPLIED" | "DUPLICATE";
export type CustomerStatus = "ACTIVE" | "INACTIVE";
export type ServiceKind = "SERVICE" | "SKIP";
export type PaymentMethod = "CASH" | "BANK_TRANSFER" | "OTHER";
export type PaymentRowStatus = "RECORDED" | "VOIDED";

/** POST /api/v1/auth/login | /refresh — `TokenResponse`. */
export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: "bearer";
  expires_in: number;
  role: string;
  scope: string;
  tenant_id: string | null;
}

/**
 * GET /api/v1/tenant/settings — the tenant's own configuration.
 *
 * The client renders currency, exponent and unit label from here rather than
 * assuming any of them: they are per-tenant configuration on the `tenant` row
 * (P0 §4), not constants. `business_date` is the tenant timezone's today,
 * resolved server-side (P0 R4).
 */
export interface TenantSettings {
  name: string;
  currency: string;
  currency_exponent: number;
  unit_label: string;
  timezone: string;
  business_date: string;
  default_quantity: string;
  default_unit_price_minor: number;
}

/** `serialize_customer` — as returned by GET /customers and inside an entity. */
export interface Customer {
  id: string;
  code: string;
  name: string;
  phone_e164: string | null;
  whatsapp_e164: string | null;
  address: string | null;
  area: string | null;
  default_quantity: string;
  unit_price_minor: number;
  status: CustomerStatus;
  row_version: number;
  unit_label: string;
  currency: string;
  currency_exponent: number;
}

/**
 * GET /customers/{id} adds two *derived* fields the route computes on read.
 * They are server-authoritative: the client displays them and never recomputes
 * either one (FIN-4 / FIN-11).
 */
export interface CustomerDetail extends Customer {
  outstanding_minor: number;
  payment_status: string;
}

export interface CustomerListResponse {
  items: Customer[];
}

/** `serialize_record`. */
export interface ServiceRecord {
  id: string;
  customer_id: string;
  service_date: string;
  quantity: string;
  unit_price_minor: number;
  unit_label: string;
  charge_minor: number;
  kind: ServiceKind;
  status: string;
  corrects_id: string | null;
  superseded_by_id: string | null;
  adjustment_minor: number;
  reason: string | null;
  source: string;
  input_method: string;
  operation_id: string;
  recorded_at: string | null;
  row_version: number;
  currency: string;
  currency_exponent: number;
}

/**
 * GET /service/day/{date}. `business_date` is the tenant's own today, resolved
 * server-side from the tenant timezone — the client never decides what "today"
 * is, it reads it from here.
 */
export interface DayResponse {
  service_date: string;
  business_date: string;
  items: ServiceRecord[];
}

/** `serialize_payment` — a manual payment (PAY-1/2). */
export interface Payment {
  id: string;
  customer_id: string;
  amount_minor: number;
  method: PaymentMethod;
  received_on: string;
  reference: string | null;
  note: string | null;
  status: PaymentRowStatus;
  voided_reason: string | null;
  voided_at: string | null;
  operation_id: string;
  source: string;
  recorded_at: string | null;
  row_version: number;
  currency: string;
  currency_exponent: number;
}

/**
 * `serialize_statement` — one immutable issued statement (FIN-8).
 *
 * The five movement figures arrive **already split by origin**: a service
 * correction and a payment reversal are different columns even though both are
 * ADJUSTMENT rows in the ledger. Nothing here is added up on the client; the
 * closing balance is the server's own.
 */
export interface Statement {
  id: string;
  customer_id: string;
  cycle_id: string;
  issued_at: string | null;
  opening_balance_minor: number;
  charges_minor: number;
  service_adjustments_minor: number;
  payments_minor: number;
  payment_reversals_minor: number;
  closing_balance_minor: number;
  service_days: number;
  total_quantity: string;
  unit_label: string;
  currency: string;
  currency_exponent: number;
  row_version: number;
}

export interface BillingCycle {
  id: string;
  period_start: string;
  period_end: string;
  status: string;
  closed_at: string | null;
}

/** The four §11.1 derivations. Four distinct figures, never one from another. */
export interface ReportingTotals {
  business_generated_minor: number;
  billed_value_minor: number;
  collected_minor: number;
  outstanding_minor: number;
}

export interface RecentPayment {
  id: string;
  customer_id: string;
  customer_name: string;
  customer_code: string;
  amount_minor: number;
  method: PaymentMethod;
  received_on: string;
  status: PaymentRowStatus;
  reference: string | null;
  recorded_at: string | null;
}

/** GET /dashboard/summary — every figure derived server-side. */
export interface DashboardSummary {
  business_date: string;
  currency: string;
  currency_exponent: number;
  unit_label: string;
  open_cycle: BillingCycle | null;
  outstanding_minor: number;
  all_time: ReportingTotals;
  /** Null — not zeros — when no cycle is open. */
  current_cycle: ReportingTotals | null;
  customers: {
    total: number;
    active: number;
    with_balance_due: number;
    in_credit: number;
  };
  recent_payments: RecentPayment[];
}

export interface OutstandingCustomer {
  customer_id: string;
  code: string;
  name: string;
  area: string | null;
  status: CustomerStatus;
  outstanding_minor: number;
}

export interface OutstandingResponse {
  currency: string;
  currency_exponent: number;
  items: OutstandingCustomer[];
}

// --- operating costs (P6) ----------------------------------------------------
//
// What the business pays its providers. A separate concept from the customer
// ledger above and from platform commission, which the tenant cannot see at all.

export type CostRecurrence = "MONTHLY" | "ANNUAL";

export interface CostRate {
  id: string;
  cost_item_id: string;
  effective_from: string;
  effective_to: string | null;
  unit: string | null;
  unit_price_minor: number | null;
  fixed_amount_minor: number | null;
  fixed_recurrence: CostRecurrence | null;
  currency: string;
  currency_exponent: number;
  source_note: string | null;
  created_at: string | null;
}

export interface CostItem {
  id: string;
  code: string;
  name: string;
  description: string | null;
  status: string;
  created_at: string | null;
  rates: CostRate[];
}

export interface CostItemsResponse {
  items: CostItem[];
}

/**
 * One cost item for one month.
 *
 * `null` is meaningful throughout and is never rendered as zero: no measured
 * usage means no estimate, no invoice means no actual and therefore no variance
 * (P6 §14).
 */
export interface CostLine {
  cost_item_id: string;
  code: string;
  name: string;
  period_month: string;
  currency: string;
  currency_exponent: number;
  rate: CostRate | null;
  usage_quantity: string | null;
  usage_unit: string | null;
  usage_inputs: Record<string, unknown> | null;
  estimated_amount_minor: number | null;
  actual_amount_minor: number | null;
  actual_invoice_reference: string | null;
  variance_minor: number | null;
  usage_id: string | null;
  actual_id: string | null;
}

/** Totals are per currency: provider prices are not converted (P6 §18). */
export interface CostTotals {
  currency: string;
  estimated_minor: number | null;
  actual_minor: number | null;
  variance_minor: number | null;
}

export interface CostSummary {
  period_month: string;
  lines: CostLine[];
  totals: CostTotals[];
}

export interface CostHistoryMonth {
  period_month: string;
  totals: CostTotals[];
}

export interface CostHistory {
  from_month: string;
  to_month: string;
  months: CostHistoryMonth[];
  range_totals: CostTotals[];
}

export interface CostScenarioResult {
  label: string | null;
  cost_item_id: string;
  code: string;
  name: string;
  period_month: string;
  usage_quantity: string | null;
  usage_unit: string | null;
  derived_from: {
    events_per_day: number;
    seconds_per_event: string;
    days: number;
  } | null;
  estimated_amount_minor: number | null;
  currency: string;
  currency_exponent: number;
  rate: CostRate | null;
}

export interface CostScenarioResponse {
  period_month: string;
  results: CostScenarioResult[];
  totals: { currency: string; estimated_minor: number | null }[];
}

/** Every mutation's reply (`OperationResponse`). */
export interface OperationResult<T> {
  status: OperationStatus;
  entity: T;
}

export interface ListResponse<T> {
  items: T[];
}

/** The one frozen error envelope (P0 §15). */
export interface ApiErrorBody {
  error: {
    code: string;
    detail: string;
    field_errors?: Record<string, string>;
    [extra: string]: unknown;
  };
}

// --- reminders (P7) ----------------------------------------------------------
//
// Read-only shapes. The client renders these and filters on `status`; it never
// derives a stage, an eligibility or an amount of its own — those are the
// server's, computed from the tenant's schedule and the ledger.

export type ReminderKind = "STATEMENT" | "REMINDER" | "FINAL" | "OWNER_ALERT";
export type ReminderState = "PENDING" | "SENT" | "FAILED" | "CANCELLED";

/** The owner's five buckets, derived server-side. */
export type ReminderStatus =
  | "DUE"
  | "WAITING"
  | "ATTENTION"
  | "SETTLED"
  | "NO_STATEMENT";

export interface ReminderStage {
  day: number;
  kind: ReminderKind;
}

export interface Reminder {
  id: string;
  customer_id: string;
  cycle_id: string;
  schedule_day: number;
  kind: ReminderKind;
  state: ReminderState;
  /** The balance when the stage was generated — NOT what was delivered. */
  amount_minor_at_generation: number;
  attempt_count: number;
  last_error: string | null;
  generated_at: string | null;
  sent_at: string | null;
  cancelled_at: string | null;
}

export interface ReminderCycleRef {
  cycle_id: string;
  statement_id: string;
  period_start: string;
  period_end: string;
  statement_closing_balance_minor: number;
}

export interface ReminderRow {
  customer_id: string;
  code: string;
  name: string;
  area: string | null;
  customer_status: CustomerStatus;
  /** Live and authoritative, from the ledger — never a stored reminder amount. */
  outstanding_minor: number;
  status: ReminderStatus;
  has_contact: boolean;
  cycle: ReminderCycleRef | null;
  latest: Reminder | null;
  next_stage: ReminderStage | null;
  owner_alert: Reminder | null;
  history: Reminder[];
}

export interface ReminderOverview {
  business_date: string;
  currency: string;
  currency_exponent: number;
  /** The tenant's configured schedule. The client shows it; it does not know it. */
  schedule: ReminderStage[];
  due_stage: ReminderStage | null;
  counts: { total: number; due: number; attention: number; settled: number };
  items: ReminderRow[];
}

export interface ReminderAttempt {
  id: string;
  channel: "WHATSAPP" | "SMS" | "EMAIL";
  provider: string;
  template_key: string;
  state: "QUEUED" | "ACCEPTED" | "DELIVERED" | "FAILED";
  error: string | null;
  attempt_no: number;
  /** The already-rendered values handed to the provider (REM-7). */
  payload: Record<string, string> | null;
  created_at: string | null;
}

export interface ReminderDetail extends Reminder {
  is_outstanding_reminder: boolean;
  outstanding_minor: number;
  currency: string;
  currency_exponent: number;
  attempts: ReminderAttempt[];
}
