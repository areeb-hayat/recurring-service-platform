import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { createOperation } from "@/api/operation";
import { messageFor } from "@/api/errors";
import {
  listCustomerHistory,
  listCustomerPayments,
  listCustomerStatements,
  voidPayment,
} from "@/api/finance";
import type { CustomerDetail, Payment, ServiceRecord, Statement } from "@/api/types";
import { EmptyState } from "@/components/Feedback";
import { longDate, methodWords, shortDate } from "@/dashboard/DashboardPage";
import { formatMoney } from "@/lib/money";
import { StatementDetail } from "@/statements/StatementsPage";
import { useSync } from "@/sync/SyncProvider";
import { useLocalData } from "@/sync/useLocalData";

/**
 * One customer's money: what they owe, what they were billed, what they paid.
 *
 * Three server-authoritative lists and no client accounting engine. The balance
 * at the top is `outstanding_minor` from `GET /customers/{id}`; the statements
 * are immutable issued documents; the payment list includes voided rows, because
 * a reversal is exactly the movement that explains a balance somebody is
 * querying (AUD-8), and hiding it would leave the number unaccountable.
 *
 * Online, these come from the API. Offline, payments and statements come from
 * the P5 snapshot — the same rows, put there by the change feed. Service history
 * has no offline source (the snapshot keeps only the current round's records),
 * so it says so rather than showing a partial list as if it were complete.
 */
