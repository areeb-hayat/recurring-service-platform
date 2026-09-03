/**
 * Customer aliases — `/api/v1/customers/{id}/aliases` (P8).
 *
 * The names a customer is actually called. One read and three writes, all under
 * the existing `customer:read` / `customer:write` capabilities, because an alias
 * is customer data.
 *
 * **Online only, exactly as customer create and edit are.** Every write here
 * carries an `operation_id` generated once at the click and travels straight to
 * the server; none of them is in the offline mutation registry and
 * `POST /sync/operations` refuses them. V1's offline write guarantee is CONFIRM
 * and SKIP (P0 §7.2, as clarified), and P8 does not widen it.
 *
 * **There is no delete.** An alias that is no longer used is *retired* — the row
 * stays, the audit trail keeps the text before and after, and the name stops
 * matching. How somebody was known last year is what explains an audit row from
 * last year.
 *
 * **The client never normalizes.** It sends what the person typed; the server
 * derives the comparison key with the one normalization path. Two clients
 * normalizing for themselves would be two definitions of "the same name".
 */

import { request } from "./client";
import type { OperationEnvelope } from "./operation";
import type { CustomerAlias, ListResponse, OperationResult } from "./types";

export function listAliases(
  customerId: string,
  includeInactive = false,
): Promise<ListResponse<CustomerAlias>> {
  const suffix = includeInactive ? "?include_inactive=true" : "";
  return request<ListResponse<CustomerAlias>>(
    `/customers/${customerId}/aliases${suffix}`,
  );
}

export interface AliasDraft {
  alias: string;
}

export function addAlias(
  customerId: string,
  envelope: OperationEnvelope<AliasDraft>,
): Promise<OperationResult<CustomerAlias>> {
  return request<OperationResult<CustomerAlias>>(`/customers/${customerId}/aliases`, {
    method: "POST",
    body: { operation_id: envelope.operation_id, alias: envelope.payload.alias },
  });
}

export function updateAlias(
  customerId: string,
  aliasId: string,
  envelope: OperationEnvelope<AliasDraft>,
): Promise<OperationResult<CustomerAlias>> {
  return request<OperationResult<CustomerAlias>>(
    `/customers/${customerId}/aliases/${aliasId}`,
    {
      method: "PATCH",
      body: { operation_id: envelope.operation_id, alias: envelope.payload.alias },
    },
  );
}

export interface AliasRetirement {
  reason: string | null;
}

/** Retire an alias. Deliberately a POST to `/deactivate`, never a DELETE. */
export function deactivateAlias(
  customerId: string,
  aliasId: string,
  envelope: OperationEnvelope<AliasRetirement>,
): Promise<OperationResult<CustomerAlias>> {
  return request<OperationResult<CustomerAlias>>(
    `/customers/${customerId}/aliases/${aliasId}/deactivate`,
    {
      method: "POST",
      body: { operation_id: envelope.operation_id, reason: envelope.payload.reason },
    },
  );
}
