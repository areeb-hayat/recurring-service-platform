import { useId, useState, type FormEvent } from "react";

import { ApiError, fieldErrorFor, messageFor } from "@/api/errors";
import type { CustomerDraft } from "@/api/customers";
import type { Customer } from "@/api/types";
import { isValidQuantity } from "@/lib/decimal";
import { majorToMinor, minorToMajor } from "@/lib/money";

/**
 * The create and edit form.
 *
 * Every field here exists on `CreateCustomerRequest`/`UpdateCustomerRequest`;
 * nothing is invented and nothing the backend accepts is hidden. `code` is
 * immutable after creation because the update request has no `code` field — the
 * form reflects that rather than pretending otherwise.
 *
 * The price is entered in **major** units, because that is how a person says it,
 * and converted to minor units by counting digits rather than by multiplying a
 * float. That is a representation change on a number the person typed, not a
 * derived amount: the server still decides every charge (P0 §5).
 */
export interface CustomerFormValues {
  code: string;
  name: string;
  phone_e164: string;
  whatsapp_e164: string;
  address: string;
  area: string;
  default_quantity: string;
  unit_price_major: string;
  status: "ACTIVE" | "INACTIVE";
}

export function emptyValues(): CustomerFormValues {
  return {
    code: "",
    name: "",
    phone_e164: "",
    whatsapp_e164: "",
    address: "",
    area: "",
    default_quantity: "1",
    unit_price_major: "0",
    status: "ACTIVE",
  };
}

export function valuesFromCustomer(customer: Customer): CustomerFormValues {
  return {
    code: customer.code,
    name: customer.name,
    phone_e164: customer.phone_e164 ?? "",
    whatsapp_e164: customer.whatsapp_e164 ?? "",
    address: customer.address ?? "",
    area: customer.area ?? "",
    default_quantity: customer.default_quantity,
    unit_price_major: minorToMajor(customer.unit_price_minor, customer.currency_exponent),
    status: customer.status,
  };
}

// Both moved to `lib/money.ts` in P6 and re-exported here so existing callers
// are unchanged. The payment form parses a typed amount too, and "what a person
// typed, as minor units" wants one definition rather than two that agree today.
export { majorToMinor, minorToMajor };

export function toDraft(values: CustomerFormValues, exponent: number): CustomerDraft | null {
  const minor = majorToMinor(values.unit_price_major, exponent);
  if (minor === null || !isValidQuantity(values.default_quantity)) return null;
  const optional = (v: string) => (v.trim() === "" ? null : v.trim());
  return {
    code: values.code.trim(),
    name: values.name.trim(),
    phone_e164: optional(values.phone_e164),
    whatsapp_e164: optional(values.whatsapp_e164),
    address: optional(values.address),
    area: optional(values.area),
    default_quantity: values.default_quantity.trim(),
    unit_price_minor: minor,
  };
}

