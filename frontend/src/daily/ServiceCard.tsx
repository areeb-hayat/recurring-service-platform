import { useEffect, useId, useState } from "react";

import { createOperation } from "@/api/operation";
import type { ServiceIntent } from "@/api/service";
import type { TenantSettings } from "@/api/types";
import { QuantityStepper } from "@/components/QuantityStepper";
import { isValidQuantity, isZeroQuantity } from "@/lib/decimal";
import { useSync } from "@/sync/SyncProvider";
import type { RegisterEntry } from "./useRegister";

/**
 * One customer, one day, two decisions — and one write path.
 *
 * CONFIRM and SKIP do the same three things whether or not there is a network:
 * mint the `operation_id` once, write the envelope to the outbox, and let the
 * engine deliver it. There is no "online save" branch and no "offline save"
 * branch, which is why an interrupted online save is exactly as safe as an
 * offline one.
 *
 * **What the card claims.** Once queued it says *"Saved on this device — waiting
 * to sync"*, never "Recorded". "Recorded" is a statement about the server, and
 * the server has not spoken yet. It becomes "Recorded" when the operation comes
 * back APPLIED (or DUPLICATE) and the record arrives in the snapshot.
 *
 * No amount is calculated here. The unit price is shown as context; the charge
 * appears only once the server has answered with it.
 */
export function ServiceCard({
  entry,
  settings,
  position,
  total,
  onNext,
  onLeaveForLater,
  onQueued,
}: {
  entry: RegisterEntry;
  settings: TenantSettings;
  position: number;
  total: number;
  /** Move on from a customer who has just been dealt with. */
  onNext: () => void;
  /** Move on from a customer who has *not* been dealt with. */
  onLeaveForLater: () => void;
  /** Keeps the card on this customer once something has been saved for them. */
  onQueued: (customerId: string) => void;
}) {
  const { customer } = entry;
  const { enqueue } = useSync();
  const [quantity, setQuantity] = useState(customer.default_quantity);
  const [saving, setSaving] = useState(false);
  const [failed, setFailed] = useState(false);
  const hintId = useId();

  // A different customer is a different intent: reset the draft.
  useEffect(() => {
    setQuantity(customer.default_quantity);
    setFailed(false);
  }, [customer.id, customer.default_quantity]);

  const quantityOk = isValidQuantity(quantity);
  const canConfirm = quantityOk && !isZeroQuantity(quantity) && !saving;

  async function queue(intent: ServiceIntent) {
    setSaving(true);
    setFailed(false);
    try {
      // Generated once, here, at the moment of intent — and never again, however
      // many times it is later retried (SYN-1).
      const envelope = createOperation(
        intent.kind === "SKIP" ? "service.skip" : "service.record",
        intent,
      );
      await enqueue(envelope, {
        customer_id: customer.id,
        customer_name: customer.name,
        service_date: settings.business_date,
        kind: intent.kind,
        quantity: intent.quantity ?? null,
        unit_label: customer.unit_label,
      });
      // Stay on this customer showing what was saved. Advancing automatically
      // would put one entry's confirmation over the next customer's name, which
      // is how the wrong person gets recorded twice.
      onQueued(customer.id);
    } catch {
      // The device itself refused to store it (private browsing, no quota).
      // Saying nothing here would lose the entry silently, which is the one
      // outcome the whole package exists to prevent.
      setFailed(true);
    } finally {
      setSaving(false);
    }
  }

  function confirm() {
    if (!canConfirm) return;
    void queue({
      customer_id: customer.id,
      kind: "SERVICE",
      quantity,
      // The business date the *server* last stated, carried with the intent so a
      // round recorded on Saturday and synchronised on Sunday stays Saturday's.
      service_date: settings.business_date,
      input_method: "BUTTON",
    });
  }

  function skip() {
    if (saving) return;
    void queue({
      customer_id: customer.id,
      kind: "SKIP",
      service_date: settings.business_date,
      input_method: "BUTTON",
    });
  }

  if (entry.record) {
    return (
      <section className="card stack" aria-labelledby="register-customer">
        <p className="card-position">
          {position} of {total}
        </p>
        <h2 id="register-customer" className="customer-name">
          {customer.name}
        </h2>
        <p className="notice notice-success" role="status">
          {entry.record.kind === "SKIP"
            ? "Skipped today."
            : `Recorded ${entry.record.quantity} ${entry.record.unit_label}.`}
        </p>
        <button className="btn btn-primary btn-lg" type="button" onClick={onNext} autoFocus>
          Next customer
        </button>
      </section>
    );
  }

  if (entry.queued) {
    const { kind, quantity: queuedQuantity, unit_label } = entry.queued.context;
    return (
      <section className="card stack" aria-labelledby="register-customer">
        <p className="card-position">
          {position} of {total}
        </p>
        <h2 id="register-customer" className="customer-name">
          {customer.name}
        </h2>
        <p className="notice notice-pending" role="status">
          {kind === "SKIP"
            ? "Skip saved on this device — waiting to sync."
            : `${queuedQuantity} ${unit_label} saved on this device — waiting to sync.`}
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
        disabled={saving}
        describedBy={hintId}
      />
      <p id={hintId} className={quantityOk ? "hint" : "hint hint-error"}>
        {quantityOk
          ? `Up to 3 decimal places. Usually ${customer.default_quantity}.`
          : "Enter a number with up to 3 decimal places, for example 2 or 1.5"}
      </p>

      {failed ? (
        <div className="notice notice-error" role="alert">
          <span>
            This device would not save the entry. Nothing was recorded — check
            that the browser is allowed to store data, then try again.
          </span>
        </div>
      ) : null}

      <button
        className="btn btn-primary btn-xl"
        type="button"
        onClick={confirm}
        disabled={!canConfirm}
      >
        {saving ? "Saving…" : "Confirm"}
      </button>
      <button
        className="btn btn-secondary btn-lg"
        type="button"
        onClick={skip}
        disabled={saving}
      >
        Skip today
      </button>
      <button
        className="btn btn-quiet"
        type="button"
        onClick={onLeaveForLater}
        disabled={saving}
      >
        Leave for later
      </button>
    </section>
  );
}
