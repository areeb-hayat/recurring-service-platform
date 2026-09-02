import { useEffect, useId, useState } from "react";

import { messageFor } from "@/api/errors";
import { recordService, type ServiceIntent } from "@/api/service";
import type { OperationResult, ServiceRecord } from "@/api/types";
import { QuantityStepper } from "@/components/QuantityStepper";
import { isValidQuantity, isZeroQuantity } from "@/lib/decimal";
import type { RegisterEntry } from "./useRegister";
import { usePendingOperation } from "./usePendingOperation";

/**
 * One customer, one day, two decisions.
 *
 * CONFIRM sends what was delivered. SKIP TODAY records that nothing was — a
 * `SKIP` is a real record, not an absence, which is why it is a button and not
 * simply moving on. Both go through the same endpoint and the same envelope
 * rule; the only difference is `kind` and whether a quantity travels.
 *
 * No amount is calculated here. The card shows the customer's current unit price
 * as context, and the charge appears only after the server has answered with it.
 */
export function ServiceCard({
  entry,
  position,
  total,
  onRecorded,
  onNext,
}: {
  entry: RegisterEntry;
  position: number;
  total: number;
  onRecorded: () => void;
  onNext: () => void;
}) {
  const { customer } = entry;
  const [quantity, setQuantity] = useState(customer.default_quantity);
  const [saved, setSaved] = useState<ServiceRecord | null>(null);
  const hintId = useId();

  // A different customer is a different intent: reset the draft and any result.
  useEffect(() => {
    setQuantity(customer.default_quantity);
    setSaved(null);
  }, [customer.id, customer.default_quantity]);

  const operation = usePendingOperation<ServiceIntent, OperationResult<ServiceRecord>>(
    (envelope) => recordService(envelope),
    (result) => {
      setSaved(result.entity);
      onRecorded();
    },
  );

  const busy = operation.phase === "sending";
  const unresolved = operation.phase === "unresolved";
  const quantityOk = isValidQuantity(quantity);
  const canConfirm = quantityOk && !isZeroQuantity(quantity) && !busy && !unresolved;

  function confirm() {
    if (!canConfirm) return;
    // `service_date` is omitted on purpose: the server resolves the tenant's
    // today, and it is the only party entitled to.
    void operation.start("service.record", {
      customer_id: customer.id,
      kind: "SERVICE",
      quantity,
      input_method: "BUTTON",
    });
  }

  function skip() {
    if (busy || unresolved) return;
    void operation.start("service.skip", {
      customer_id: customer.id,
      kind: "SKIP",
      input_method: "BUTTON",
    });
  }

  if (saved) {
    return (
      <section className="card stack" aria-labelledby="register-customer">
        <p className="card-position">
          {position} of {total}
        </p>
        <h2 id="register-customer" className="customer-name">
          {customer.name}
        </h2>
        <p className="notice notice-success" role="status">
          {saved.kind === "SKIP"
            ? "Skipped today."
            : `Recorded ${saved.quantity} ${saved.unit_label}.`}
        </p>
        <button className="btn btn-primary btn-lg" type="button" onClick={onNext} autoFocus>
          Next customer
        </button>
      </section>
    );
  }

  return (
    <section className="card stack" aria-labelledby="register-customer">
      <p className="card-position">
        {position} of {total}
      </p>
      <h2 id="register-customer" className="customer-name">
        {customer.name}
      </h2>
      <p className="customer-meta">
        {customer.code}
        {customer.area ? ` · ${customer.area}` : ""}
      </p>

      <QuantityStepper
        value={quantity}
        onChange={setQuantity}
        unitLabel={customer.unit_label}
        disabled={busy || unresolved}
        describedBy={hintId}
      />
      <p id={hintId} className={quantityOk ? "hint" : "hint hint-error"}>
        {quantityOk
          ? `Up to 3 decimal places. Usually ${customer.default_quantity}.`
          : "Enter a number with up to 3 decimal places, for example 2 or 1.5"}
      </p>

      {operation.error ? (
        <div className="notice notice-error" role="alert">
          <span>{messageFor(operation.error)}</span>
          {unresolved ? (
            <span className="notice-actions">
              <button className="btn btn-quiet" type="button" onClick={() => void operation.retry()}>
                Retry
              </button>
              <button className="btn btn-quiet" type="button" onClick={operation.discard}>
                Discard
              </button>
            </span>
          ) : null}
        </div>
      ) : null}

      {unresolved ? (
        <p className="hint" role="status">
          We are not sure this reached the server. Retry sends the same entry — it
          cannot be recorded twice.
        </p>
      ) : null}

      <button
        className="btn btn-primary btn-xl"
        type="button"
        onClick={confirm}
        disabled={!canConfirm}
      >
        {busy ? "Saving…" : "Confirm"}
      </button>
      <button
        className="btn btn-secondary btn-lg"
        type="button"
        onClick={skip}
        disabled={busy || unresolved}
      >
        Skip today
      </button>
      <button className="btn btn-quiet" type="button" onClick={onNext} disabled={busy}>
        Leave for later
      </button>
    </section>
  );
}
