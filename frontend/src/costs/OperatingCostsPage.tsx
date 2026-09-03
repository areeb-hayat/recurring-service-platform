import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { messageFor } from "@/api/errors";
import {
  createCostItem,
  createCostRate,
  getCostHistory,
  getCostSummary,
  listCostItems,
  priceScenarios,
  recordCostActual,
  recordCostUsage,
  type CostActualDraft,
  type CostItemDraft,
  type CostRateDraft,
  type CostUsageDraft,
} from "@/api/costs";
import { createOperation } from "@/api/operation";
import type {
  CostItem,
  CostLine,
  CostRate,
  CostScenarioResponse,
  CostTotals,
} from "@/api/types";
import { EmptyState, Loading } from "@/components/Feedback";
import { formatMoney, majorToMinor } from "@/lib/money";
import { useSync } from "@/sync/SyncProvider";

/**
 * Running costs — what the business pays its providers (P6).
 *
 * **This is not the customer ledger and it is not commission.** Nothing on this
 * screen touches a customer's balance, and no commission figure exists anywhere
 * in it — commission is platform scope and the owner's token cannot read it.
 * The two are kept apart in the data model, in the API and here.
 *
 * **Estimated is not actual.** The estimate is measured usage times the rate the
 * owner configured; the actual is what the provider's invoice said. Variance is
 * `actual − estimated`, and it only appears when both halves exist. A month with
 * no invoice shows a blank, never a zero — an invoice that has not arrived is
 * not an invoice for nothing.
 *
 * **Rates are data.** Every provider price on this screen came out of the
 * database and can be changed here without a deployment. Changing one never
 * restates a month already recorded: that month kept the terms it was computed
 * with.
 *
 * **Online only.** These figures are not in the sync feed, so offline the screen
 * says so rather than showing something it cannot vouch for.
 */
export function OperatingCostsPage() {
  const { online } = useSync();
  const queryClient = useQueryClient();
  const [month, setMonth] = useState<string | null>(null);
  const [panel, setPanel] = useState<
    | { kind: "usage"; line: CostLine }
    | { kind: "actual"; line: CostLine }
    | { kind: "rate"; item: CostItem }
    | { kind: "item" }
    | null
  >(null);

  const summary = useQuery({
    queryKey: ["cost-summary", month],
    queryFn: () => getCostSummary(month ?? undefined),
    enabled: online,
  });
  const items = useQuery({
    queryKey: ["cost-items"],
    queryFn: listCostItems,
    enabled: online,
  });
  const history = useQuery({
    queryKey: ["cost-history", month],
    queryFn: () => getCostHistory(12, month ?? undefined),
    enabled: online,
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["cost-summary"] });
    void queryClient.invalidateQueries({ queryKey: ["cost-items"] });
    void queryClient.invalidateQueries({ queryKey: ["cost-history"] });
    setPanel(null);
  };

  if (!online) {
    return (
      <div className="stack">
        <h1 className="day-title">Running costs</h1>
        <p className="notice" role="status">
          Unavailable offline. Provider costs are kept on the server and are not
          synchronised to this device.
        </p>
      </div>
    );
  }

  if (summary.isPending || items.isPending) {
    return <Loading label="Loading running costs…" />;
  }
  if (!summary.data || !items.data) {
    return (
      <div className="stack">
        <h1 className="day-title">Running costs</h1>
        <p className="notice notice-error" role="alert">
          {messageFor(summary.error ?? items.error)}
        </p>
      </div>
    );
  }

  const period = summary.data.period_month;
  const itemById = new Map(items.data.items.map((i) => [i.id, i]));

  return (
    <div className="stack">
      <header className="day-header">
        <div>
          <h1 className="day-title">Running costs</h1>
          <p className="day-sub">
            What the business pays for hosting, services and tools. Separate from
            anything a customer owes.
          </p>
        </div>
        <MonthPicker value={period} onChange={setMonth} />
      </header>

      <TotalsRow totals={summary.data.totals} />

      <section className="stack" aria-labelledby="breakdown-heading">
        <h2 id="breakdown-heading" className="section-title">
          By provider
        </h2>
        {summary.data.lines.length === 0 ? (
          <EmptyState>
            No cost items yet. Add one for each service you pay for — hosting,
            storage, messaging, your domain.
          </EmptyState>
        ) : (
          <ul className="list cost-list">
            {summary.data.lines.map((line) => (
              <CostLineRow
                key={line.cost_item_id}
                line={line}
                onRecordUsage={() => setPanel({ kind: "usage", line })}
                onRecordActual={() => setPanel({ kind: "actual", line })}
                onAddRate={() => {
                  const item = itemById.get(line.cost_item_id);
                  if (item) setPanel({ kind: "rate", item });
                }}
              />
            ))}
          </ul>
        )}
        <button
          className="btn btn-quiet"
          type="button"
          onClick={() => setPanel({ kind: "item" })}
        >
          Add a cost item
        </button>
      </section>

      {panel?.kind === "usage" ? (
        <UsageForm line={panel.line} onDone={invalidate} onCancel={() => setPanel(null)} />
      ) : null}
      {panel?.kind === "actual" ? (
        <ActualForm line={panel.line} onDone={invalidate} onCancel={() => setPanel(null)} />
      ) : null}
      {panel?.kind === "rate" ? (
        <RateForm item={panel.item} onDone={invalidate} onCancel={() => setPanel(null)} />
      ) : null}
      {panel?.kind === "item" ? (
        <ItemForm onDone={invalidate} onCancel={() => setPanel(null)} />
      ) : null}

      <ScenarioCalculator items={items.data.items} periodMonth={period} />

      <HistoryTable
        months={history.data?.months ?? []}
        rangeTotals={history.data?.range_totals ?? []}
        loading={history.isPending}
      />
    </div>
  );
}

