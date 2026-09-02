/**
 * The API shapes P4 consumes.
 *
 * Hand-written to mirror the backend serializers exactly, not invented: every
 * field below appears in `app/customers/commands.py::serialize_customer`,
 * `app/service/commands.py::serialize_record`, `app/api/schemas.py`, or a route
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

/** Every mutation's reply (`OperationResponse`). */
export interface OperationResult<T> {
  status: OperationStatus;
  entity: T;
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
