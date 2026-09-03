/** POST /api/v1/sync/operations and GET /api/v1/sync/changes. */

import { request } from "@/api/client";
import type { OperationEnvelope } from "@/api/operation";
import type { ServiceIntent } from "@/api/service";
import type { SyncChangesResponse, SyncPushResponse } from "./types";

/**
 * Push queued envelopes.
 *
 * The body is the envelope exactly as it was stored — same `operation_id`, same
 * payload — on the first attempt and on every retry. A refresh of an expired
 * access token replays this identical request from inside `api/client`, so a
 * token expiring mid-push cannot change what is being asked for.
 */
export function pushOperations(
  envelopes: OperationEnvelope<ServiceIntent>[],
): Promise<SyncPushResponse> {
  return request<SyncPushResponse>("/sync/operations", {
    method: "POST",
    body: {
      operations: envelopes.map((envelope) => ({
        operation_id: envelope.operation_id,
        op_type: envelope.op_type,
        payload: envelope.payload,
        client_created_at: envelope.client_created_at,
      })),
    },
  });
}

export function getChanges(since: number, limit?: number): Promise<SyncChangesResponse> {
  const params = new URLSearchParams({ since: String(since) });
  if (limit !== undefined) params.set("limit", String(limit));
  return request<SyncChangesResponse>(`/sync/changes?${params.toString()}`);
}
