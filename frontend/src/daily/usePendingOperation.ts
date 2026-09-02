/**
 * One unresolved write, and the rule that it keeps its identity.
 *
 * An envelope is created once, when the person taps CONFIRM or SKIP. From then
 * on the hook is in one of three states:
 *
 *  - **idle** — nothing outstanding;
 *  - **sending** — in flight;
 *  - **unresolved** — a transport failure. The envelope is *kept*. Retry resends
 *    it byte-for-byte, so if the first attempt did reach the server the reply is
 *    DUPLICATE and nothing is recorded twice (P0 §7.6).
 *
 * A server verdict — applied, rejected or conflicting — ends the operation
 * either way: the server has answered, and answering again with the same id
 * would only replay the same answer. A new intent gets a new envelope.
 *
 * This is why the quantity control is locked while an operation is unresolved.
 * Editing the quantity and pressing Retry would send a *different* payload under
 * the same `operation_id`, which SYN-14 correctly refuses. The person can retry
 * what they asked for, or discard it and start again — not silently mutate it.
 */

import { useCallback, useRef, useState } from "react";

import { ApiError } from "@/api/errors";
import { createOperation, type OperationEnvelope, type OpType } from "@/api/operation";

export type OperationPhase = "idle" | "sending" | "unresolved";

export interface PendingOperation<P, R> {
  phase: OperationPhase;
  error: ApiError | null;
  /** The id currently in flight or awaiting retry — surfaced for tests and support. */
  operationId: string | null;
  start: (op_type: OpType, payload: P) => Promise<R | null>;
  retry: () => Promise<R | null>;
  discard: () => void;
}

export function usePendingOperation<P, R>(
  send: (envelope: OperationEnvelope<P>) => Promise<R>,
  onApplied: (result: R) => void,
): PendingOperation<P, R> {
  const [phase, setPhase] = useState<OperationPhase>("idle");
  const [error, setError] = useState<ApiError | null>(null);
  const [operationId, setOperationId] = useState<string | null>(null);
  const envelopeRef = useRef<OperationEnvelope<P> | null>(null);

  const dispatch = useCallback(
    async (envelope: OperationEnvelope<P>): Promise<R | null> => {
      setPhase("sending");
      setError(null);
      try {
        const result = await send(envelope);
        envelopeRef.current = null;
        setOperationId(null);
        setPhase("idle");
        onApplied(result);
        return result;
      } catch (cause) {
        const failure =
          cause instanceof ApiError
            ? cause
            : new ApiError({
                kind: "TRANSPORT",
                code: "NETWORK",
                status: 0,
                detail: "network error",
              });
        setError(failure);
        if (failure.isRetryable) {
          // Not a verdict: keep the envelope so a retry reuses the same id.
          setPhase("unresolved");
        } else {
          envelopeRef.current = null;
          setOperationId(null);
          setPhase("idle");
        }
        return null;
      }
    },
    [send, onApplied],
  );

  const start = useCallback(
    (op_type: OpType, payload: P) => {
      const envelope = createOperation(op_type, payload);
      envelopeRef.current = envelope;
      setOperationId(envelope.operation_id);
      return dispatch(envelope);
    },
    [dispatch],
  );

  const retry = useCallback(() => {
    const envelope = envelopeRef.current;
    if (!envelope) return Promise.resolve(null);
    return dispatch(envelope); // same envelope, same operation_id
  }, [dispatch]);

  const discard = useCallback(() => {
    envelopeRef.current = null;
    setOperationId(null);
    setError(null);
    setPhase("idle");
  }, []);

  return { phase, error, operationId, start, retry, discard };
}
