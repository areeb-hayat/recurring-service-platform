/**
 * Error classification and the user-facing message map.
 *
 * Two kinds of failure exist and they are not the same thing:
 *
 *  - a **verdict** — the server looked at the operation and answered. It is
 *    terminal for retry.
 *  - a **transport failure** — the network dropped, the request timed out, or
 *    the server returned 5xx. That is *not* an answer (P0 §7.3): the operation
 *    may well have been applied. It is retryable, and it must be retried with
 *    the same `operation_id`.
 *
 * Nothing here ever renders `detail` from a 5xx or an unknown code straight to
 * the user; the map below decides what a person sees.
 */

export type FailureKind = "VERDICT" | "TRANSPORT" | "AUTH";

export class ApiError extends Error {
  readonly kind: FailureKind;
  readonly code: string;
  readonly status: number;
  readonly detail: string;
  readonly fieldErrors: Record<string, string>;
  readonly extra: Record<string, unknown>;

  constructor(init: {
    kind: FailureKind;
    code: string;
    status: number;
    detail: string;
    fieldErrors?: Record<string, string>;
    extra?: Record<string, unknown>;
  }) {
    super(init.detail);
    this.name = "ApiError";
    this.kind = init.kind;
    this.code = init.code;
    this.status = init.status;
    this.detail = init.detail;
    this.fieldErrors = init.fieldErrors ?? {};
    this.extra = init.extra ?? {};
  }

  /** A transport failure is not a verdict, so the same operation may be resent. */
  get isRetryable(): boolean {
    return this.kind === "TRANSPORT";
  }
}

/**
 * Backend code -> what to tell the person, phrased as the situation or the next
 * action rather than as a fault (P0 §8.7: no accounting jargon, no stack traces,
 * no database text).
 */
const MESSAGES: Record<string, string> = {
  UNAUTHENTICATED: "Your session has ended. Please sign in again.",
  PERMISSION_DENIED: "You do not have access to that.",
  NOT_FOUND: "We could not find that.",
  VALIDATION: "Please check the highlighted fields and try again.",
  CONFLICT: "Someone else changed this first. Reload and try again.",
  SERVICE_ALREADY_RECORDED:
    "Today is already recorded for this customer. Reload to see what was saved.",
  CYCLE_ROLLOVER_REQUIRED:
    "The current billing period has ended. Close it before recording more work.",
  CYCLE_PERIOD_NOT_ENDED: "This period has not finished yet.",
  CUSTOMER_CODE_TAKEN: "That customer code is already in use. Choose another.",
  ROW_VERSION_CONFLICT:
    "Someone else updated this customer while you were editing. Reload and try again.",
  IDEMPOTENCY_KEY_REUSE:
    "This action was already sent with different details. Reload and start again.",
  NETWORK: "Could not reach the server. Check your connection and try again.",
  SERVER: "The server had a problem. Nothing was lost — try again.",
};

const LOGIN_FAILED = "Email or password is not correct.";

export function messageFor(error: unknown, context?: "login"): string {
  if (!(error instanceof ApiError)) {
    return "Something went wrong. Please try again.";
  }
  if (context === "login" && error.code === "UNAUTHENTICATED") return LOGIN_FAILED;
  return MESSAGES[error.code] ?? MESSAGES.SERVER!;
}

export function fieldErrorFor(error: unknown, field: string): string | undefined {
  return error instanceof ApiError ? error.fieldErrors[field] : undefined;
}