// --- summary pieces -----------------------------------------------------------

function TotalsRow({ totals }: { totals: CostTotals[] }) {
  if (totals.length === 0) {
    return <p className="empty">Nothing recorded for this month yet.</p>;
  }
  return (
    <div className="stack">
      {totals.map((total) => (
        <section
          key={total.currency}
          className="stat-grid"
          aria-label={`Totals in ${total.currency}`}
        >
          <Stat
            label="Estimated"
            value={amount(total.estimated_minor, total.currency)}
            hint="usage x your rates"
            emphasis
          />
          <Stat
            label="Actually invoiced"
            value={amount(total.actual_minor, total.currency)}
            hint="what providers billed"
          />
          <Stat
            label="Difference"
            value={signed(total.variance_minor, total.currency)}
            hint="invoiced minus estimated"
          />
        </section>
      ))}
      {totals.length > 1 ? (
        <p className="empty">
          Totals are kept in each provider's own currency. Nothing is converted —
          there is no exchange rate in this system.
        </p>
      ) : null}
    </div>
  );
}

function CostLineRow({
  line,
  onRecordUsage,
  onRecordActual,
  onAddRate,
}: {
  line: CostLine;
  onRecordUsage: () => void;
  onRecordActual: () => void;
  onAddRate: () => void;
}) {
  const usagePriced = line.rate?.unit_price_minor != null;
  return (
    <li className="list-row cost-row">
      <div className="list-main">
        <span className="list-title">{line.name}</span>
        <span className="list-sub">{rateWords(line)}</span>
        {line.usage_quantity ? (
          <span className="list-sub">
            Measured: {line.usage_quantity} {line.usage_unit}
          </span>
        ) : null}
      </div>
      <div className="cost-figures">
        <Figure
          label="Estimated"
          value={amount(line.estimated_amount_minor, line.currency, line.currency_exponent)}
        />
        <Figure
          label="Invoiced"
          value={amount(line.actual_amount_minor, line.currency, line.currency_exponent)}
        />
        <Figure
          label="Difference"
          value={signed(line.variance_minor, line.currency, line.currency_exponent)}
        />
      </div>
      <div className="cost-actions">
        {usagePriced ? (
          <button className="btn btn-quiet" type="button" onClick={onRecordUsage}>
            {line.usage_id ? "Change usage" : "Enter usage"}
          </button>
        ) : null}
        <button className="btn btn-quiet" type="button" onClick={onRecordActual}>
          {line.actual_id ? "Change invoice" : "Enter invoice"}
        </button>
        <button className="btn btn-quiet" type="button" onClick={onAddRate}>
          New rate
        </button>
      </div>
    </li>
  );
}

function Figure({ label, value }: { label: string; value: string }) {
  return (
    <span className="figure">
      <span className="figure-label">{label}</span>
      <span className="figure-value">{value}</span>
    </span>
  );
}

