import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";

import { createCustomer, type CustomerDraft } from "@/api/customers";
import type { Customer, OperationResult } from "@/api/types";
import { Loading } from "@/components/Feedback";
import { usePendingOperation } from "@/daily/usePendingOperation";
import { useLocalData } from "@/sync/useLocalData";
import {
  CustomerForm,
  emptyValues,
  minorToMajor,
  toDraft,
  type CustomerFormValues,
} from "./CustomerForm";

/**
 * Add a customer.
 *
 * The quantity and price start from the tenant's own configured defaults, not
 * from numbers chosen here — the whole point of putting them on the tenant row
 * (P0 §4) is that a new customer inherits the business's normal terms without
 * anybody retyping them. They are read from the device's snapshot, so the form
 * renders without a network — but creating a customer is **not** one of V1's
 * offline operations, so the save itself needs the connection and says so.
 */
export function CustomerCreatePage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { settings, loading } = useLocalData();
  const [values, setValues] = useState<CustomerFormValues>(emptyValues);
  const [seeded, setSeeded] = useState(false);

  useEffect(() => {
    if (seeded || !settings) return;
    setValues((current) => ({
      ...current,
      default_quantity: settings.default_quantity,
      unit_price_major: minorToMajor(
        settings.default_unit_price_minor,
        settings.currency_exponent,
      ),
    }));
    setSeeded(true);
  }, [seeded, settings]);

  const operation = usePendingOperation<CustomerDraft, OperationResult<Customer>>(
    (envelope) => createCustomer(envelope),
    (result) => {
      void queryClient.invalidateQueries({ queryKey: ["customers"] });
      navigate(`/customers/${result.entity.id}`, { replace: true });
    },
  );

  if (loading) return <Loading label="Loading…" />;
  if (!settings) {
    return (
      <p className="notice notice-error" role="alert">
        Unavailable offline. This device has not synchronised yet — connect once
        and this form will know the business defaults.
      </p>
    );
  }

  const config = settings;

  function submit() {
    const draft = toDraft(values, config.currency_exponent);
    if (!draft) return;
    void operation.start("customer.create", draft);
  }

  return (
    <div className="stack">
      <h1 className="day-title">Add customer</h1>
      <CustomerForm
        values={values}
        onChange={setValues}
        onSubmit={submit}
        onCancel={() => navigate("/customers")}
        submitLabel="Save customer"
        busy={operation.phase === "sending"}
        error={operation.error}
        currency={config.currency}
        currencyExponent={config.currency_exponent}
        unitLabel={config.unit_label}
        codeEditable
        showStatus={false}
      />
      {operation.phase === "unresolved" ? (
        <div className="notice notice-error" role="alert">
          <span>
            We are not sure this reached the server. Retry sends the same customer —
            it cannot be created twice.
          </span>
          <span className="notice-actions">
            <button className="btn btn-quiet" type="button" onClick={() => void operation.retry()}>
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
