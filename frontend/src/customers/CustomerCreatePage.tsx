import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";

import { createCustomer, type CustomerDraft } from "@/api/customers";
import type { Customer, OperationResult } from "@/api/types";
import { ErrorNotice, Loading } from "@/components/Feedback";
import { messageFor } from "@/api/errors";
import { usePendingOperation } from "@/daily/usePendingOperation";
import { useTenantSettingsQuery } from "@/daily/useRegister";
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
 * anybody retyping them.
 */
export function CustomerCreatePage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const settings = useTenantSettingsQuery();
  const [values, setValues] = useState<CustomerFormValues>(emptyValues);
  const [seeded, setSeeded] = useState(false);

  useEffect(() => {
    if (seeded || !settings.data) return;
    setValues((current) => ({
      ...current,
      default_quantity: settings.data.default_quantity,
      unit_price_major: minorToMajor(
        settings.data.default_unit_price_minor,
        settings.data.currency_exponent,
      ),
    }));
    setSeeded(true);
  }, [seeded, settings.data]);

  const operation = usePendingOperation<CustomerDraft, OperationResult<Customer>>(
    (envelope) => createCustomer(envelope),
    (result) => {
      void queryClient.invalidateQueries({ queryKey: ["customers"] });
      navigate(`/customers/${result.entity.id}`, { replace: true });
    },
  );

  if (settings.isPending) return <Loading label="Loading…" />;
  if (!settings.data) {
    return (
      <ErrorNotice message={messageFor(settings.error)} onRetry={() => void settings.refetch()} />
    );
  }

  const config = settings.data;

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
