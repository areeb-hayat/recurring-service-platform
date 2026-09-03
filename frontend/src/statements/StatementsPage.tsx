import { useMemo, useState } from "react";

import type { Customer, Statement } from "@/api/types";
import { EmptyState, Loading } from "@/components/Feedback";
import { formatMoney } from "@/lib/money";
import { useLocalData } from "@/sync/useLocalData";
import { longDate } from "@/dashboard/DashboardPage";

/**
 * Issued statements — the list, and one opened up.
 *
 * **A statement is immutable from the instant it is issued** (FIN-8), so there
 * is no edit control, no delete and no "recalculate" on this screen. There is
 * also no "issue statement" button: a statement is only sound once its cycle can
 * receive no further entries, so issuing happens as part of closing a cycle and
 * P0 §15 exposes no route that would let it happen sooner. Nothing here invents
 * an automatic month close either.
 *
 * **Every figure is the server's.** The six columns arrive already split by
 * origin — a service correction and a payment reversal are different lines even
 * though both are ADJUSTMENT rows in the ledger — and the closing balance is
 * read, never re-added. The screen's only arithmetic-looking act is *showing*
 * the identity so a person can follow it.
 *
 * Read from the P5 snapshot, so a statement that has synced is readable offline.
 */
export function StatementsPage() {
  const { settings, customers, statements, loading, unavailable } = useLocalData();
  const [openId, setOpenId] = useState<string | null>(null);
  const [query, setQuery] = useState("");

  const byId = useMemo(() => {
    const map = new Map<string, Customer>();
    for (const c of customers) map.set(c.id, c);
    return map;
  }, [customers]);

  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const sorted = [...statements].sort(
      (a, b) => b.row_version - a.row_version,
    );
    if (!needle) return sorted;
    return sorted.filter((s) => {
      const customer = byId.get(s.customer_id);
      return (
        customer?.name.toLowerCase().includes(needle) ||
        customer?.code.toLowerCase().includes(needle)
      );
    });
  }, [statements, byId, query]);

  if (loading) return <Loading label="Loading statements…" />;

  if (unavailable) {
    return (
      <div className="stack">
        <h1 className="day-title">Statements</h1>
        <p className="notice" role="status">
          Unavailable offline. Statements reach this device when it synchronises.
        </p>
      </div>
    );
  }

  const open = rows.find((s) => s.id === openId) ?? null;

  return (
    <div className="stack">
      <header className="day-header">
        <div>
          <h1 className="day-title">Statements</h1>
          <p className="day-sub">
            Issued bills. Once issued, a statement never changes.
          </p>
        </div>
      </header>

      {statements.length === 0 ? (
        <EmptyState>
          No statements yet. One is issued for each customer when you close a
          billing period.
        </EmptyState>
      ) : (
        <>
          <div className="field">
            <label htmlFor="statement-search">Find a customer</label>
            <input
              id="statement-search"
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Name or code"
            />
          </div>

          {rows.length === 0 ? (
            <EmptyState>No statements match that.</EmptyState>
          ) : (
            <ul className="list">
              {rows.map((statement) => {
                const customer = byId.get(statement.customer_id);
                return (
                  <li key={statement.id} className="list-row">
                    <button
                      type="button"
                      className="list-main list-button"
                      aria-expanded={openId === statement.id}
                      onClick={() =>
                        setOpenId(openId === statement.id ? null : statement.id)
                      }
                    >
                      <span className="list-title">
                        {customer?.name ?? "Customer"}
                      </span>
                      <span className="list-sub">
                        {statement.issued_at
                          ? `Issued ${longDate(statement.issued_at)}`
                          : "Issued"}
                        {customer ? ` · ${customer.code}` : ""}
                      </span>
                    </button>
                    <span className="amount">
                      {formatMoney(
                        statement.closing_balance_minor,
                        statement.currency,
                        statement.currency_exponent,
                      )}
                    </span>
                  </li>
                );
              })}
            </ul>
          )}
        </>
      )}

      {open ? (
        <StatementDetail
          statement={open}
          customerName={byId.get(open.customer_id)?.name ?? "Customer"}
          onClose={() => setOpenId(null)}
        />
      ) : null}

      {settings ? null : null}
    </div>
  );
}

/**
 * One statement, in the order a person reads a bill.
 *
 * The wording avoids accounting vocabulary on purpose: "brought forward",
 * "charges", "money in" rather than debits and credits. The identity
 *
 *     opening + charges + service adjustments − payments + reversals = closing
 *
 * is laid out line by line so it can be followed, but every one of those six
 * numbers is printed as the server sent it.
 */
export function StatementDetail({
  statement,
  customerName,
  onClose,
}: {
  statement: Statement;
  customerName: string;
  onClose?: () => void;
}) {
  const money = (minor: number) =>
    formatMoney(minor, statement.currency, statement.currency_exponent);

  return (
    <section className="card stack statement" aria-label={`Statement for ${customerName}`}>
      <header className="statement-head">
        <div>
          <h2 className="section-title">{customerName}</h2>
          <p className="day-sub">
            {statement.issued_at ? `Issued ${longDate(statement.issued_at)}` : "Issued"}
            {" · "}
            {statement.service_days} day{statement.service_days === 1 ? "" : "s"}
            {" · "}
            {statement.total_quantity} {statement.unit_label}
          </p>
        </div>
        {onClose ? (
          <button className="btn btn-quiet" type="button" onClick={onClose}>
            Close
          </button>
        ) : null}
      </header>

      <dl className="detail">
        <Line label="Brought forward" value={money(statement.opening_balance_minor)} />
        <Line label="This period's deliveries" value={money(statement.charges_minor)} />
        {statement.service_adjustments_minor !== 0 ? (
          <Line
            label="Corrections to deliveries"
            value={money(statement.service_adjustments_minor)}
          />
        ) : null}
        <Line label="Money received" value={`− ${money(statement.payments_minor)}`} />
        {statement.payment_reversals_minor !== 0 ? (
          <Line
            label="Payments reversed"
            value={money(statement.payment_reversals_minor)}
          />
        ) : null}
        <Line
          label="Balance at close"
          value={money(statement.closing_balance_minor)}
          strong
        />
      </dl>

      <p className="empty">
        This statement is a record of what was billed. It cannot be changed —
        anything that needs correcting appears on the next one.
      </p>
    </section>
  );
}

function Line({
  label,
  value,
  strong,
}: {
  label: string;
  value: string;
  strong?: boolean;
}) {
  return (
    <div className={strong ? "detail-row detail-total" : "detail-row"}>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}