export function CustomerFinancials({ customer }: { customer: CustomerDetail }) {
  const { online } = useSync();
  const local = useLocalData();
  const queryClient = useQueryClient();
  const [openStatement, setOpenStatement] = useState<string | null>(null);
  const [voiding, setVoiding] = useState<Payment | null>(null);

  const statementsQuery = useQuery({
    queryKey: ["statements", customer.id],
    queryFn: () => listCustomerStatements(customer.id),
    enabled: online,
  });
  const paymentsQuery = useQuery({
    queryKey: ["payments", customer.id],
    queryFn: () => listCustomerPayments(customer.id),
    enabled: online,
  });
  const historyQuery = useQuery({
    queryKey: ["history", customer.id],
    queryFn: () => listCustomerHistory(customer.id),
    enabled: online,
  });

  // Offline, fall back to what synchronised. Never to a computed guess.
  const statements: Statement[] =
    statementsQuery.data?.items ??
    local.statements.filter((s) => s.customer_id === customer.id);
  const payments: Payment[] =
    paymentsQuery.data?.items ??
    local.payments.filter((p) => p.customer_id === customer.id);
  const history: ServiceRecord[] = historyQuery.data?.items ?? [];

  const money = (minor: number) =>
    formatMoney(minor, customer.currency, customer.currency_exponent);

  const open = statements.find((s) => s.id === openStatement) ?? null;

  return (
    <div className="stack">
      <section className="stack" aria-labelledby="pay-heading">
        <h2 id="pay-heading" className="section-title">
          Payments
        </h2>
        {online ? (
          <Link className="btn btn-primary" to={`/customers/${customer.id}/pay`}>
            Record a payment
          </Link>
        ) : (
          <p className="notice" role="status">
            Recording a payment needs a connection — it is saved on the server,
            never on this device.
          </p>
        )}

        {payments.length === 0 ? (
          <EmptyState>No payments recorded for this customer.</EmptyState>
        ) : (
          <ul className="list">
            {payments
              .slice()
              .reverse()
              .map((payment) => (
                <li key={payment.id} className="list-row">
                  <div className="list-main">
                    <span className="list-title">
                      {money(payment.amount_minor)}
                      {payment.status === "VOIDED" ? " · reversed" : ""}
                    </span>
                    <span className="list-sub">
                      {longDate(payment.received_on)} ·{" "}
                      {methodWords(payment.method)}
                      {payment.reference ? ` · ${payment.reference}` : ""}
                      {payment.voided_reason ? ` — ${payment.voided_reason}` : ""}
                    </span>
                  </div>
                  {payment.status === "RECORDED" && online ? (
                    <button
                      className="btn btn-quiet"
                      type="button"
                      onClick={() => setVoiding(payment)}
                    >
                      Reverse
                    </button>
                  ) : null}
                </li>
              ))}
          </ul>
        )}
      </section>

      {voiding ? (
        <VoidPaymentDialog
          payment={voiding}
          currency={customer.currency}
          exponent={customer.currency_exponent}
          onDone={() => {
            setVoiding(null);
            void queryClient.invalidateQueries({ queryKey: ["payments", customer.id] });
            void queryClient.invalidateQueries({ queryKey: ["customer", customer.id] });
          }}
          onCancel={() => setVoiding(null)}
        />
      ) : null}

      <section className="stack" aria-labelledby="statements-heading">
        <h2 id="statements-heading" className="section-title">
          Statements
        </h2>
        {statements.length === 0 ? (
          <EmptyState>
            No statements yet — one is issued when a billing period is closed.
          </EmptyState>
        ) : (
          <ul className="list">
            {statements.map((statement) => (
              <li key={statement.id} className="list-row">
                <button
                  type="button"
                  className="list-main list-button"
                  aria-expanded={openStatement === statement.id}
                  onClick={() =>
                    setOpenStatement(
                      openStatement === statement.id ? null : statement.id,
                    )
                  }
                >
                  <span className="list-title">
                    {statement.issued_at
                      ? longDate(statement.issued_at)
                      : "Issued statement"}
                  </span>
                  <span className="list-sub">
                    {statement.service_days} day
                    {statement.service_days === 1 ? "" : "s"} ·{" "}
                    {statement.total_quantity} {statement.unit_label}
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
            ))}
          </ul>
        )}
        {open ? (
          <StatementDetail
            statement={open}
            customerName={customer.name}
            onClose={() => setOpenStatement(null)}
          />
        ) : null}
      </section>

      <section className="stack" aria-labelledby="history-heading">
        <h2 id="history-heading" className="section-title">
          Deliveries
        </h2>
        {!online ? (
          <p className="notice" role="status">
            Past deliveries are unavailable offline.
          </p>
        ) : history.length === 0 ? (
          <EmptyState>Nothing recorded for this customer yet.</EmptyState>
        ) : (
          <ul className="list">
            {history.map((record) => (
              <li key={record.id} className="list-row">
                <div className="list-main">
                  <span className="list-title">
                    {shortDate(record.service_date)} ·{" "}
                    {record.kind === "SKIP"
                      ? "Skipped"
                      : `${record.quantity} ${record.unit_label}`}
                  </span>
                  <span className="list-sub">{statusWords(record)}</span>
                </div>
                <span
                  className={
                    record.status === "ACTIVE" ? "amount" : "amount amount-void"
                  }
                >
                  {money(record.charge_minor)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

/**
 * A void needs a reason, so the reason is the form (AUD-6).
 *
 * The payment row is never deleted and its amount is never edited: the server
 * marks it VOIDED and appends a compensating ledger entry, which is what returns
 * the balance. Nothing here calculates the resulting balance — the customer is
 * simply re-read afterwards.
 */
function VoidPaymentDialog({
  payment,
  currency,
  exponent,
  onDone,
  onCancel,
}: {
  payment: Payment;
  currency: string;
  exponent: number;
  onDone: () => void;
  onCancel: () => void;
}) {
  const [reason, setReason] = useState("");
  const { syncNow } = useSync();

  const mutation = useMutation({
    mutationFn: (text: string) =>
      // Generated once, here, at the moment of intent — the same rule every
      // other write follows.
      voidPayment(payment.id, createOperation("payment.void", { reason: text })),
    onSuccess: () => {
      void syncNow();
      onDone();
    },
  });

  const ready = reason.trim().length > 0;

  return (
    <section className="card stack" aria-label="Reverse this payment">
      <h3 className="section-title">
        Reverse {formatMoney(payment.amount_minor, currency, exponent)}
      </h3>
      <p className="empty">
        The payment stays on the record, marked as reversed, and the balance goes
        back to what it was. Nothing is deleted.
      </p>
      {mutation.isError ? (
        <p className="notice notice-error" role="alert">
          {messageFor(mutation.error)}
        </p>
      ) : null}
      <div className="field">
        <label htmlFor="void-reason">Why is it being reversed?</label>
        <input
          id="void-reason"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder="Entered twice, wrong customer…"
        />
      </div>
      <div className="form-actions">
        <button
          className="btn btn-primary"
          type="button"
          disabled={!ready || mutation.isPending}
          onClick={() => mutation.mutate(reason.trim())}
        >
          {mutation.isPending ? "Reversing…" : "Reverse payment"}
        </button>
        <button className="btn btn-quiet" type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </section>
  );
}

/** The server's own record status, said in words (AUD-8). */
function statusWords(record: ServiceRecord): string {
  if (record.status === "SUPERSEDED") return `Corrected${reasonPart(record)}`;
  if (record.status === "VOIDED") return `Cancelled${reasonPart(record)}`;
  if (record.corrects_id) return `Correction${reasonPart(record)}`;
  return record.kind === "SKIP" ? "No delivery" : "Delivered";
}

function reasonPart(record: ServiceRecord): string {
  return record.reason ? ` — ${record.reason}` : "";
}
