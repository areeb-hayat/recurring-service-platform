import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { getCustomer, updateCustomer, type CustomerPatch } from "@/api/customers";
import { messageFor } from "@/api/errors";
import type { Customer, OperationResult } from "@/api/types";
import { ErrorNotice, Loading } from "@/components/Feedback";
import { formatMoney } from "@/lib/money";
import { usePendingOperation } from "@/daily/usePendingOperation";
import { CustomerAliases } from "./CustomerAliases";
import { CustomerFinancials } from "./CustomerFinancials";
import {
  CustomerForm,
  majorToMinor,
  valuesFromCustomer,
  type CustomerFormValues,
} from "./CustomerForm";

/**
 * One customer: what the server knows, and a way to change it.
 *
 * The outstanding balance and payment status are printed exactly as the server
 * derived them (FIN-4 / FIN-11). Nothing on this page adds anything up, and
 * there is no delete: accepted financial history has no hard-delete path
 * (AUD-1), so a customer who has left is marked Inactive and stays.
 *
 * P6 added the financial view below the details — payments, issued statements
 * and delivery history, each a server-authoritative list. The balance at the top
 * and the movements underneath come from the same ledger, read twice by the
 * server, never reconciled by the client.
 *
 * P7 added the reminder history to that view, and P8 adds the names this
 * customer is actually called: an owner records "Ahmed bhai" here, and from then
 * on searching for it finds him. That section is customer identity, so it sits
 * with the details rather than with the money.
 */
export function CustomerDetailPage() {
  const { customerId = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [values, setValues] = useState<CustomerFormValues | null>(null);

  const query = useQuery({
    queryKey: ["customer", customerId],
    queryFn: () => getCustomer(customerId),
  });

  const operation = usePendingOperation<CustomerPatch, OperationResult<Customer>>(
    (envelope) => updateCustomer(customerId, envelope),
    () => {
      setEditing(false);
      setValues(null);
      void queryClient.invalidateQueries({ queryKey: ["customer", customerId] });
      void queryClient.invalidateQueries({ queryKey: ["customers"] });
    },
  );

  if (query.isPending) return <Loading label="Loading customer…" />;
  if (!query.data) {
    return <ErrorNotice message={messageFor(query.error)} onRetry={() => void query.refetch()} />;
  }

  const customer = query.data;

  function beginEdit() {
    setValues(valuesFromCustomer(customer));
    setEditing(true);
  }

  function save() {
    if (!values) return;
    const minor = majorToMinor(values.unit_price_major, customer.currency_exponent);
    if (minor === null) return;
    void operation.start("customer.update", {
      name: values.name.trim(),
      phone_e164: values.phone_e164.trim() || null,
      whatsapp_e164: values.whatsapp_e164.trim() || null,
      address: values.address.trim() || null,
      area: values.area.trim() || null,
      default_quantity: values.default_quantity.trim(),
      unit_price_minor: minor,
      status: values.status,
      // Optimistic concurrency: the server answers ROW_VERSION_CONFLICT rather
      // than letting this write silently overwrite someone else's.
      expected_row_version: customer.row_version,
    });
  }

  if (editing && values) {
    return (
      <div className="stack">
        <h1 className="day-title">Edit {customer.name}</h1>
        <CustomerForm
          values={values}
          onChange={setValues}
          onSubmit={save}
          onCancel={() => {
            setEditing(false);
            setValues(null);
            operation.discard();
          }}
          submitLabel="Save changes"
          busy={operation.phase === "sending"}
          error={operation.error}
          currency={customer.currency}
          currencyExponent={customer.currency_exponent}
          unitLabel={customer.unit_label}
          codeEditable={false}
          showStatus
        />
        {operation.phase === "unresolved" ? (
          <div className="notice notice-error" role="alert">
            <span>
              We are not sure this reached the server. Retry sends the same change —
              it cannot be applied twice.
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

  return (
    <div className="stack">
      <header className="day-header">
        <h1 className="day-title">{customer.name}</h1>
        <button className="btn btn-primary" type="button" onClick={beginEdit}>
          Edit
        </button>
      </header>

      <dl className="detail card">
        <Row label="Customer code" value={customer.code} />
        <Row label="Status" value={customer.status === "ACTIVE" ? "Active" : "Inactive"} />
        <Row label="Phone" value={customer.phone_e164} />
        <Row label="WhatsApp" value={customer.whatsapp_e164} />
        <Row label="Area" value={customer.area} />
        <Row label="Address" value={customer.address} />
        <Row
          label="Usual quantity"
          value={`${customer.default_quantity} ${customer.unit_label}`}
        />
        <Row
          label={`Price per ${customer.unit_label}`}
          value={formatMoney(
            customer.unit_price_minor,
            customer.currency,
            customer.currency_exponent,
          )}
        />
        <Row
          label="Outstanding"
          value={formatMoney(
            customer.outstanding_minor,
            customer.currency,
            customer.currency_exponent,
          )}
        />
        <Row label="Payment status" value={humanStatus(customer.payment_status)} />
      </dl>

      {/* P8: the names this customer is actually called. Identity, not money —
          so it sits above the financial view rather than inside it. */}
      <CustomerAliases customerId={customer.id} />

      {/* The financial view: the server's balance above, and the documents and
          movements behind it. Nothing here re-derives the number. */}
      <CustomerFinancials customer={customer} />

      <button className="btn btn-quiet" type="button" onClick={() => navigate("/customers")}>
        Back to customers
      </button>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string | null }) {
  return (
    <div className="detail-row">
      <dt>{label}</dt>
      <dd>{value && value.trim() !== "" ? value : "—"}</dd>
    </div>
  );
}

/**
 * The server's own status value, said in words.
 *
 * Exactly the three `PaymentState` values `customer_payment_status` can return
 * (P0 §5.6). An unknown value falls through unchanged rather than being guessed
 * at — a new state would be a product decision, not a label to invent here.
 */
function humanStatus(status: string): string {
  const words: Record<string, string> = {
    PAID: "Paid up",
    PARTIALLY_PAID: "Partly paid",
    UNPAID: "Unpaid",
  };
  return words[status] ?? status;
}
