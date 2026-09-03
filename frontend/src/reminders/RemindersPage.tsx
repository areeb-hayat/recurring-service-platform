import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { messageFor } from "@/api/errors";
import { createOperation } from "@/api/operation";
import { getReminder, getReminderOverview, sendReminder } from "@/api/reminders";
import type {
  Reminder,
  ReminderDetail,
  ReminderRow,
  ReminderStage,
  ReminderStatus,
} from "@/api/types";
import { EmptyState, ErrorNotice, Loading } from "@/components/Feedback";
import { longDate } from "@/dashboard/DashboardPage";
import { formatMoney } from "@/lib/money";
import { useSync } from "@/sync/SyncProvider";

/**
 * Reminders — where each customer stands in this month's schedule (P7).
 *
 * **The server decides; this screen shows.** The amount beside a customer is the
 * live authoritative outstanding, the stage is the one the server's schedule made
 * due, and the status is derived on the server from both. Nothing on this page
 * adds a figure up, works out whether a reminder is due, or knows that the
 * schedule is 1/4/8/12/15 — those days arrive in the response, because they are
 * the tenant's configuration rather than this app's knowledge (REM-1).
 *
 * **Reminders are sent by the server's daily run, never from here.** There is no
 * "send reminders" button, because a person pressing one is exactly how a
 * schedule turns into a message flood. The single write on the page re-attempts
 * a *delivery that failed* — the stage already exists, and the server re-checks
 * the balance before it goes, so a customer who has paid since gets the stage
 * cancelled instead of a message.
 *
 * **Online only.** Reminder history is not in the sync feed, so offline the page
 * says so instead of showing a stale stage or a stale balance.
 */

const FILTERS = [
  { key: "needs", label: "Needs action" },
  { key: "all", label: "Everyone" },
  { key: "settled", label: "Settled" },
] as const;

type FilterKey = (typeof FILTERS)[number]["key"];

const STATUS_LABEL: Record<ReminderStatus, string> = {
  DUE: "Due now",
  WAITING: "Reminded",
  ATTENTION: "Needs you",
  SETTLED: "Paid up",
  NO_STATEMENT: "Not billed yet",
};

const STAGE_LABEL: Record<string, string> = {
  STATEMENT: "statement",
  REMINDER: "reminder",
  FINAL: "final notice",
  OWNER_ALERT: "alert to you",
};

