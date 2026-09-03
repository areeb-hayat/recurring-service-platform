/**
 * The operation envelope (P0 §7.2) and the one rule that makes retries safe.
 *
 * `operation_id` is generated **once, at the moment of user intent** — the tap on
 * CONFIRM, SKIP or Save — and is never regenerated. Not when the fetch fails, not
 * when the request times out, not when the person presses Retry. A lost response
 * is indistinguishable from a lost request, so the only safe thing to send is the
 * *same* operation: the server either applies it or answers DUPLICATE with the
 * original result (P0 §7.6).
 *
 * The envelope is deliberately a plain, serialisable value with exactly the P0
 * §7.2 field names. P5 writes this object into the IndexedDB `outbox` before the
 * network call and moves it to `issues` on a REJECTED/CONFLICT verdict; nothing
 * about the shape has to change for that. P4 keeps it in memory only — there is
 * no outbox yet and none is pretended.
 */

import { uuidv7 } from "@/lib/uuid";

/**
 * The op types this client sends.
 *
 * P0 §7.2's enumeration is the envelope's *extensible* vocabulary, not a promise
 * about what may be queued: the offline write guarantee in V1 is CONFIRM and
 * SKIP alone. The payment and operating-cost types below travel on this same
 * envelope, with the same generated-once `operation_id`, but only ever straight
 * to the server — they are never written to the outbox, and `POST
 * /sync/operations` refuses them.
 */
export type OpType =
  | "customer.create"
  | "customer.update"
  | "service.record"
  | "service.skip"
  // Online-only (PAY-8).
  | "payment.record"
  | "payment.void"
  // Online-only (P6 §19).
  | "cost.item.create"
  | "cost.rate.create"
  | "cost.usage.record"
  | "cost.actual.record"
  // Online-only (P7 §19). Reminder generation and delivery are server-only, so
  // this never enters the outbox — it is a manual re-dispatch of a stage the
  // server already created, not a write a device may queue.
  | "reminder.send"
  // Online-only (P8 §13). An alias write bumps its customer's `row_version`, so
  // the change reaches every device through the ordinary customer feed — but the
  // write itself is never queued: aliases are customer data, and customer edits
  // have always been online-only. The offline guarantee stays CONFIRM and SKIP.
  | "customer.alias.add"
  | "customer.alias.update"
  | "customer.alias.deactivate";

export interface OperationEnvelope<P> {
  operation_id: string;
  op_type: OpType;
  payload: P;
  client_created_at: string;
}

/** Call this exactly once per user intent, never inside a retry path. */
export function createOperation<P>(op_type: OpType, payload: P): OperationEnvelope<P> {
  return {
    operation_id: uuidv7(),
    op_type,
    payload,
    client_created_at: new Date().toISOString(),
  };
}
