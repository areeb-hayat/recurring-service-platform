import { useMemo, useState } from "react";

import { messageFor } from "@/api/errors";
import { ApiError } from "@/api/errors";
import { EmptyState } from "@/components/Feedback";
import { useLocalData } from "./useLocalData";
import { useSync } from "./SyncProvider";
import type { IssueEntry } from "./types";

/**
 * Needs Attention — the operations the server refused, waiting for a person.
 *
 * What this screen deliberately does **not** do: resend, merge, overwrite or
 * "fix" anything. A conflict means another device already recorded that customer
 * for that date, and the server has said so with its own state attached. Sending
 * the same operation again would replay the same refusal; sending a *changed*
 * one under the same `operation_id` is refused outright (SYN-14). So the only
 * action offered is to read what happened and mark it reviewed.
 *
 * If a corrected entry is genuinely needed, it is a new deliberate act with a new
 * `operation_id` — made on the register like any other entry, not conjured here.
 *
 * Marking an issue reviewed keeps the row and stamps it; nothing is deleted, and
 * nothing expires on its own (SYN-12).
 */
export function IssuesPage() {
  const { issues, loading } = useLocalData();
  const { resolveIssue } = useSync();
  const [showResolved, setShowResolved] = useState(false);

  const [open, resolved] = useMemo(() => {
    const unresolved = issues.filter((issue) => issue.resolved_at === null);
    return [unresolved, issues.filter((issue) => issue.resolved_at !== null)];
  }, [issues]);

  return (
    <div className="stack">
      <header className="day-header">
        <h1 className="day-title">Needs attention</h1>
        <p className="day-progress" role="status">
          {open.length === 0
            ? "Nothing is waiting."
            : `${open.length} ${open.length === 1 ? "entry" : "entries"} the server did not accept`}
        </p>
      </header>

      {loading ? null : open.length === 0 ? (
        <EmptyState>
          Everything this device saved has been accepted by the server.
        </EmptyState>
      ) : null}

      <ul className="list issue-list">
        {open.map((issue) => (
          <li key={issue.operation_id}>
            <IssueCard
              issue={issue}
              onResolve={() => void resolveIssue(issue.operation_id)}
            />
          </li>
        ))}
      </ul>

      {resolved.length > 0 ? (
        <section className="round-list">
          <button
            className="btn btn-quiet"
            type="button"
            onClick={() => setShowResolved((v) => !v)}
          >
            {showResolved ? "Hide reviewed" : `Show reviewed (${resolved.length})`}
          </button>
          {showResolved ? (
            <ul className="list issue-list">
              {resolved.map((issue) => (
                <li key={issue.operation_id}>
                  <IssueCard issue={issue} onResolve={null} />
                </li>
              ))}
            </ul>
          ) : null}
        </section>
      ) : null}
    </div>
  );
}

function IssueCard({
  issue,
  onResolve,
}: {
  issue: IssueEntry;
  onResolve: (() => void) | null;
}) {
  const { context, server_state } = issue;
  return (
    <article className="card stack issue-card">
      <h2 className="issue-customer">{context.customer_name}</h2>
      <p className="customer-meta">
        {context.kind === "SKIP"
          ? "Skip today"
          : `Confirm ${context.quantity} ${context.unit_label}`}
        {" · "}
        {formatDate(context.service_date)}
      </p>

      <p className={issue.verdict === "CONFLICT" ? "notice" : "notice notice-error"}>
        {explain(issue)}
      </p>

      {server_state ? (
        <p className="hint">
          The server has:{" "}
          {server_state.kind === "SKIP"
            ? "a skip"
            : `${server_state.quantity} ${server_state.unit_label}`}{" "}
          for {formatDate(server_state.service_date)}.
        </p>
      ) : null}

      {onResolve ? (
        <>
          <p className="hint">
            This entry was not saved on the server. If it still needs recording,
            record it again on the round — that is a new, separate entry.
          </p>
          <button className="btn btn-secondary" type="button" onClick={onResolve}>
            I have reviewed this
          </button>
        </>
      ) : (
        <p className="hint">Reviewed {formatDateTime(issue.resolved_at)}.</p>
      )}
    </article>
  );
}

/** One sentence per code, from the same map every other screen uses. */
function explain(issue: IssueEntry): string {
  return messageFor(
    new ApiError({
      kind: "VERDICT",
      code: issue.error.code,
      status: issue.verdict === "CONFLICT" ? 409 : 422,
      detail: issue.error.detail,
    }),
  );
}

function formatDate(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  if (!y || !m || !d) return iso;
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString(undefined, {
    weekday: "long",
    day: "numeric",
    month: "long",
    timeZone: "UTC",
  });
}

function formatDateTime(iso: string | null): string {
  if (!iso) return "";
  const at = new Date(iso);
  return Number.isNaN(at.getTime()) ? iso : at.toLocaleString();
}
