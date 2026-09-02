import { useId } from "react";

import { formatQuantity, isValidQuantity, parseQuantity, stepQuantity } from "@/lib/decimal";

/**
 * The quantity control for the daily register.
 *
 * Minus and plus move by whole units, which is the overwhelmingly common case and
 * needs no keyboard. The field itself stays editable and accepts up to three
 * decimal places, because the column is NUMERIC(12,3) and a half unit is a real
 * delivery — hiding decimals behind the stepper would make legitimate quantities
 * unrecordable.
 *
 * All arithmetic goes through `lib/decimal`, on scaled integers. The value never
 * becomes a JS `number`.
 */
export function QuantityStepper({
  value,
  onChange,
  unitLabel,
  disabled = false,
  describedBy,
}: {
  value: string;
  onChange: (next: string) => void;
  unitLabel: string;
  disabled?: boolean;
  describedBy?: string;
}) {
  const inputId = useId();
  const valid = isValidQuantity(value);
  const atZero = valid && parseQuantity(value) === 0n;

  return (
    <div className="stepper">
      <button
        className="btn btn-step"
        type="button"
        aria-label={`Decrease ${unitLabel}`}
        disabled={disabled || atZero}
        onClick={() => onChange(stepQuantity(value, -1))}
      >
        −
      </button>

      <div className="stepper-value">
        <label className="visually-hidden" htmlFor={inputId}>
          Quantity in {unitLabel}
        </label>
        <input
          id={inputId}
          className={valid ? "stepper-input" : "stepper-input is-invalid"}
          type="text"
          inputMode="decimal"
          autoComplete="off"
          value={value}
          disabled={disabled}
          aria-invalid={!valid}
          aria-describedby={describedBy}
          onChange={(e) => onChange(e.target.value)}
          onBlur={() => {
            if (isValidQuantity(value)) onChange(formatQuantity(parseQuantity(value)));
          }}
        />
        <span className="stepper-unit">{unitLabel}</span>
      </div>

      <button
        className="btn btn-step"
        type="button"
        aria-label={`Increase ${unitLabel}`}
        disabled={disabled}
        onClick={() => onChange(stepQuantity(value, 1))}
      >
        +
      </button>
    </div>
  );
}
