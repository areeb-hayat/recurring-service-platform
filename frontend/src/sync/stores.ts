/**
 * Typed access to the four stores, and the two writes that must be atomic.
 *
 * The atomic pair (SYN-6):
 *
 *  - {@link promoteToIssue} — removing an operation from `outbox` and writing it
 *    into `issues` happen in **one** IndexedDB transaction. There is no instant
 *    at which a REJECTED or CONFLICT operation exists in neither store, so a tab
 *    closing between the two writes cannot lose the problem.
 *  - {@link applyChanges} — the snapshot rows of a page and the cursor that says
 *    they were received are written in one transaction. The cursor can therefore
 *    never be ahead of the data: a crash mid-page rolls back both, and the page
 *    is simply fetched again.
 */

import type { OperationEnvelope } from "@/api/operation";
import type { ServiceIntent } from "@/api/service";
import type { SyncDatabase } from "./db";
import type {
  IssueEntry,
  MetaKey,
  MetaShape,
  OperationContext,
  OutboxEntry,
  SnapshotEntity,
  SnapshotRow,
  SyncChange,
  SyncOperationError,
} from "./types";

const META_DEFAULTS: MetaShape = {
  sync_cursor: 0,
  feed_version: 0,
  last_synced_at: null,
  business_date: null,
  next_seq: 1,
  tenant_id: null,
  dashboard_read_at: null,
};

// --- meta -------------------------------------------------------------------

export async function getMeta<K extends MetaKey>(
  db: SyncDatabase,
  key: K,
): Promise<MetaShape[K]> {
  const row = await db.get("meta", key);
  return (row === undefined ? META_DEFAULTS[key] : (row.value as MetaShape[K]));
}

export async function setMeta<K extends MetaKey>(
  db: SyncDatabase,
  key: K,
  value: MetaShape[K],
): Promise<void> {
  await db.put("meta", { key, value });
}

// --- outbox -----------------------------------------------------------------

/**
 * Write the operation to durable storage **before** anything is attempted.
 *
 * SYN-5. This resolving is the point at which the entry is safe; the network
 * call is a separate, later, retryable act. Nothing else in the write path is
 * allowed to run first.
 */
export async function enqueueOperation(
  db: SyncDatabase,
  envelope: OperationEnvelope<ServiceIntent>,
  context: OperationContext,
): Promise<OutboxEntry> {
  const tx = db.transaction(["outbox", "meta"], "readwrite");
  const metaStore = tx.objectStore("meta");
  const seqRow = await metaStore.get("next_seq");
  const seq = (seqRow?.value as number | undefined) ?? META_DEFAULTS.next_seq;

  const entry: OutboxEntry = {
    operation_id: envelope.operation_id,
    envelope,
    context,
    seq,
    attempt_count: 0,
    last_attempt_at: null,
    next_attempt_at: 0,
    last_error: null,
  };
  await tx.objectStore("outbox").put(entry);
  await metaStore.put({ key: "next_seq", value: seq + 1 });
  await tx.done;
  return entry;
}

/** Everything queued, in the order the person acted. */
export async function listOutbox(db: SyncDatabase): Promise<OutboxEntry[]> {
  return db.getAllFromIndex("outbox", "by_seq");
}

export async function countOutbox(db: SyncDatabase): Promise<number> {
  return db.count("outbox");
}

export async function removeFromOutbox(
  db: SyncDatabase,
  operationId: string,
): Promise<void> {
  await db.delete("outbox", operationId);
}

/**
 * A transport failure is not a verdict (P0 §7.3): the entry stays queued and is
 * only rescheduled.
 */
export async function recordAttemptFailure(
  db: SyncDatabase,
  operationIds: string[],
  error: SyncOperationError | null,
  backoffMs: (attempt: number) => number,
): Promise<void> {
  const tx = db.transaction("outbox", "readwrite");
  const store = tx.objectStore("outbox");
  const now = Date.now();
  for (const id of operationIds) {
    const entry = await store.get(id);
    if (!entry) continue;
    const attempt_count = entry.attempt_count + 1;
    await store.put({
      ...entry,
      attempt_count,
      last_attempt_at: new Date(now).toISOString(),
      next_attempt_at: now + backoffMs(attempt_count),
      last_error: error,
    });
  }
  await tx.done;
}

// --- issues -----------------------------------------------------------------

/** SYN-6: one transaction, so the operation is never in neither store. */
export async function promoteToIssue(
  db: SyncDatabase,
  entry: OutboxEntry,
  issue: Omit<IssueEntry, "operation_id" | "envelope" | "context" | "created_at">,
): Promise<void> {
  const tx = db.transaction(["outbox", "issues", "snapshot"], "readwrite");
  await tx.objectStore("issues").put({
    operation_id: entry.operation_id,
    envelope: entry.envelope,
    context: entry.context,
    created_at: new Date().toISOString(),
    ...issue,
  });
  await tx.objectStore("outbox").delete(entry.operation_id);
  // A conflict comes with the server's own record for that customer and date.
  // Storing it is what turns "still to do" into "done" on the round: the work
  // *is* recorded, just not by this device's operation.
  if (issue.server_state) {
    await tx.objectStore("snapshot").put({
      key: snapshotKey("daily_service_record", issue.server_state.id),
      entity: "daily_service_record",
      id: issue.server_state.id,
      row_version: issue.server_state.row_version,
      data: issue.server_state,
    });
  }
  await tx.done;
}

