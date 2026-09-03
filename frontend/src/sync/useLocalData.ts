/**
 * Reading the device's own copy of what the server said.
 *
 * Every screen that has to work offline reads through here, and every value it
 * returns came from the server. Nothing in this module computes a charge, a
 * balance, a due state or a payment status — offline or online, those are the
 * server's answers or they are not shown at all (SYN-9).
 *
 * When the snapshot has nothing to answer with, the caller renders "Unavailable
 * offline". A guess would be worse than a blank.
 */

import { useEffect, useState } from "react";

import type {
  Customer,
  DashboardSummary,
  Payment,
  ServiceRecord,
  Statement,
  TenantSettings,
} from "@/api/types";
import { useSync } from "./SyncProvider";
import {
  getMeta,
  listIssues,
  listOutbox,
  readSnapshot,
  readSnapshotRow,
} from "./stores";
import type { IssueEntry, OutboxEntry } from "./types";

export interface LocalData {
  settings: TenantSettings | null;
  customers: Customer[];
  records: ServiceRecord[];
  payments: Payment[];
  statements: Statement[];
  outbox: OutboxEntry[];
  issues: IssueEntry[];
}

const EMPTY: LocalData = {
  settings: null,
  customers: [],
  records: [],
  payments: [],
  statements: [],
  outbox: [],
  issues: [],
};

export interface LocalDataResult extends LocalData {
  loading: boolean;
  /** The device has never held enough to render this screen. */
  unavailable: boolean;
}

/**
 * Everything the register and the customer list need, from IndexedDB.
 *
 * Re-read whenever the engine reports that local data changed (`revision`), so
 * a completed sync, a queued CONFIRM and a resolved issue all refresh the screen
 * without a second source of truth in React state.
 */
export function useLocalData(): LocalDataResult {
  const { engine, revision } = useSync();
  const [data, setData] = useState<LocalData>(EMPTY);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    if (!engine) {
      setData(EMPTY);
      setLoading(false);
      return;
    }
    void (async () => {
      const db = await engine.db();
      const [settings, customers, records, payments, statements, outbox, issues] =
        await Promise.all([
          readSnapshotRow<TenantSettings>(db, "tenant", engine.tenantId),
          readSnapshot<Customer>(db, "customer"),
          readSnapshot<ServiceRecord>(db, "daily_service_record"),
          readSnapshot<Payment>(db, "payment"),
          readSnapshot<Statement>(db, "statement"),
          listOutbox(db),
          listIssues(db),
        ]);
      if (cancelled) return;
      setData({ settings, customers, records, payments, statements, outbox, issues });
      setLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [engine, revision]);

  return {
    ...data,
    loading,
    unavailable: !loading && data.settings === null,
  };
}

/**
 * The last dashboard summary the server computed, and when it was read.
 *
 * Returned rather than recomputed: every figure on it came from
 * `GET /dashboard/summary`. When there is none, the screen says so — a
 * dashboard that guesses is worse than one that admits it is offline.
 */
export function useCachedDashboard(): {
  summary: DashboardSummary | null;
  readAt: string | null;
  loading: boolean;
} {
  const { engine, revision } = useSync();
  const [state, setState] = useState<{
    summary: DashboardSummary | null;
    readAt: string | null;
    loading: boolean;
  }>({ summary: null, readAt: null, loading: true });

  useEffect(() => {
    let cancelled = false;
    if (!engine) {
      setState({ summary: null, readAt: null, loading: false });
      return;
    }
    void (async () => {
      const db = await engine.db();
      const [summary, readAt] = await Promise.all([
        readSnapshotRow<DashboardSummary>(db, "dashboard", engine.tenantId),
        getMeta(db, "dashboard_read_at"),
      ]);
      if (cancelled) return;
      setState({ summary, readAt, loading: false });
    })();
    return () => {
      cancelled = true;
    };
  }, [engine, revision]);

  return state;
}
