import { useId, useState, type FormEvent } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { getCustomer } from "@/api/customers";
import { messageFor, fieldErrorFor } from "@/api/errors";
import { recordPayment, type PaymentDraft } from "@/api/finance";
import type { OperationResult, Payment, PaymentMethod } from "@/api/types";
import { ErrorNotice, Loading, SuccessNotice } from "@/components/Feedback";
import { usePendingOperation } from "@/daily/usePendingOperation";
import { formatMoney, majorToMinor } from "@/lib/money";
import { useSync } from "@/sync/SyncProvider";

const METHODS: { value: PaymentMethod; label: string }[] = [
  { value: "CASH", label: "Cash" },
  { value: "BANK_TRANSFER", label: "Bank transfer" },
  { value: "OTHER", label: "Other" },
];

/**
 * Record a payment the owner has received.
 *
 * **Online only, and it says so** (PAY-8). Payments are not queued into the
 * outbox: the offline write guarantee in V1 is CONFIRM and SKIP, and pretending
 * otherwise would mean a device holding money movements the ledger has never
 * seen. When there is no connection the form is disabled with a plain sentence,
 * not a silently failing button.
 *
 * **The `operation_id` is generated once**, at the moment the person presses
 * Record, and a retry resends the identical envelope — so a lost response cannot
 * become a second payment. That is also why the amount is locked while a
 * transport failure is unresolved: editing it and retrying would send a
 * different request under the same id, which the server correctly refuses.
 *
 * **The resulting balance is not computed here.** After the server accepts, the
 * customer is re-read and the new outstanding is whatever the server says it is
 * (FIN-4). Any amount is allowed, including more than is owed — an overpayment
 * is a credit, not an error (FIN-10) — and the form never clamps what somebody
 * says they were handed.
 */
export function RecordPaymentPage() {
  const { customerId = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { online, syncNow } = useSync();
  const formId = useId();

  const [amount, setAmount] = useState("");
  const [method, setMethod] = useState<PaymentMethod>("CASH");
  const [reference, setReference] = useState("");
  const [note, setNote] = useState("");
  const [touched, setTouched] = useState(false);
  const [saved, setSaved] = useState<Payment | null>(null);

  const query = useQuery({
    queryKey: ["customer", customerId],
    queryFn: () => getCustomer(customerId),
  });

  const operation = usePendingOperation<PaymentDraft, OperationResult<Payment>>(
    (envelope) => recordPayment(envelope),
    (result) => {
      setSaved(result.entity);
      setAmount("");
      setReference("");
      setNote("");
      setTouched(false);
      void queryClient.invalidateQueries({ queryKey: ["customer", customerId] });
      void queryClient.invalidateQueries({ queryKey: ["payments", customerId] });
      // Bring the device's own copy of the payment into the snapshot, so the
      // customer page reads the same row offline.
      void syncNow();
    },
  );

  if (query.isPending) return <Loading label="Loading customer…" />;
  if (!query.data) {
    return (
      <ErrorNotice
        message={messageFor(query.error)}
        onRetry={() => void query.refetch()}
      />
    );
  }

  const customer = query.data;
  const exponent = customer.currency_exponent;
  const minor = majorToMinor(amount, exponent);
  const amountOk = minor !== null && minor > 0;
  const busy = operation.phase === "sending";
  const locked = operation.phase === "unresolved";

  function submit(event: FormEvent) {
    event.preventDefault();
    setTouched(true);
    if (!amountOk || busy || locked || !online) return;
    setSaved(null);
    void operation.start("payment.record", {
      customer_id: customerId,
      amount_minor: minor,
      method,
      reference: reference.trim() || null,
      note: note.trim() || null,
    });
  }

  const overpaying =
    amountOk && minor > customer.outstanding_minor && customer.outstanding_minor > 0;

  return (
    <div className="stack">
      <header className="day-header">
        <div>
          <h1 className="day-title">Record a payment</h1>
          <p className="day-sub">
            {customer.name} · owes{" "}
            {formatMoney(customer.outstanding_minor, customer.currency, exponent)}
          </p>
        </div>
      </header>

      {!online ? (
        <p className="notice notice-error" role="alert">
          You are offline. Payments are recorded on the server and cannot be
          saved on this device — the amount would be money the ledger has not
          seen. Try again once you have a connection.
        </p>
      ) : null}

      {saved ? (
        <SuccessNotice>
          Recorded {formatMoney(saved.amount_minor, saved.currency, saved.currency_exponent)}
          . The balance shown above is the server's.
        </SuccessNotice>
      ) : null}

      <form className="card stack" onSubmit={submit} noValidate>
        {operation.error && !operation.error.isRetryable ? (
          <p className="notice notice-error" role="alert">
            {messageFor(operation.error)}
          </p>
        ) : null}

        <div className="field">
          <label htmlFor={formId + "-amount"}>
            Amount received ({customer.currency})
          </label>
          <input
            id={formId + "-amount"}
            inputMode="decimal"
            value={amount}
            disabled={locked || !online}
            onChange={(e) => setAmount(e.target.value)}
            aria-invalid={touched && !amountOk}
            className="amount-input"
          />
          {touched && !amountOk ? (
            <p className="field-message" role="alert">
              Enter an amount greater than zero, with up to {exponent} decimal
              places.
            </p>
          ) : (
            <p className="field-message">
              {fieldErrorFor(operation.error, "amount_minor") ??
                "Part of the balance is fine. So is more than is owed."}
            </p>
          )}
        </div>

        <fieldset className="field" disabled={locked || !online}>
          <legend>How it was paid</legend>
          <div className="choice-row">
            {METHODS.map((entry) => (
              <label key={entry.value} className="choice">
                <input
                  type="radio"
                  name="method"
                  value={entry.value}
                  checked={method === entry.value}
                  onChange={() => setMethod(entry.value)}
                />
                <span>{entry.label}</span>
              </label>
            ))}
          </div>
        </fieldset>

        <div className="field">
          <label htmlFor={formId + "-reference"}>Reference (optional)</label>
          <input
            id={formId + "-reference"}
            value={reference}
            disabled={locked || !online}
            onChange={(e) => setReference(e.target.value)}
            placeholder="Slip number, transfer id…"
          />
        </div>

        <div className="field">
          <label htmlFor={formId + "-note"}>Note (optional)</label>
          <textarea
            id={formId + "-note"}
            value={note}
            disabled={locked || !online}
            onChange={(e) => setNote(e.target.value)}
            rows={2}
          />
        </div>

        {overpaying ? (
          <p className="notice" role="status">
            That is more than the balance. It will be kept as credit against
            future deliveries.
          </p>
        ) : null}

        <div className="form-actions">
          <button
            className="btn btn-primary"
            type="submit"
            disabled={busy || locked || !online}
          >
            {busy ? "Recording…" : "Record payment"}
          </button>
          <button
            className="btn btn-quiet"
            type="button"
            onClick={() => navigate(`/customers/${customerId}`)}
          >
            Back to customer
          </button>
        </div>
      </form>

      {locked ? (
        <div className="notice notice-error" role="alert">
          <span>
            We are not sure this reached the server. Retry sends the same
            payment — it cannot be recorded twice.
          </span>
          <span className="notice-actions">
            <button
              className="btn btn-quiet"
              type="button"
              onClick={() => void operation.retry()}
            >
              Retry
            </button>
            <button className="btn btn-quiet" type="button" onClick={operation.discard}>
              Discard
            </button>
          </span>
        </div>
      ) : null}
    </div>
  );
}
