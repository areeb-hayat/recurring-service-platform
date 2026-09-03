/**
 * The IndexedDB handle: four stores, one per P0 §7.1 role, and one database per
 * tenant.
 *
 * **Why per-tenant databases.** Two people can sign in on the same browser, and
 * P0 §7.1 says `meta` is cleared on sign-out — but it does not say what happens
 * to work that is still queued when somebody signs out mid-round. Deleting the
 * outbox to protect the next tenant's privacy would throw away accepted human
 * intent, which SYN-5/SYN-12 exist to prevent; keeping one shared database would
 * show tenant A's customers to tenant B. Naming the database after the tenant
 * settles both at once: signing out closes the handle and touches no data, and a
 * different tenant simply opens a different database it cannot see past. Nothing
 * is deleted on sign-out, so signing back in resumes exactly where the round
 * stopped.
 *
 * The tenant id here is a *namespace*, never an authority: every request is
 * still scoped server-side by the bearer token, and the client never sends a
 * `tenant_id` (SEC-3).
 */

import { openDB, type DBSchema, type IDBPDatabase } from "idb";

import type { IssueEntry, MetaShape, OutboxEntry, SnapshotRow } from "./types";

export const DB_VERSION = 1;

export interface SyncDB extends DBSchema {
  outbox: {
    key: string; // operation_id
    value: OutboxEntry;
    indexes: { by_seq: number };
  };
  issues: {
    key: string; // operation_id
    value: IssueEntry;
    indexes: { by_created_at: string };
  };
  snapshot: {
    key: string; // `${entity}:${id}`
    value: SnapshotRow;
    indexes: { by_entity: string };
  };
  meta: {
    key: string;
    value: { key: string; value: MetaShape[keyof MetaShape] };
  };
}

export type SyncDatabase = IDBPDatabase<SyncDB>;

export function databaseName(tenantId: string): string {
  return `rsp-sync-v1-${tenantId}`;
}

const open = new Map<string, Promise<SyncDatabase>>();

/** Open (and cache) this tenant's database. Safe to call on every access. */
export function openSyncDb(tenantId: string): Promise<SyncDatabase> {
  const name = databaseName(tenantId);
  let handle = open.get(name);
  if (!handle) {
    handle = openDB<SyncDB>(name, DB_VERSION, {
      upgrade(db) {
        const outbox = db.createObjectStore("outbox", { keyPath: "operation_id" });
        outbox.createIndex("by_seq", "seq");

        const issues = db.createObjectStore("issues", { keyPath: "operation_id" });
        issues.createIndex("by_created_at", "created_at");

        const snapshot = db.createObjectStore("snapshot", { keyPath: "key" });
        snapshot.createIndex("by_entity", "entity");

        db.createObjectStore("meta", { keyPath: "key" });
      },
    });
    open.set(name, handle);
  }
  return handle;
}

/** Drop the cached handle (tests, and signing out). Deletes nothing. */
export function closeSyncDb(tenantId: string): void {
  const name = databaseName(tenantId);
  const handle = open.get(name);
  open.delete(name);
  void handle?.then((db) => db.close()).catch(() => undefined);
}

export function resetSyncDbCache(): void {
  open.clear();
}
