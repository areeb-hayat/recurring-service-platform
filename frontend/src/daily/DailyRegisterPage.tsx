import { useCallback, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { messageFor } from "@/api/errors";
import { EmptyState, ErrorNotice, Loading } from "@/components/Feedback";
import { ServiceCard } from "./ServiceCard";
import {
  buildRegister,
  useCustomersQuery,
  useDayQuery,
  useTenantSettingsQuery,
  type RegisterEntry,
} from "./useRegister";

/**
 * The screen this product exists for.
 *
 * One customer at a time, in a card big enough to use standing up and one-handed.
 * The rest of the round is listed underneath so the owner can jump to anyone out
 * of order — a real round is not a straight line — and so "who is left" is
 * visible without counting.
 *
 * After a save the card stays on that customer showing what was recorded, and the
 * person moves on deliberately with "Next customer". Advancing automatically
 * would mean the confirmation of one entry appears over the top of the next
 * customer's name, which is exactly how the wrong person gets recorded twice.
 */
export function DailyRegisterPage() {
  const queryClient = useQueryClient();
  const settings = useTenantSettingsQuery();
  const day = useDayQuery(settings.data?.business_date);
  const customers = useCustomersQuery();
  const [cursor, setCursor] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const register = useMemo(
    () => (customers.data && day.data ? buildRegister(customers.data, day.data) : null),
    [customers.data, day.data],
  );

  const refresh = useCallback(
    (customerId: string) => {
      // Stay on the customer just recorded so the result is read where it was
      // made; the refetch turns the card into its recorded state.
      setSelectedId(customerId);
      void queryClient.invalidateQueries({ queryKey: ["day"] });
    },
    [queryClient],
  );

  const failure = settings.error ?? day.error ?? customers.error;
  if (failure) {
    return (
      <ErrorNotice
        message={messageFor(failure)}
        onRetry={() => {
          void settings.refetch();
          void day.refetch();
          void customers.refetch();
        }}
      />
    );
  }

  if (!register) return <Loading label="Loading today's round…" />;

  const { pending, done, entries, businessDate } = register;
  const selected: RegisterEntry | undefined =
    (selectedId ? entries.find((e) => e.customer.id === selectedId) : undefined) ??
    pending[Math.min(cursor, Math.max(pending.length - 1, 0))];

  const advance = () => {
    setSelectedId(null);
    setCursor((c) => (pending.length === 0 ? 0 : (c + 1) % pending.length));
  };

  return (
    <div className="stack">
      <header className="day-header">
        <h1 className="day-title">{formatBusinessDate(businessDate)}</h1>
        <p className="day-progress" role="status">
          {done.length} of {entries.length} recorded
        </p>
      </header>

      {entries.length === 0 ? (
        <EmptyState>No active customers yet. Add one from Customers.</EmptyState>
      ) : null}

      {selected ? (
        selected.record ? (
          <section className="card stack" aria-labelledby="register-customer">
            <h2 id="register-customer" className="customer-name">
              {selected.customer.name}
            </h2>
            <p className="notice notice-success" role="status">
              {selected.record.kind === "SKIP"
                ? "Skipped today."
                : `Recorded ${selected.record.quantity} ${selected.record.unit_label}.`}
            </p>
            <button className="btn btn-primary btn-lg" type="button" onClick={advance}>
              Next customer
            </button>
          </section>
        ) : (
          <ServiceCard
            key={selected.customer.id}
            entry={selected}
            position={Math.min(done.length + 1, entries.length)}
            total={entries.length}
            onRecorded={() => refresh(selected.customer.id)}
            onNext={advance}
          />
        )
      ) : null}

      {pending.length === 0 && entries.length > 0 && !selected ? (
        <EmptyState>Everyone is done for today.</EmptyState>
      ) : null}

      <RoundList title={`Still to do (${pending.length})`} entries={pending} onPick={setSelectedId} />
      <RoundList title={`Done (${done.length})`} entries={done} onPick={setSelectedId} />
    </div>
  );
}

function RoundList({
  title,
  entries,
  onPick,
}: {
  title: string;
  entries: RegisterEntry[];
  onPick: (id: string) => void;
}) {
  if (entries.length === 0) return null;
  return (
    <section className="round-list">
      <h2 className="round-title">{title}</h2>
      <ul className="list">
        {entries.map(({ customer, record }) => (
          <li key={customer.id}>
            <button className="row" type="button" onClick={() => onPick(customer.id)}>
              <span className="row-main">{customer.name}</span>
              <span className="row-meta">
                {record === null
                  ? (customer.area ?? "")
                  : record.kind === "SKIP"
                    ? "Skipped"
                    : `${record.quantity} ${record.unit_label}`}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** Formatting only — the date itself is the server's business date, never derived. */
function formatBusinessDate(iso: string): string {
  const [year, month, day] = iso.split("-").map(Number);
  if (!year || !month || !day) return iso;
  return new Date(Date.UTC(year, month - 1, day)).toLocaleDateString(undefined, {
    weekday: "long",
    day: "numeric",
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  });
}
