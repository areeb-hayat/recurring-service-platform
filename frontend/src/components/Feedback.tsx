import type { ReactNode } from "react";

/**
 * The three states every asynchronous screen has, in one place so they read the
 * same everywhere. Errors are announced (`role="alert"`); loading and success are
 * polite (`role="status"`), so a screen reader is not interrupted mid-sentence by
 * a spinner.
 */

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <p className="notice" role="status">
      {label}
    </p>
  );
}

export function ErrorNotice({
  message,
  onRetry,
  retryLabel = "Try again",
}: {
  message: string;
  onRetry?: () => void;
  retryLabel?: string;
}) {
  return (
    <div className="notice notice-error" role="alert">
      <span>{message}</span>
      {onRetry ? (
        <button className="btn btn-quiet" type="button" onClick={onRetry}>
          {retryLabel}
        </button>
      ) : null}
    </div>
  );
}

export function SuccessNotice({ children }: { children: ReactNode }) {
  return (
    <p className="notice notice-success" role="status">
      {children}
    </p>
  );
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <p className="empty">{children}</p>;
}
