import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { getOutstanding } from "@/api/finance";
import { messageFor } from "@/api/errors";
import type { OutstandingResponse } from "@/api/types";
import { ErrorNotice, Loading } from "@/components/Feedback";
import { formatMoney } from "@/lib/money";
import { useSync } from "@/sync/SyncProvider";
import { useCachedDashboard } from "@/sync/useLocalData";

/**
 * The owner's landing screen.
 *
 * **Every figure here was computed by the server** and is printed exactly as it
 * arrived (FIN-4, FIN-11, SYN-9). Nothing on this page adds up a list of
 * customers, and nothing derives one total from another — business generated,
 * collected and outstanding are three distinct answers the ledger gives to three
 * distinct questions, separated by adjustment origin (P0 §11.1).
 *
 * **Offline it shows the last summary it was given, stamped with when.** That is
 * the honest middle ground between a blank screen and a lie: a cached total is
 * useful precisely because it says how old it is. It is never recomputed locally
 * to "freshen" it.
 *
 * Commission does not appear, at all. It is platform scope and an owner-admin
 * token cannot read it. Operating costs do not appear either: what the business
 * pays its providers is a separate account with its own screen, and a figure
 * that mixed the two would mean nothing.
 */
export function DashboardPage() {
  const { online, refreshDashboard } = useSync();
  const { summary, readAt, loading } = useCachedDashboard();
  const [error, setError] = useState<unknown>(null);
  const [outstanding, setOutstanding] = useState<OutstandingResponse | null>(null);

  useEffect(() => {
    if (!online) return;
    let cancelled = false;
    void (async () => {
      try {
        await refreshDashboard();
        const rows = await getOutstanding(10);
        if (!cancelled) {
          setOutstanding(rows);
          setError(null);
        }
      } catch (cause) {
        if (!cancelled) setError(cause);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [online, refreshDashboard]);

  if (loading) return <Loading label="Loading your numbers…" />;

  if (!summary) {
    return (
      <div className="stack">
        <h1 className="day-title">Overview</h1>
        {online ? (
          <ErrorNotice message={messageFor(error)} />
        ) : (
          <p className="notice" role="status">
            Unavailable offline. Your numbers are worked out on the server, and
            this device has not received them yet.
          </p>
        )}
      </div>
    );
  }

  const money = (minor: number) =>
    formatMoney(minor, summary.currency, summary.currency_exponent);
  const cycle = summary.current_cycle;

  return (
    <div className="stack">
      <header className="day-header">
        <div>
          <h1 className="day-title">Overview</h1>
          <p className="day-sub">{longDate(summary.business_date)}</p>
        </div>
      </header>

      {!online && readAt ? (
        <p className="notice" role="status">
          Offline — showing the figures from {relative(readAt)}. They will update
          when you are back online.
        </p>
      ) : null}
      {online && error ? <ErrorNotice message={messageFor(error)} /> : null}

      <section className="stat-grid" aria-label="Money">
        <Stat
          label="Owed to you"
          value={money(summary.outstanding_minor)}
          hint={`${summary.customers.with_balance_due} customer${
            summary.customers.with_balance_due === 1 ? "" : "s"
          } with a balance`}
          emphasis
        />
        <Stat
          label={cycle ? "Sold this period" : "Sold, all time"}
          value={money(
            (cycle ?? summary.all_time).business_generated_minor,
          )}
          hint={cycle ? periodWords(summary) : "since you started"}
        />
        <Stat
          label={cycle ? "Collected this period" : "Collected, all time"}
          value={money((cycle ?? summary.all_time).collected_minor)}
          hint="payments received, less anything reversed"
        />
        <Stat
          label="Customers"
          value={String(summary.customers.active)}
          hint={
            summary.customers.total === summary.customers.active
              ? "all active"
              : `${summary.customers.total} in total, ${
                  summary.customers.total - summary.customers.active
                } inactive`
          }
        />
      </section>

      {summary.customers.in_credit > 0 ? (
        <p className="empty">
          {summary.customers.in_credit} customer
          {summary.customers.in_credit === 1 ? " is" : "s are"} in credit — they
          have paid ahead.
        </p>
      ) : null}

      <section className="stack" aria-labelledby="owing-heading">
        <h2 id="owing-heading" className="section-title">
          Who owes the most
        </h2>
        {outstanding === null ? (
          <p className="empty">
            {online ? "Loading…" : "Unavailable offline."}
          </p>
        ) : outstanding.items.length === 0 ? (
          <p className="empty">Nobody has an outstanding balance.</p>
        ) : (
          <ul className="list">
            {outstanding.items.map((row) => (
              <li key={row.customer_id} className="list-row">
                <Link to={`/customers/${row.customer_id}`} className="list-main">
                  <span className="list-title">{row.name}</span>
                  <span className="list-sub">
                    {row.code}
                    {row.area ? ` · ${row.area}` : ""}
                  </span>
                </Link>
                <span
                  className={
                    row.outstanding_minor < 0 ? "amount amount-credit" : "amount"
                  }
                >
                  {formatMoney(
                    row.outstanding_minor,
                    outstanding.currency,
                    outstanding.currency_exponent,
                  )}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="stack" aria-labelledby="activity-heading">
        <h2 id="activity-heading" className="section-title">
          Recent payments
        </h2>
        {summary.recent_payments.length === 0 ? (
          <p className="empty">No payments recorded yet.</p>
        ) : (
          <ul className="list">
            {summary.recent_payments.map((payment) => (
              <li key={payment.id} className="list-row">
                <Link
                  to={`/customers/${payment.customer_id}`}
                  className="list-main"
                >
                  <span className="list-title">{payment.customer_name}</span>
                  <span className="list-sub">
                    {shortDate(payment.received_on)} · {methodWords(payment.method)}
                    {payment.status === "VOIDED" ? " · reversed" : ""}
                  </span>
                </Link>
                <span
                  className={
                    payment.status === "VOIDED" ? "amount amount-void" : "amount"
                  }
                >
                  {money(payment.amount_minor)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <nav className="quick-links" aria-label="Go to">
        <Link className="btn btn-quiet" to="/statements">
          Statements
        </Link>
        <Link className="btn btn-quiet" to="/customers">
          Customers
        </Link>
        <Link className="btn btn-quiet" to="/operating-costs">
          Running costs
        </Link>
      </nav>
    </div>
  );
}

function Stat({
  label,
  value,
  hint,
  emphasis,
}: {
  label: string;
  value: string;
  hint?: string;
  emphasis?: boolean;
}) {
  return (
    <div className={emphasis ? "stat stat-emphasis" : "stat"}>
      <span className="stat-label">{label}</span>
      <strong className="stat-value">{value}</strong>
      {hint ? <span className="stat-hint">{hint}</span> : null}
    </div>
  );
}

function periodWords(summary: { open_cycle: { period_start: string; period_end: string } | null }) {
  if (!summary.open_cycle) return "";
  return `${shortDate(summary.open_cycle.period_start)} – ${shortDate(
    summary.open_cycle.period_end,
  )}`;
}

/**
 * Dates are formatted from the server's own ISO strings, never from a `Date`
 * the device constructed for itself — the business date is the tenant's, not the
 * browser's (P0 R4). Parsing the parts avoids the timezone shift `new Date("…")`
 * would introduce.
 */
function parts(iso: string): { y: number; m: number; d: number } | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  if (!match) return null;
  return { y: Number(match[1]), m: Number(match[2]), d: Number(match[3]) };
}

const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

export function longDate(iso: string): string {
  const p = parts(iso);
  if (!p) return iso;
  return `${p.d} ${MONTHS[p.m - 1]} ${p.y}`;
}

export function shortDate(iso: string): string {
  const p = parts(iso);
  if (!p) return iso;
  return `${p.d} ${MONTHS[p.m - 1]?.slice(0, 3)}`;
}

function relative(isoInstant: string): string {
  const then = Date.parse(isoInstant);
  if (Number.isNaN(then)) return "earlier";
  const minutes = Math.round((Date.now() - then) / 60000);
  if (minutes < 1) return "a moment ago";
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  const days = Math.round(hours / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

export function methodWords(method: string): string {
  const words: Record<string, string> = {
    CASH: "Cash",
    BANK_TRANSFER: "Bank transfer",
    OTHER: "Other",
  };
  return words[method] ?? method;
}
