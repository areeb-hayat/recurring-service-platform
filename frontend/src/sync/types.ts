/**
 * What the four IndexedDB stores hold (P0 §7.1).
 *
 * Everything here is a plain serialisable value: IndexedDB stores structured
 * clones, and a record that survives a browser restart cannot contain a class
 * instance, a function or a `Date` we would then have to trust.
 */

import type { OperationEnvelope } from "@/api/operation";
import type { ServiceIntent } from "@/api/service";
import type { Customer, ServiceRecord, TenantSettings } from "@/api/types";

/** The verdicts the server may return for one operation (P0 §7.3). */
export type SyncVerdict = "APPLIED" | "DUPLICATE" | "REJECTED" | "CONFLICT";

export interface SyncOperationError {
  code: string;
  detail: string;
  field_errors?: Record<string, string>;
}

/** One element of `POST /sync/operations`'s `results`. */
export interface SyncOperationResult {
  operation_id: string;
  status: SyncVerdict;
  entity?: ServiceRecord;
  error?: SyncOperationError;
  server_state?: ServiceRecord;
}

export interface SyncPushResponse {
  results: SyncOperationResult[];
}

export interface SyncChange {
  entity: string;
  id: string;
  row_version: number;
  data: unknown;
}

export interface SyncChangesResponse {
  since: number;
  cursor: number;
  has_more: boolean;
  head: number;
  feed_version: number;
  entities: string[];
  changes: SyncChange[];
}

/**
 * Enough about the intent to describe it to a person without the network.
 *
 * Needs Attention has to read sensibly on a device that has been offline for a
 * day: "Ayesha Khan · 2 bottle · Saturday" needs the customer's *name*, and a
 * name lives in the snapshot, which a later resync may legitimately change. The
 * context is captured when the operation is created so the issue always shows
 * what the person actually did.
 */
export interface OperationContext {
  customer_id: string;
  customer_name: string;
  service_date: string;
  kind: "SERVICE" | "SKIP";
  quantity: string | null;
  unit_label: string;
}

export interface OutboxEntry {
  operation_id: string;
  /** The P0 §7.2 envelope, byte-for-byte what gets sent on every attempt. */
  envelope: OperationEnvelope<ServiceIntent>;
  context: OperationContext;
  /** Creation order, so the queue is pushed in the order the person acted. */
  seq: number;
  attempt_count: number;
  last_attempt_at: string | null;
  /** Epoch ms. Bounded backoff; nothing is retried before this. */
  next_attempt_at: number;
  last_error: SyncOperationError | null;
}

export interface IssueEntry {
  operation_id: string;
  envelope: OperationEnvelope<ServiceIntent>;
  context: OperationContext;
  verdict: "REJECTED" | "CONFLICT";
  error: SyncOperationError;
  /** The server's authoritative state for a conflict, where it supplied one. */
  server_state: ServiceRecord | null;
  created_at: string;
  /** Null until a person reviews it. Nothing else ever sets this. */
  resolved_at: string | null;
}

export type SnapshotEntity = "tenant" | "customer" | "daily_service_record";

export interface SnapshotRow {
  /** `${entity}:${id}` — one key space, one store. */
  key: string;
  entity: SnapshotEntity;
  id: string;
  row_version: number;
  data: unknown;
}

export interface SnapshotTenantRow extends SnapshotRow {
  entity: "tenant";
  data: TenantSettings;
}

export interface SnapshotCustomerRow extends SnapshotRow {
  entity: "customer";
  data: Customer;
}

export interface SnapshotRecordRow extends SnapshotRow {
  entity: "daily_service_record";
  data: ServiceRecord;
}

/** `meta` is a small key/value store; these are its keys. */
export interface MetaShape {
  sync_cursor: number;
  feed_version: number;
  last_synced_at: string | null;
  /** The last business date the *server* stated. Never derived from the device. */
  business_date: string | null;
  /** Monotonic counter behind `OutboxEntry.seq`. */
  next_seq: number;
  /** Which tenant this database belongs to — a tripwire, not an authority. */
  tenant_id: string | null;
}

export type MetaKey = keyof MetaShape;