export function CustomerForm({
  values,
  onChange,
  onSubmit,
  onCancel,
  submitLabel,
  busy,
  error,
  currency,
  currencyExponent,
  unitLabel,
  codeEditable,
  showStatus,
}: {
  values: CustomerFormValues;
  onChange: (next: CustomerFormValues) => void;
  onSubmit: () => void;
  onCancel: () => void;
  submitLabel: string;
  busy: boolean;
  error: unknown;
  currency: string;
  currencyExponent: number;
  unitLabel: string;
  codeEditable: boolean;
  showStatus: boolean;
}) {
  const formId = useId();
  const [touched, setTouched] = useState(false);

  const quantityOk = isValidQuantity(values.default_quantity);
  const priceOk = majorToMinor(values.unit_price_major, currencyExponent) !== null;
  const nameOk = values.name.trim() !== "";
  const codeOk = !codeEditable || values.code.trim() !== "";
  const valid = quantityOk && priceOk && nameOk && codeOk;

  const set = (patch: Partial<CustomerFormValues>) => onChange({ ...values, ...patch });

  function submit(event: FormEvent) {
    event.preventDefault();
    setTouched(true);
    if (valid && !busy) onSubmit();
  }

  const codeTaken = error instanceof ApiError && error.code === "CUSTOMER_CODE_TAKEN";

  return (
    <form className="card stack" onSubmit={submit} noValidate>
      {error ? (
        <p className="notice notice-error" role="alert">
          {messageFor(error)}
        </p>
      ) : null}

      <Field
        id={formId + "-name"}
        label="Name"
        value={values.name}
        onChange={(v) => set({ name: v })}
        required
        invalid={touched && !nameOk}
        message={
          fieldErrorFor(error, "name") ?? (touched && !nameOk ? "Name is required" : undefined)
        }
      />

      <Field
        id={formId + "-code"}
        label="Customer code"
        value={values.code}
        onChange={(v) => set({ code: v })}
        required={codeEditable}
        disabled={!codeEditable}
        invalid={(touched && !codeOk) || codeTaken}
        message={
          codeEditable
            ? (fieldErrorFor(error, "code") ??
              (codeTaken ? "That code is already in use" : undefined))
            : "A code cannot be changed once the customer exists"
        }
      />

      <Field
        id={formId + "-phone"}
        label="Phone"
        value={values.phone_e164}
        onChange={(v) => set({ phone_e164: v })}
        type="tel"
        message={fieldErrorFor(error, "phone_e164")}
      />

      <Field
        id={formId + "-whatsapp"}
        label="WhatsApp"
        value={values.whatsapp_e164}
        onChange={(v) => set({ whatsapp_e164: v })}
        type="tel"
        message={fieldErrorFor(error, "whatsapp_e164")}
      />

      <Field
        id={formId + "-area"}
        label="Area"
        value={values.area}
        onChange={(v) => set({ area: v })}
      />

      <Field
        id={formId + "-address"}
        label="Address"
        value={values.address}
        onChange={(v) => set({ address: v })}
        multiline
      />

      <Field
        id={formId + "-quantity"}
        label={"Usual quantity (" + unitLabel + ")"}
        value={values.default_quantity}
        onChange={(v) => set({ default_quantity: v })}
        inputMode="decimal"
        invalid={touched && !quantityOk}
        message={
          touched && !quantityOk
            ? "Use up to 3 decimal places, for example 2 or 1.5"
            : undefined
        }
      />

      <Field
        id={formId + "-price"}
        label={"Price per " + unitLabel + " (" + currency + ")"}
        value={values.unit_price_major}
        onChange={(v) => set({ unit_price_major: v })}
        inputMode="decimal"
        invalid={touched && !priceOk}
        message={
          touched && !priceOk
            ? "Use a positive amount with up to " + currencyExponent + " decimal places"
            : undefined
        }
      />

      {showStatus ? (
        <div className="field">
          <label htmlFor={formId + "-status"}>Status</label>
          <select
            id={formId + "-status"}
            value={values.status}
            onChange={(e) => set({ status: e.target.value as "ACTIVE" | "INACTIVE" })}
          >
            <option value="ACTIVE">Active — appears on the daily round</option>
            <option value="INACTIVE">Inactive — kept, but off the round</option>
          </select>
        </div>
      ) : null}

      <button className="btn btn-primary btn-lg" type="submit" disabled={busy}>
        {busy ? "Saving…" : submitLabel}
      </button>
      <button className="btn btn-quiet" type="button" onClick={onCancel} disabled={busy}>
        Cancel
      </button>
    </form>
  );
}

function Field({
  id,
  label,
  value,
  onChange,
  type = "text",
  inputMode,
  required = false,
  disabled = false,
  multiline = false,
  invalid = false,
  message,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  type?: string;
  inputMode?: "decimal" | "text";
  required?: boolean;
  disabled?: boolean;
  multiline?: boolean;
  invalid?: boolean;
  message?: string;
}) {
  const messageId = id + "-message";
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {multiline ? (
        <textarea
          id={id}
          value={value}
          disabled={disabled}
          required={required}
          rows={2}
          aria-invalid={invalid || undefined}
          aria-describedby={message ? messageId : undefined}
          onChange={(e) => onChange(e.target.value)}
        />
      ) : (
        <input
          id={id}
          value={value}
          disabled={disabled}
          required={required}
          type={type}
          inputMode={inputMode}
          autoComplete="off"
          aria-invalid={invalid || undefined}
          aria-describedby={message ? messageId : undefined}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
      {message ? (
        <p id={messageId} className={invalid ? "hint hint-error" : "hint"}>
          {message}
        </p>
      ) : null}
    </div>
  );
}
