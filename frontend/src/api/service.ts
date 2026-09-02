/** POST /api/v1/service/records and GET /api/v1/service/day/{date}. */

import { request } from "./client";
import type { OperationEnvelope } from "./operation";
import type { DayResponse, OperationResult, ServiceKind, ServiceRecord } from "./types";

/**
 * The day's register.
 *
 * `date` is the client's *guess* at the tenant's today, and the response carries
 * the authoritative `business_date` beside it. The caller compares them and
 * refetches if they differ, so the screen is always showing the server's business
 * date rather than the browser's idea of one (P0 R4). The client never decides.
 */
export function getDay(date: string): Promise<DayResponse> {
  return request<DayResponse>(`/service/day/${date}`);
}

export interface ServiceIntent {
  customer_id: string;
  kind: ServiceKind;
  /** Omitted for a SKIP: the backend takes no quantity for one. */
  quantity?: string;
  /**
   * Deliberately absent for the daily register. `service_date` is optional on
   * `RecordServiceRequest` and the server resolves "today" from the tenant
   * timezone when it is omitted, which is the only authority worth trusting at
   * a midnight boundary.
   */
  service_date?: string;
  input_method: "BUTTON";
}

export function recordService(
  envelope: OperationEnvelope<ServiceIntent>,
): Promise<OperationResult<ServiceRecord>> {
  return request<OperationResult<ServiceRecord>>("/service/records", {
    method: "POST",
    body: { operation_id: envelope.operation_id, ...envelope.payload },
  });
}
