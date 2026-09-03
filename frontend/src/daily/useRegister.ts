/**
 * The daily register's data — now read from the device, not from the network.
 *
 * P4 composed the screen from three live reads. P5 composes it from the
 * IndexedDB `snapshot`, which the sync engine fills from exactly those same
 * server reads and then keeps current from the change feed. That is the whole
 * difference between an app that works in a stairwell and one that does not, and
 * it costs nothing in authority: every field still came from the server, and the
 * join below still derives no money, no balance and no due state.
 *
 * **Which date.** Still never the client's. `business_date` is whatever the
 * server last said it was, carried in the snapshot. Offline it may be yesterday's
 * — which is why the screen prints it in words rather than saying "Today".
 *
 * **Three states per customer**, not two:
 *
 *  - *pending* — nothing recorded, nothing queued;
 *  - *queued* — saved on this device, waiting to sync. Not accepted by anyone yet;
 *  - *done* — the server has a record for this customer and date.
 *
 * A queued entry is never shown as recorded. That distinction is the entire
 * honesty of the offline story.
 */

import { useMemo } from "react";

import type { Customer, ServiceRecord, TenantSettings } from "@/api/types";
import { useLocalData } from "@/sync/useLocalData";
import type { IssueEntry, OutboxEntry } from "@/sync/types";

export interface RegisterEntry {
  customer: Customer;
  /** The server's record for this customer and business date, if it has one. */
  record: ServiceRecord | null;
  /** Saved on this device and not yet answered by the server. */
  queued: OutboxEntry | null;
}

export interface Register {
  businessDate: string;
  settings: TenantSettings;
  entries: RegisterEntry[];
  pending: RegisterEntry[];
  queued: RegisterEntry[];
  done: RegisterEntry[];
}

export interface RegisterResult {
  register: Register | null;
  loading: boolean;
  /** Nothing has ever been synchronised on this device. */
  unavailable: boolean;
  issues: IssueEntry[];
}

export function buildRegister(
  settings: TenantSettings,
  customers: Customer[],
  records: ServiceRecord[],
  outbox: OutboxEntry[],
): Register {
  const businessDate = settings.business_date;

  const byCustomer = new Map<string, ServiceRecord>();
  for (const record of records) {
    if (record.service_date !== businessDate) continue;
    if (record.status !== "ACTIVE") continue;
    byCustomer.set(record.customer_id, record);
  }

  const queuedByCustomer = new Map<string, OutboxEntry>();
  for (const entry of outbox) {
    if (entry.context.service_date !== businessDate) continue;
    queuedByCustomer.set(entry.context.customer_id, entry);
  }

  const entries: RegisterEntry[] = customers
    .filter((customer) => customer.status === "ACTIVE")
    .sort((a, b) => a.name.localeCompare(b.name) || a.id.localeCompare(b.id))
    .map((customer) => ({
      customer,
      record: byCustomer.get(customer.id) ?? null,
      queued: queuedByCustomer.get(customer.id) ?? null,
    }));

  return {
    businessDate,
    settings,
    entries,
    pending: entries.filter((e) => e.record === null && e.queued === null),
    queued: entries.filter((e) => e.record === null && e.queued !== null),
    done: entries.filter((e) => e.record !== null),
  };
}

export function useRegister(): RegisterResult {
  const { settings, customers, records, outbox, issues, loading, unavailable } =
    useLocalData();

  const register = useMemo(
    () => (settings ? buildRegister(settings, customers, records, outbox) : null),
    [settings, customers, records, outbox],
  );

  return { register, loading, unavailable, issues };
}