/**
 * An accepted operation leaves the queue and its record enters the snapshot, in
 * one transaction.
 *
 * The record written here is the server's own serialization, returned by the
 * push — not something the client assembled. Waiting for the change feed to
 * deliver it instead would leave a customer showing as "still to do" for a beat
 * after they were confirmed, which is exactly how somebody gets recorded twice.
 */
export async function settleOperation(
  db: SyncDatabase,
  operationId: string,
  record: { id: string; row_version: number } | null,
): Promise<void> {
  const tx = db.transaction(["outbox", "snapshot"], "readwrite");
  await tx.objectStore("outbox").delete(operationId);
  if (record) {
    await tx.objectStore("snapshot").put({
      key: snapshotKey("daily_service_record", record.id),
      entity: "daily_service_record",
      id: record.id,
      row_version: record.row_version,
      data: record,
    });
  }
  await tx.done;
}

export async function listIssues(db: SyncDatabase): Promise<IssueEntry[]> {
  return db.getAllFromIndex("issues", "by_created_at");
}

export async function countUnresolvedIssues(db: SyncDatabase): Promise<number> {
  const all = await db.getAll("issues");
  return all.filter((issue) => issue.resolved_at === null).length;
}

/**
 * The only way an issue stops raising Needs Attention (SYN-12).
 *
 * The entry is *kept*, marked with when a person reviewed it. Deleting it would
 * make the review indistinguishable from the entry never having existed, and
 * would leave no trace of a conflict somebody decided to accept.
 */
export async function resolveIssue(
  db: SyncDatabase,
  operationId: string,
): Promise<void> {
  const tx = db.transaction("issues", "readwrite");
  const store = tx.objectStore("issues");
  const issue = await store.get(operationId);
  if (issue && issue.resolved_at === null) {
    await store.put({ ...issue, resolved_at: new Date().toISOString() });
  }
  await tx.done;
}

// --- snapshot ---------------------------------------------------------------

export function snapshotKey(entity: SnapshotEntity, id: string): string {
  return `${entity}:${id}`;
}

export async function readSnapshot<T>(
  db: SyncDatabase,
  entity: SnapshotEntity,
): Promise<T[]> {
  const rows = await db.getAllFromIndex("snapshot", "by_entity", entity);
  return rows.map((row) => row.data as T);
}

export async function readSnapshotRow<T>(
  db: SyncDatabase,
  entity: SnapshotEntity,
  id: string,
): Promise<T | null> {
  const row = await db.get("snapshot", snapshotKey(entity, id));
  return row ? (row.data as T) : null;
}

export interface SnapshotWrite {
  entity: SnapshotEntity;
  id: string;
  row_version: number;
  data: unknown;
}

/**
 * Entities the client stores from the feed.
 *
 * `tenant` is deliberately absent. Its configuration reaches the device by the
 * ordinary `GET /tenant/settings` read on every sync, which is strictly fresher:
 * the business date changes daily without the tenant *row* changing, so waiting
 * for a `row_version` bump would leave a device believing in yesterday while it
 * is online. One writer for that row, and it is the direct read.
 */
const FEED_ENTITIES: readonly string[] = [
  "customer",
  "daily_service_record",
  // P6. Financial history the owner's screens render — and only render: every
  // figure on them is one the server computed (SYN-9).
  "payment",
  "statement",
];

function toWrite(change: SyncChange): SnapshotWrite | null {
  if (!FEED_ENTITIES.includes(change.entity)) return null;
  return {
    entity: change.entity as SnapshotEntity,
    id: change.id,
    row_version: change.row_version,
    data: change.data,
  };
}

export interface ApplyOptions {
  /**
   * Which service dates are worth keeping on the device — in P5, the business
   * date the server most recently stated, because that is the one day the
   * register renders.
   *
   * Rows outside it are *seen* — the cursor still advances past them — and not
   * stored, so nothing claims to be offline-available that is not. Holding every
   * record a tenant ever produced would also make the first sync of a year-old
   * business a download nobody asked for.
   */
  retainServiceDates?: (serviceDate: string) => boolean;
}

/**
 * Apply one page of changes and advance the cursor, atomically.
 *
 * A row already at or above the incoming `row_version` is left alone, so
 * replaying an older cursor cannot move the snapshot backwards.
 */