function Stat({
  label,
  value,
  hint,
  emphasis,
}: {
  label: string;
  value: string;
  hint?: string;
  emphasis?: boolean;
}) {
  return (
    <div className={emphasis ? "stat stat-emphasis" : "stat"}>
      <span className="stat-label">{label}</span>
      <strong className="stat-value">{value}</strong>
      {hint ? <span className="stat-hint">{hint}</span> : null}
    </div>
  );
}

// --- forms --------------------------------------------------------------------

function ItemForm({ onDone, onCancel }: { onDone: () => void; onCancel: () => void }) {
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const mutation = useMutation({
    mutationFn: (draft: CostItemDraft) =>
      createCostItem(createOperation("cost.item.create", draft)),
    onSuccess: onDone,
  });
  const ready = code.trim() !== "" && name.trim() !== "";

  return (
    <Panel title="Add a cost item" error={mutation.error} onCancel={onCancel}>
      <Field label="Short code" value={code} onChange={setCode} placeholder="HOSTING" />
      <Field
        label="What is it?"
        value={name}
        onChange={setName}
        placeholder="App hosting and database"
      />
      <Actions
        busy={mutation.isPending}
        ready={ready}
        label="Add item"
        onSubmit={() => mutation.mutate({ code: code.trim(), name: name.trim() })}
        onCancel={onCancel}
      />
    </Panel>
  );
}

/**
 * A new rate for a provider.
 *
 * Two shapes and no third: priced per unit of what you use, or a flat charge.
 * The rate starts on a date and runs until the next one begins — the previous
 * rate is closed automatically, never edited, so estimates already recorded keep
 * the price they were worked out with.
 */
function RateForm({
  item,
  onDone,
  onCancel,
}: {
  item: CostItem;
  onDone: () => void;
  onCancel: () => void;
}) {
  const [shape, setShape] = useState<"USAGE" | "FIXED">("USAGE");
  const [from, setFrom] = useState("");
  const [unit, setUnit] = useState("");
  const [price, setPrice] = useState("");
  const [recurrence, setRecurrence] = useState<"MONTHLY" | "ANNUAL">("MONTHLY");
  const [currency, setCurrency] = useState("USD");
  const [exponent] = useState(2);
  const [note, setNote] = useState("");

  const mutation = useMutation({
    mutationFn: (draft: CostRateDraft) =>
      createCostRate(item.id, createOperation("cost.rate.create", draft)),
    onSuccess: onDone,
  });

  const minor = majorToMinor(price, exponent);
  const ready =
    from !== "" && minor !== null && (shape === "FIXED" || unit.trim() !== "");

  return (
    <Panel title={`New rate for ${item.name}`} error={mutation.error} onCancel={onCancel}>
      <div className="field">
        <label htmlFor="rate-shape">How is it charged?</label>
        <select
          id="rate-shape"
          value={shape}
          onChange={(e) => setShape(e.target.value as "USAGE" | "FIXED")}
        >
          <option value="USAGE">By how much we use</option>
          <option value="FIXED">A flat charge</option>
        </select>
      </div>

      <Field label="Starting from" value={from} onChange={setFrom} type="date" />

      {shape === "USAGE" ? (
        <Field
          label="Charged per"
          value={unit}
          onChange={setUnit}
          placeholder="audio_hour, GB_month, million_tokens"
        />
      ) : (
        <div className="field">
          <label htmlFor="rate-recurrence">How often</label>
          <select
            id="rate-recurrence"
            value={recurrence}
            onChange={(e) => setRecurrence(e.target.value as "MONTHLY" | "ANNUAL")}
          >
            <option value="MONTHLY">Every month</option>
            <option value="ANNUAL">Once a year (shown as a twelfth each month)</option>
          </select>
        </div>
      )}

      <Field
        label={shape === "USAGE" ? "Price per unit" : "Amount"}
        value={price}
        onChange={setPrice}
        inputMode="decimal"
      />
      <Field label="Currency" value={currency} onChange={setCurrency} placeholder="USD" />
      <Field
        label="Where this price came from (optional)"
        value={note}
        onChange={setNote}
        placeholder="Provider pricing page, 3 Sep 2026"
      />

      <Actions
        busy={mutation.isPending}
        ready={ready}
        label="Save rate"
        onSubmit={() =>
          mutation.mutate({
            effective_from: from,
            unit: shape === "USAGE" ? unit.trim() : null,
            unit_price_minor: shape === "USAGE" ? minor : null,
            fixed_amount_minor: shape === "FIXED" ? minor : null,
            fixed_recurrence: shape === "FIXED" ? recurrence : null,
            currency: currency.trim().toUpperCase() || null,
            currency_exponent: exponent,
            source_note: note.trim() || null,
          })
        }
        onCancel={onCancel}
      />
    </Panel>
  );
}

