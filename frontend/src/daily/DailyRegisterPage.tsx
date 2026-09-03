import { useMemo, useState } from "react";

import { EmptyState, Loading } from "@/components/Feedback";
import { useSync } from "@/sync/SyncProvider";
import { ServiceCard } from "./ServiceCard";
import { useRegister, type RegisterEntry } from "./useRegister";

/**
 * The screen this product exists for, now with no network on its critical path.
 *
 * One customer at a time, in a card big enough to use standing up and one-handed.
 * Everything it renders comes from the device's snapshot of what the server said,
 * so a stairwell, a basement or a dead SIM changes nothing about the round.
 *
 * The business date is printed in words at the top, deliberately, instead of the
 * word "Today". Offline the snapshot may be carrying yesterday's date, and the
 * person deserves to see which day their taps are being filed under before they
 * make thirty of them.
 */
export function DailyRegisterPage() {
  const { register, loading, unavailable } = useRegister();
  const { online, hydrated } = useSync();
  const [cursor, setCursor] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const selected: RegisterEntry | undefined = useMemo(() => {
    if (!register) return undefined;
    const todo = [...register.pending, ...register.queued];
    return (
      (selectedId
        ? register.entries.find((e) => e.customer.id === selectedId)
        : undefined) ??
      register.pending[Math.min(cursor, Math.max(register.pending.length - 1, 0))] ??
      todo[0]
    );
  }, [register, selectedId, cursor]);

  if (loading) return <Loading label="Loading today's round…" />;

  if (unavailable || !register) {
    return (
      <div className="stack">
        <p className="notice notice-error" role="alert">
          {online
            ? "We could not load the round. It will appear once this device has synchronised."
            : "Unavailable offline. This device has not synchronised yet — connect once and the round will be available without a network."}
        </p>
      </div>
    );
  }

  const { pending, queued, done, entries, businessDate, settings } = register;

  // Two different "move on"s, and they are not the same movement.
  //
  // After a save the customer has *left* the pending list, so the list has
  // already shifted under the cursor and the same index is now the next person.
  // Incrementing here would step over somebody — which on a real round means a
  // house missed in silence.
  const afterSave = () => setSelectedId(null);

  // "Leave for later" changes nothing, so the cursor has to do the moving.
  const leaveForLater = () => {
    setSelectedId(null);
    setCursor((c) => (pending.length === 0 ? 0 : (c + 1) % pending.length));
  };

  return (
    <div className="stack">
      <header className="day-header">
        <h1 className="day-title">{formatBusinessDate(businessDate)}</h1>
        <p className="day-progress" role="status">
          {done.length} of {entries.length} recorded
          {queued.length > 0 ? ` · ${queued.length} waiting to sync` : ""}
        </p>
        {!online && hydrated ? (
          <p className="notice notice-pending" role="status">
            Offline. Entries are saved on this device and sent when the connection
            returns.
          </p>
        ) : null}
      </header>

      {entries.length === 0 ? (
        <EmptyState>No active customers yet. Add one from Customers.</EmptyState>
      ) : null}

      {selected ? (
        <ServiceCard
          key={selected.customer.id}
          entry={selected}
          settings={settings}
          position={Math.min(done.length + queued.length + 1, entries.length)}
          total={entries.length}
          onNext={afterSave}
          onLeaveForLater={leaveForLater}
          onQueued={setSelectedId}
        />
      ) : null}

      {pending.length === 0 && queued.length === 0 && entries.length > 0 && !selected ? (
        <EmptyState>Everyone is done for today.</EmptyState>
      ) : null}

      <RoundList
        title={`Still to do (${pending.length})`}
        entries={pending}
        onPick={setSelectedId}
      />
      <RoundList
        title={`Waiting to sync (${queued.length})`}
        entries={queued}
        onPick={setSelectedId}
      />
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
        {entries.map((entry) => (
          <li key={entry.customer.id}>
            <button className="row" type="button" onClick={() => onPick(entry.customer.id)}>
              <span className="row-main">{entry.customer.name}</span>
              <span className="row-meta">{describe(entry)}</span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** Wording is the whole point: only a server record is ever called "recorded". */
function describe(entry: RegisterEntry): string {
  if (entry.record) {
    return entry.record.kind === "SKIP"
      ? "Skipped"
      : `${entry.record.quantity} ${entry.record.unit_label}`;
  }
  if (entry.queued) {
    const { kind, quantity, unit_label } = entry.queued.context;
    return kind === "SKIP" ? "Skip · waiting to sync" : `${quantity} ${unit_label} · waiting to sync`;
  }
  return entry.customer.area ?? "";
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