export async function applyChanges(
  db: SyncDatabase,
  changes: SyncChange[],
  cursor: number,
  extraMeta: Partial<MetaShape> = {},
  options: ApplyOptions = {},
): Promise<void> {
  const tx = db.transaction(["snapshot", "meta"], "readwrite");
  const snapshot = tx.objectStore("snapshot");
  for (const change of changes) {
    const write = toWrite(change);
    if (!write) continue;
    if (
      write.entity === "daily_service_record" &&
      options.retainServiceDates &&
      !options.retainServiceDates(
        (write.data as { service_date: string }).service_date,
      )
    ) {
      continue;
    }
    const key = snapshotKey(write.entity, write.id);
    const existing = await snapshot.get(key);
    if (existing && existing.row_version > write.row_version) continue;
    await snapshot.put({ key, ...write });
  }

  const meta = tx.objectStore("meta");
  await meta.put({ key: "sync_cursor", value: cursor });
  for (const [key, value] of Object.entries(extraMeta)) {
    await meta.put({ key, value: value as MetaShape[MetaKey] });
  }
  await tx.done;
}

/**
 * Store one server-computed document under its own key.
 *
 * Used for the dashboard summary: not a feed entity, but a server-authoritative
 * read the device keeps so that opening the dashboard offline shows the last
 * known figures with an "as of" stamp instead of nothing. It is written
 * verbatim — the client never recomputes any part of it.
 */
export async function writeSnapshotDoc(
  db: SyncDatabase,
  entity: SnapshotEntity,
  id: string,
  data: unknown,
  extraMeta: Partial<MetaShape> = {},
): Promise<void> {
  const tx = db.transaction(["snapshot", "meta"], "readwrite");
  await tx.objectStore("snapshot").put({
    key: snapshotKey(entity, id),
    entity,
    id,
    row_version: 0,
    data,
  });
  const meta = tx.objectStore("meta");
  for (const [key, value] of Object.entries(extraMeta)) {
    await meta.put({ key, value: value as MetaShape[MetaKey] });
  }
  await tx.done;
}

/**
 * Write the tenant's configuration and the meta that goes with it, atomically.
 *
 * Unconditional: this row has one writer, and what it writes is always the most
 * recent authoritative read.
 */
export async function writeTenantSettings(
  db: SyncDatabase,
  tenantId: string,
  settings: unknown,
  extraMeta: Partial<MetaShape> = {},
): Promise<void> {
  const tx = db.transaction(["snapshot", "meta"], "readwrite");
  await tx.objectStore("snapshot").put({
    key: snapshotKey("tenant", tenantId),
    entity: "tenant",
    id: tenantId,
    row_version: 0,
    data: settings,
  });
  const meta = tx.objectStore("meta");
  for (const [key, value] of Object.entries(extraMeta)) {
    await meta.put({ key, value: value as MetaShape[MetaKey] });
  }
  await tx.done;
}

/** Seed the snapshot from the ordinary read routes, with its starting cursor. */
export async function seedSnapshot(
  db: SyncDatabase,
  rows: SnapshotWrite[],
  cursor: number,
  extraMeta: Partial<MetaShape> = {},
): Promise<void> {
  const tx = db.transaction(["snapshot", "meta"], "readwrite");
  const snapshot = tx.objectStore("snapshot");
  for (const row of rows) {
    await snapshot.put({ key: snapshotKey(row.entity, row.id), ...row });
  }
  const meta = tx.objectStore("meta");
  await meta.put({ key: "sync_cursor", value: cursor });
  for (const [key, value] of Object.entries(extraMeta)) {
    await meta.put({ key, value: value as MetaShape[MetaKey] });
  }
  await tx.done;
}

/** Drop stored service records the retention rule no longer keeps.

Service records only. `payment` and `statement` are deliberately **not** pruned:
they are not a rolling day-view, they are the financial history the customer and
statement screens render, and dropping older ones would invent a retention
horizon nobody chose (the defect D6 removed in P5). They are also far fewer —
one statement per customer per cycle, and one row per payment.

Snapshot-only: it touches neither `outbox` nor `issues`, which are not caches.
Unresolved work is never collateral of a cache cleanup. */
export async function pruneServiceRecords(
  db: SyncDatabase,
  keep: (serviceDate: string) => boolean,
): Promise<void> {
  const tx = db.transaction("snapshot", "readwrite");
  const store = tx.objectStore("snapshot");
  const rows = await store.index("by_entity").getAll("daily_service_record");
  for (const row of rows) {
    const { service_date } = row.data as { service_date: string };
    if (!keep(service_date)) await store.delete(row.key);
  }
  await tx.done;
}

/**
 * Forget every cached read and start the feed again.
 *
 * Used when the server reports a different `feed_version` — the feed now carries
 * entities this device has already scrolled past, and only a resync from zero
 * can deliver their older rows. The outbox and the issues store are deliberately
 * **not** touched: neither is a cache.
 */
export async function resetSnapshot(db: SyncDatabase): Promise<void> {
  const tx = db.transaction(["snapshot", "meta"], "readwrite");
  await tx.objectStore("snapshot").clear();
  await tx.objectStore("meta").put({ key: "sync_cursor", value: 0 });
  await tx.done;
}

export type { SnapshotRow };