function UsageForm({
  line,
  onDone,
  onCancel,
}: {
  line: CostLine;
  onDone: () => void;
  onCancel: () => void;
}) {
  const [quantity, setQuantity] = useState(line.usage_quantity ?? "");
  const [reason, setReason] = useState("");
  const replacing = line.usage_id !== null;

  const mutation = useMutation({
    mutationFn: (draft: CostUsageDraft) =>
      recordCostUsage(createOperation("cost.usage.record", draft)),
    onSuccess: onDone,
  });

  const ready =
    /^\d+(\.\d{1,6})?$/.test(quantity.trim()) && (!replacing || reason.trim() !== "");

  return (
    <Panel
      title={`Usage for ${line.name}`}
      error={mutation.error}
      onCancel={onCancel}
      note={`Measured in ${line.usage_unit ?? "units"}. The estimate is worked out on the server from the rate that applied to this month.`}
    >
      <Field
        label={`How much was used (${line.usage_unit ?? "units"})`}
        value={quantity}
        onChange={setQuantity}
        inputMode="decimal"
      />
      {replacing ? (
        <Field
          label="Why is the earlier figure being replaced?"
          value={reason}
          onChange={setReason}
          placeholder="Recounted from the provider console"
        />
      ) : null}
      <Actions
        busy={mutation.isPending}
        ready={ready}
        label={replacing ? "Replace usage" : "Save usage"}
        onSubmit={() =>
          mutation.mutate({
            cost_item_id: line.cost_item_id,
            period_month: line.period_month,
            usage_quantity: quantity.trim(),
            correction_reason: replacing ? reason.trim() : null,
          })
        }
        onCancel={onCancel}
      />
    </Panel>
  );
}

function ActualForm({
  line,
  onDone,
  onCancel,
}: {
  line: CostLine;
  onDone: () => void;
  onCancel: () => void;
}) {
  const [amountText, setAmountText] = useState("");
  const [reference, setReference] = useState("");
  const [reason, setReason] = useState("");
  const replacing = line.actual_id !== null;

  const mutation = useMutation({
    mutationFn: (draft: CostActualDraft) =>
      recordCostActual(createOperation("cost.actual.record", draft)),
    onSuccess: onDone,
  });

  const minor = majorToMinor(amountText, line.currency_exponent);
  const ready = minor !== null && (!replacing || reason.trim() !== "");

  return (
    <Panel
      title={`Invoice for ${line.name}`}
      error={mutation.error}
      onCancel={onCancel}
      note="What the provider actually charged. Leave it alone until the invoice arrives — a blank is not the same as zero."
    >
      <Field
        label={`Amount invoiced (${line.currency})`}
        value={amountText}
        onChange={setAmountText}
        inputMode="decimal"
      />
      <Field
        label="Invoice reference (optional)"
        value={reference}
        onChange={setReference}
      />
      {replacing ? (
        <Field
          label="Why is the earlier amount being replaced?"
          value={reason}
          onChange={setReason}
          placeholder="Provider reissued the invoice"
        />
      ) : null}
      <Actions
        busy={mutation.isPending}
        ready={ready}
        label={replacing ? "Replace invoice" : "Save invoice"}
        onSubmit={() =>
          mutation.mutate({
            cost_item_id: line.cost_item_id,
            period_month: line.period_month,
            amount_minor: minor ?? 0,
            invoice_reference: reference.trim() || null,
            correction_reason: replacing ? reason.trim() : null,
          })
        }
        onCancel={onCancel}
      />
    </Panel>
  );
}

// --- scenarios ----------------------------------------------------------------

const DEFAULT_SCENARIOS = [
  { label: "Starting", eventsPerDay: 100 },
  { label: "Reasonable", eventsPerDay: 500 },
  { label: "Larger", eventsPerDay: 1000 },
];

/**
 * "What would it cost if we used this much?"
 *
 * Planning information, not an invoice: it writes nothing, appears in no total,
 * and creates no usage figure. The commands-per-day and seconds-each go to the
 * server, which does the conversion and applies the configured rate — the client
 * would have to divide to produce hours, and dividing money-adjacent numbers on
 * the client is how a planning figure quietly stops matching the recorded one.
 */