export function RemindersPage() {
  const { online } = useSync();
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState<FilterKey>("needs");
  const [openId, setOpenId] = useState<string | null>(null);

  const overview = useQuery({
    queryKey: ["reminders"],
    queryFn: () => getReminderOverview(),
    enabled: online,
  });

  const rows = useMemo(() => {
    const items = overview.data?.items ?? [];
    if (filter === "all") return items;
    if (filter === "settled") return items.filter((r) => r.status === "SETTLED");
    return items.filter((r) => r.status === "DUE" || r.status === "ATTENTION");
  }, [overview.data, filter]);

  if (!online) {
    return (
      <div className="stack">
        <h1 className="day-title">Reminders</h1>
        <p className="notice" role="status">
          Unavailable offline. Reminders are sent by the server, and this list is
          not synchronised to this device.
        </p>
      </div>
    );
  }

  if (overview.isPending) return <Loading label="Loading reminders…" />;
  if (!overview.data) {
    return (
      <div className="stack">
        <h1 className="day-title">Reminders</h1>
        <ErrorNotice
          message={messageFor(overview.error)}
          onRetry={() => void overview.refetch()}
        />
      </div>
    );
  }

  const { business_date, currency, currency_exponent, schedule, due_stage, counts } =
    overview.data;
  const money = (minor: number) => formatMoney(minor, currency, currency_exponent);

  return (
    <div className="stack">
      <header className="day-header">
        <div>
          <h1 className="day-title">Reminders</h1>
          <p className="day-sub">
            {longDate(business_date)} ·{" "}
            {due_stage
              ? `today's step is the day-${due_stage.day} ${STAGE_LABEL[due_stage.kind]}`
              : "no step is due yet this month"}
          </p>
        </div>
      </header>

      <section className="card stack" aria-label="This month's schedule">
        <h2 className="section-title">How the month runs</h2>
        <p className="day-sub">
          Sent automatically by the server. A customer who pays in full stops
          receiving them straight away, and a part payment lowers the amount.
        </p>
        <ul className="choice-row" aria-label="Reminder schedule">
          {schedule.map((stage: ReminderStage) => (
            <li
              key={`${stage.day}-${stage.kind}`}
              className={
                due_stage && stage.day === due_stage.day ? "badge badge-now" : "badge"
              }
            >
              Day {stage.day} · {STAGE_LABEL[stage.kind]}
            </li>
          ))}
        </ul>
      </section>

      <div className="stat-grid">
        <Stat label="Due now" value={String(counts.due)} />
        <Stat label="Needs you" value={String(counts.attention)} emphasis />
        <Stat label="Paid up" value={String(counts.settled)} />
      </div>

      <div className="choice-row" role="group" aria-label="Filter reminders">
        {FILTERS.map((option) => (
          <button
            key={option.key}
            type="button"
            className={filter === option.key ? "choice is-selected" : "choice"}
            aria-pressed={filter === option.key}
            onClick={() => setFilter(option.key)}
          >
            {option.label}
          </button>
        ))}
      </div>

      {counts.total === 0 ? (
        <EmptyState>
          No customers yet. Reminders begin once a billing period has been closed
          and statements have been issued.
        </EmptyState>
      ) : rows.length === 0 ? (
        <EmptyState>
          {filter === "needs"
            ? "Nothing needs you right now."
            : "Nobody in this list."}
        </EmptyState>
      ) : (
        <ul className="list">
          {rows.map((row) => (
            <ReminderRowItem
              key={row.customer_id}
              row={row}
              money={money}
              open={openId === row.customer_id}
              onToggle={() =>
                setOpenId(openId === row.customer_id ? null : row.customer_id)
              }
              onChanged={() => {
                void queryClient.invalidateQueries({ queryKey: ["reminders"] });
              }}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

function Stat({
  label,
  value,
  emphasis,
}: {
  label: string;
  value: string;
  emphasis?: boolean;
}) {
  return (
    <div className={emphasis ? "stat stat-emphasis" : "stat"}>
      <span className="stat-label">{label}</span>
      <span className="stat-value">{value}</span>
    </div>
  );
}

function ReminderRowItem({
  row,
  money,
  open,
  onToggle,
  onChanged,
}: {
  row: ReminderRow;
  money: (minor: number) => string;
  open: boolean;
  onToggle: () => void;
  onChanged: () => void;
}) {
  return (
    <li className="list-row reminder-row">
      <div className="reminder-head">
        <button
          type="button"
          className="list-main list-button"
          aria-expanded={open}
          onClick={onToggle}
        >
          <span className="list-title">{row.name}</span>
          <span className="list-sub">
            {row.code}
            {row.area ? ` · ${row.area}` : ""} · {summaryFor(row)}
          </span>
        </button>
        <span className="reminder-side">
          <span className={row.outstanding_minor < 0 ? "amount amount-credit" : "amount"}>
            {money(row.outstanding_minor)}
          </span>
          <span className={`badge badge-${row.status.toLowerCase()}`}>
            {STATUS_LABEL[row.status]}
          </span>
        </span>
      </div>
      {open ? <ReminderHistory row={row} money={money} onChanged={onChanged} /> : null}
    </li>
  );
}

/**
 * What has actually happened for this customer this cycle, in plain words.
 *
 * The stage numbers and the amounts are the server's; the sentences around them
 * are this screen's only contribution.
 */
function summaryFor(row: ReminderRow): string {
  if (row.status === "NO_STATEMENT") return "no statement issued yet";
  if (!row.has_contact && row.outstanding_minor > 0) return "no phone number on file";
  if (row.latest) {
    const when = row.latest.sent_at ?? row.latest.generated_at;
    const what = STAGE_LABEL[row.latest.kind] ?? "reminder";
    if (row.latest.state === "FAILED") return `day-${row.latest.schedule_day} ${what} failed`;
    if (row.latest.state === "CANCELLED") return `day-${row.latest.schedule_day} ${what} stopped`;
    return `day-${row.latest.schedule_day} ${what} sent${when ? ` ${longDate(when)}` : ""}`;
  }
  if (row.status === "DUE") return "not reminded yet this cycle";
  return "nothing sent this cycle";
}

function ReminderHistory({
  row,
  money,
  onChanged,
}: {
  row: ReminderRow;
  money: (minor: number) => string;
  onChanged: () => void;
}) {
  const failed = row.history.find((r) => r.state === "FAILED");

  return (
    <div className="reminder-detail stack">
      {row.cycle ? (
        <p className="day-sub">
          Billing period {row.cycle.period_start} to {row.cycle.period_end} ·
          billed {money(row.cycle.statement_closing_balance_minor)}, owed now{" "}
          {money(row.outstanding_minor)}.
        </p>
      ) : (
        <p className="day-sub">
          No statement has been issued for this customer, so no reminder is sent.
          Close a billing period to start the schedule.
        </p>
      )}

      {row.history.length === 0 ? (
        <EmptyState>Nothing has been sent this cycle.</EmptyState>
      ) : (
        <ul className="reminder-steps">
          {row.history.map((entry) => (
            <li key={entry.id} className="reminder-step">
              <span>
                Day {entry.schedule_day} · {STAGE_LABEL[entry.kind]}
              </span>
              <span className={`badge badge-${entry.state.toLowerCase()}`}>
                {entry.state === "SENT"
                  ? "sent"
                  : entry.state === "FAILED"
                    ? "failed"
                    : entry.state === "CANCELLED"
                      ? "stopped"
                      : "waiting"}
              </span>
            </li>
          ))}
        </ul>
      )}

      {row.next_stage ? (
        <p className="day-sub">
          Next: the day-{row.next_stage.day} {STAGE_LABEL[row.next_stage.kind]}, if
          anything is still owed then.
        </p>
      ) : null}

      {failed ? <RetryPanel reminder={failed} onChanged={onChanged} /> : null}
    </div>
  );
}

/**
 * Re-attempt one failed delivery.
 *
 * The only mutation on the page, and it is deliberately narrow: it names an
 * existing stage and nothing else. The `operation_id` is created once, at the
 * click, so a lost response replays instead of sending twice.
 */
function RetryPanel({
  reminder,
  onChanged,
}: {
  reminder: Reminder;
  onChanged: () => void;
}) {
  const [result, setResult] = useState<ReminderDetail | null>(null);

  const retry = useMutation({
    mutationFn: async () => {
      const envelope = createOperation<Record<string, never>>("reminder.send", {});
      const response = await sendReminder(reminder.id, envelope);
      return response.entity;
    },
    onSuccess: async (entity) => {
      // The list result is a summary; re-read the reminder for the attempt log.
      try {
        setResult(await getReminder(entity.id));
      } catch {
        setResult(entity);
      }
      onChanged();
    },
  });

  return (
    <div className="stack reminder-retry">
      <p className="notice notice-error" role="alert">
        Last attempt failed: {reminder.last_error ?? "delivery failed"} (attempt{" "}
        {reminder.attempt_count}).
      </p>
      <button
        type="button"
        className="btn"
        onClick={() => retry.mutate()}
        disabled={retry.isPending}
      >
        {retry.isPending ? "Trying again…" : "Try sending again"}
      </button>
      {retry.isError ? <ErrorNotice message={messageFor(retry.error)} /> : null}
      {result ? (
        <p className="notice" role="status">
          {result.state === "SENT"
            ? "Sent."
            : result.state === "CANCELLED"
              ? "Not sent — this customer no longer owes anything."
              : `Still failing: ${result.last_error ?? "delivery failed"}.`}
        </p>
      ) : null}
    </div>
  );
}