function ScenarioCalculator({
  items,
  periodMonth,
}: {
  items: CostItem[];
  periodMonth: string;
}) {
  const usageItems = useMemo(
    () => items.filter((i) => i.rates.some((r) => r.unit_price_minor != null)),
    [items],
  );
  const [itemId, setItemId] = useState<string>("");
  const [seconds, setSeconds] = useState("5");
  const [perDay, setPerDay] = useState(DEFAULT_SCENARIOS.map((s) => s.eventsPerDay));
  const [answer, setAnswer] = useState<CostScenarioResponse | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    if (!itemId && usageItems.length > 0) setItemId(usageItems[0]!.id);
  }, [usageItems, itemId]);

  const mutation = useMutation({
    mutationFn: () =>
      priceScenarios(
        perDay.map((n, i) => ({
          label: DEFAULT_SCENARIOS[i]?.label ?? `${n} a day`,
          cost_item_id: itemId,
          events_per_day: n,
          seconds_per_event: seconds.trim(),
          days: 30,
        })),
        periodMonth,
      ),
    onSuccess: (data) => {
      setAnswer(data);
      setError(null);
    },
    onError: setError,
  });

  if (usageItems.length === 0) return null;

  return (
    <section className="card stack" aria-labelledby="scenario-heading">
      <h2 id="scenario-heading" className="section-title">
        What if we used more?
      </h2>
      <p className="empty">
        A rough projection at three levels of use. It is planning only — nothing
        here is recorded, and no invoice is implied.
      </p>

      <div className="field">
        <label htmlFor="scenario-item">Which service</label>
        <select
          id="scenario-item"
          value={itemId}
          onChange={(e) => setItemId(e.target.value)}
        >
          {usageItems.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
            </option>
          ))}
        </select>
      </div>

      <Field
        label="Seconds per use, on average"
        value={seconds}
        onChange={setSeconds}
        inputMode="decimal"
      />

      <div className="scenario-inputs">
        {perDay.map((value, index) => (
          <div className="field" key={index}>
            <label htmlFor={`scenario-${index}`}>
              {DEFAULT_SCENARIOS[index]?.label ?? "Level"} — uses per day
            </label>
            <input
              id={`scenario-${index}`}
              inputMode="numeric"
              value={String(value)}
              onChange={(e) => {
                const next = [...perDay];
                next[index] = Number(e.target.value.replace(/\D/g, "")) || 0;
                setPerDay(next);
              }}
            />
          </div>
        ))}
      </div>

      <div className="form-actions">
        <button
          className="btn btn-primary"
          type="button"
          disabled={mutation.isPending || !itemId}
          onClick={() => mutation.mutate()}
        >
          {mutation.isPending ? "Working it out…" : "Work it out"}
        </button>
      </div>

      {error ? (
        <p className="notice notice-error" role="alert">
          {messageFor(error)}
        </p>
      ) : null}

      {answer ? (
        <ul className="list">
          {answer.results.map((result, index) => (
            <li key={index} className="list-row">
              <div className="list-main">
                <span className="list-title">{result.label}</span>
                <span className="list-sub">
                  {result.derived_from
                    ? `${result.derived_from.events_per_day} a day · ${result.usage_quantity} ${result.usage_unit ?? ""}`
                    : `${result.usage_quantity} ${result.usage_unit ?? ""}`}
                </span>
              </div>
              <span className="amount">
                {amount(
                  result.estimated_amount_minor,
                  result.currency,
                  result.currency_exponent,
                )}
                <span className="list-sub"> / month</span>
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

// --- history ------------------------------------------------------------------

function HistoryTable({
  months,
  rangeTotals,
  loading,
}: {
  months: { period_month: string; totals: CostTotals[] }[];
  rangeTotals: CostTotals[];
  loading: boolean;
}) {
  const withData = months.filter((m) => m.totals.length > 0);
  return (
    <section className="stack" aria-labelledby="history-heading">
      <h2 id="history-heading" className="section-title">
        Month by month
      </h2>
      {loading ? (
        <p className="empty">Loading…</p>
      ) : withData.length === 0 ? (
        <EmptyState>Nothing recorded in the last year.</EmptyState>
      ) : (
        <>
          <ul className="list">
            {withData
              .slice()
              .reverse()
              .map((row) => (
                <li key={row.period_month} className="list-row">
                  <div className="list-main">
                    <span className="list-title">{monthWords(row.period_month)}</span>
                  </div>
                  <div className="cost-figures">
                    {row.totals.map((total) => (
                      <Figure
                        key={total.currency}
                        label={total.currency}
                        value={`${amount(total.estimated_minor, total.currency)} est · ${amount(
                          total.actual_minor,
                          total.currency,
                        )} inv`}
                      />
                    ))}
                  </div>
                </li>
              ))}
          </ul>
          {rangeTotals.length > 0 ? (
            <p className="empty">
              Over this range:{" "}
              {rangeTotals
                .map(
                  (t) =>
                    `${amount(t.estimated_minor, t.currency)} estimated, ${amount(
                      t.actual_minor,
                      t.currency,
                    )} invoiced`,
                )
                .join(" · ")}
            </p>
          ) : null}
        </>
      )}
    </section>
  );
}

// --- small shared pieces ------------------------------------------------------

function Panel({
  title,
  error,
  note,
  onCancel,
  children,
}: {
  title: string;
  error: unknown;
  note?: string;
  onCancel: () => void;
  children: React.ReactNode;
}) {
  return (
    <section className="card stack" aria-label={title}>
      <header className="statement-head">
        <h3 className="section-title">{title}</h3>
        <button className="btn btn-quiet" type="button" onClick={onCancel}>
          Close
        </button>
      </header>
      {note ? <p className="empty">{note}</p> : null}
      {error ? (
        <p className="notice notice-error" role="alert">
          {messageFor(error)}
        </p>
      ) : null}
      {children}
    </section>
  );
}

function Field({
  label,
  value,
  onChange,
  placeholder,
  type,
  inputMode,
}: {
  label: string;
  value: string;
  onChange: (next: string) => void;
  placeholder?: string;
  type?: string;
  inputMode?: "decimal" | "numeric" | "text";
}) {
  const id = `f-${label.replace(/\W+/g, "-").toLowerCase()}`;
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        value={value}
        type={type ?? "text"}
        inputMode={inputMode}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  );
}

function Actions({
  busy,
  ready,
  label,
  onSubmit,
  onCancel,
}: {
  busy: boolean;
  ready: boolean;
  label: string;
  onSubmit: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="form-actions">
      <button
        className="btn btn-primary"
        type="button"
        disabled={busy || !ready}
        onClick={onSubmit}
      >
        {busy ? "Saving…" : label}
      </button>
      <button className="btn btn-quiet" type="button" onClick={onCancel}>
        Cancel
      </button>
    </div>
  );
}

function MonthPicker({
  value,
  onChange,
}: {
  value: string;
  onChange: (next: string) => void;
}) {
  return (
    <div className="field">
      <label htmlFor="cost-month">Month</label>
      <input
        id="cost-month"
        type="month"
        value={value.slice(0, 7)}
        onChange={(e) =>
          onChange(e.target.value ? `${e.target.value}-01` : value)
        }
      />
    </div>
  );
}

/** A missing figure is a dash, never a zero (P6 §14). */
function amount(minor: number | null, currency: string, exponent = 2): string {
  if (minor === null) return "—";
  return formatMoney(minor, currency, exponent);
}

function signed(minor: number | null, currency: string, exponent = 2): string {
  if (minor === null) return "—";
  const text = formatMoney(Math.abs(minor), currency, exponent);
  if (minor === 0) return text;
  return `${minor > 0 ? "+" : "−"}${text}`;
}

function rateWords(line: CostLine): string {
  const rate: CostRate | null = line.rate;
  if (!rate) return "No rate set for this month";
  if (rate.unit_price_minor != null) {
    return `${formatMoney(rate.unit_price_minor, rate.currency, rate.currency_exponent)} per ${rate.unit}`;
  }
  if (rate.fixed_amount_minor != null) {
    const words = rate.fixed_recurrence === "ANNUAL" ? "a year" : "a month";
    return `${formatMoney(rate.fixed_amount_minor, rate.currency, rate.currency_exponent)} ${words}`;
  }
  return "";
}

const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

function monthWords(iso: string): string {
  const match = /^(\d{4})-(\d{2})/.exec(iso);
  if (!match) return iso;
  return `${MONTH_NAMES[Number(match[2]) - 1]} ${match[1]}`;
}
