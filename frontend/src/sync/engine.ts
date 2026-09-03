/**
 * The sync engine: one write path, bounded retry, and a cursor that never
 * outruns its data.
 *
 * **One write path (P0 §7.6).** CONFIRM and SKIP do not have an online version
 * and an offline version. Every tap does the same three things in the same
 * order:
 *
 *     generate the operation_id once  ->  write the envelope to `outbox`
 *                                     ->  sync if the network is there
 *
 * The entry is durable before the first attempt, so an apparently-online action
 * survives a lost response, a closed tab or a pulled plug exactly as an offline
 * one does. A fetch having been *attempted* removes nothing; only a verdict does.
 *
 * **What moves an entry out of the queue** (SYN-6): `APPLIED` and `DUPLICATE`
 * delete it; `REJECTED` and `CONFLICT` move it into `issues` in one local
 * transaction. A network error, a timeout or a 5xx is not a verdict and leaves
 * the entry queued with a longer backoff.
 *
 * **Authentication never costs work.** A 401 is refreshed and replayed inside
 * `api/client` with the identical body. If the refresh fails, the session ends
 * and the push stops — with the outbox, the issues and the snapshot untouched,
 * waiting for whoever signs in next.
 */

import { ApiError } from "@/api/errors";
import type { OperationEnvelope } from "@/api/operation";
import type { ServiceIntent } from "@/api/service";
import { listAllCustomers } from "@/api/customers";
import { getDay } from "@/api/service";
import { getTenantSettings } from "@/api/tenant";
import type { Customer, ServiceRecord, TenantSettings } from "@/api/types";
import { getChanges, pushOperations } from "./api";
import { closeSyncDb, openSyncDb, type SyncDatabase } from "./db";
import {
  applyChanges,
  countOutbox,
  countUnresolvedIssues,
  enqueueOperation,
  getMeta,
  listOutbox,
  pruneServiceRecords,
  recordAttemptFailure,
  promoteToIssue,
  settleOperation,
  resetSnapshot,
  resolveIssue as resolveIssueRow,
  seedSnapshot,
  setMeta,
  writeTenantSettings,
} from "./stores";
import type {
  OperationContext,
  OutboxEntry,
  SyncOperationResult,
} from "./types";

/** How many operations travel in one push. The server caps the batch at 200. */
export const PUSH_BATCH_SIZE = 50;
const CHANGES_PAGE_SIZE = 500;
const MAX_PULL_PAGES = 200;

const BASE_BACKOFF_MS = 2_000;
const MAX_BACKOFF_MS = 5 * 60_000;

/** Bounded exponential backoff. Never zero, never unbounded, never a busy loop. */
export function backoffMs(attempt: number): number {
  const exponent = Math.min(attempt, 10);
  return Math.min(BASE_BACKOFF_MS * 2 ** (exponent - 1), MAX_BACKOFF_MS);
}

export interface SyncState {
  /** Whether the browser believes it has a network. */
  online: boolean;
  syncing: boolean;
  /** `outbox` count — the "N changes waiting" badge (SYN-11). */
  pending: number;
  /** Unresolved `issues` count — Needs Attention (SYN-11, SYN-12). */
  unresolved: number;
  lastSyncedAt: string | null;
  /** The last business date the *server* stated. Never the device's opinion. */
  businessDate: string | null;
  /** True once the snapshot holds enough to render the register offline. */
  hydrated: boolean;
  /** Bumped whenever local data changed, so readers know to re-read. */
  revision: number;
}

const INITIAL: SyncState = {
  online: true,
  syncing: false,
  pending: 0,
  unresolved: 0,
  lastSyncedAt: null,
  businessDate: null,
  hydrated: false,
  revision: 0,
};

type Listener = (state: SyncState) => void;

function isOnline(): boolean {
  return typeof navigator === "undefined" || navigator.onLine !== false;
}

/**
 * Which service records this device keeps.
 *
 * Exactly the ones the P5 UI renders: the round for the business date the server
 * most recently stated. No N-day window — a retention horizon in days would be a
 * product policy nobody has asked for, and the only screen that reads these rows
 * reads one day of them.
 *
 * An offline device keeps using its cached business date, so its current round
 * stays available for as long as it stays offline; the rule only bites when a
 * pull brings a newer date back from the server.
 *
 * Records for other dates are still *seen* — the cursor advances past them
 * normally, so nothing is skipped — they are simply not stored, because storing
 * them would claim an offline availability no screen offers. Needs Attention
 * needs none of them: an issue carries its own intent and the server_state it
 * was given.
 */
function retainedFor(businessDate: string): (serviceDate: string) => boolean {
  return (serviceDate) => serviceDate === businessDate;
}

export class SyncEngine {
  readonly tenantId: string;
  private state: SyncState = { ...INITIAL, online: isOnline() };
  /**
   * The browser's own claim, and what the network actually did.
   *
   * `navigator.onLine` means "there is an interface", not "the server can be
   * reached" — it is `true` on a captive-portal wifi, on a dead uplink, and in
   * more than one automation environment. So the status line reports the *and*
   * of the two: the browser thinks it is connected **and** the last attempt got
   * through. Retrying is still driven by `navigator.onLine` alone, because
   * "unreachable" is exactly the state a retry exists to leave.
   */
  private navigatorOnline = isOnline();
  private reachable = true;
  private listeners = new Set<Listener>();
  private handle: Promise<SyncDatabase> | null = null;
  private inFlight: Promise<void> | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private stopped = false;

  constructor(tenantId: string) {
    this.tenantId = tenantId;
  }

  // --- lifecycle ------------------------------------------------------------

  db(): Promise<SyncDatabase> {
    this.handle ??= openSyncDb(this.tenantId);
    return this.handle;
  }

  async start(): Promise<void> {
    this.stopped = false;
    // Re-read the browser's own view of the network. A page that *loads* while
    // offline fires no `offline` event, so a flag captured when the engine was
    // constructed would claim a connection that is not there.
    this.navigatorOnline = isOnline();
    this.publishReachability();
    const db = await this.db();
    await setMeta(db, "tenant_id", this.tenantId);
    await this.refreshCounts();
    void this.syncNow();
  }

  stop(): void {
    this.stopped = true;
    if (this.retryTimer !== null) clearTimeout(this.retryTimer);
    this.retryTimer = null;
    this.handle = null;
    // Closing the handle, never deleting the data: queued work and unresolved
    // issues belong to the business, not to the session that happened to create
    // them.
    closeSyncDb(this.tenantId);
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    listener(this.state);
    return () => this.listeners.delete(listener);
  }

  getState(): SyncState {
    return this.state;
  }

  setOnline(online: boolean): void {
    this.navigatorOnline = online;
    // Coming back is worth an optimistic attempt; that attempt decides the rest.
    if (online) this.reachable = true;
    this.publishReachability();
    if (online) void this.syncNow();
  }

  private publishReachability(): void {
    this.patch({ online: this.navigatorOnline && this.reachable });
  }

  private markReachable(reachable: boolean): void {
    this.reachable = reachable;
    this.publishReachability();
  }

  private patch(next: Partial<SyncState>): void {
    this.state = { ...this.state, ...next };
    for (const listener of this.listeners) listener(this.state);
  }

  private async refreshCounts(bumpRevision = false): Promise<void> {
    const db = await this.db();
    const [pending, unresolved, lastSyncedAt, businessDate] = await Promise.all([
      countOutbox(db),
      countUnresolvedIssues(db),
      getMeta(db, "last_synced_at"),
      getMeta(db, "business_date"),
    ]);
    this.patch({
      pending,
      unresolved,
      lastSyncedAt,
      businessDate,
      hydrated: businessDate !== null,
      revision: bumpRevision ? this.state.revision + 1 : this.state.revision,
    });
  }

  // --- writes ---------------------------------------------------------------

  /**
   * Queue one operation, then try to send it.
   *
   * The `await` on the durable write is the guarantee: the promise this returns
   * does not resolve until the envelope is in IndexedDB, and only then is the
   * network involved.
   */
  async enqueue(
    envelope: OperationEnvelope<ServiceIntent>,
    context: OperationContext,
  ): Promise<OutboxEntry> {
    const db = await this.db();
    const entry = await enqueueOperation(db, envelope, context);
    await this.refreshCounts(true);
    if (this.navigatorOnline) void this.syncNow();
    return entry;
  }

  async resolveIssue(operationId: string): Promise<void> {
    const db = await this.db();
    await resolveIssueRow(db, operationId);
    await this.refreshCounts(true);
  }

  // --- the cycle ------------------------------------------------------------

  /** Push then pull. Concurrent calls join the run already in progress. */
  syncNow(): Promise<void> {
    if (this.inFlight) return this.inFlight;
    this.inFlight = this.run().finally(() => {
      this.inFlight = null;
    });
    return this.inFlight;
  }

  private async run(): Promise<void> {
    // Driven by the browser's flag, not by the effective one: an unreachable
    // server is precisely what a retry is for.
    if (this.stopped || !this.navigatorOnline) return;
    this.patch({ syncing: true });
    try {
      // A queue that is merely waiting out its backoff must not hold the pull
      // hostage: the register would go stale for as long as one entry keeps
      // failing. Only a push that could not reach the server at all skips it,
      // because a pull would not reach it either.
      if ((await this.push()) !== "failed") {
        await this.pull();
        this.markReachable(true);
      }
    } catch (error) {
      if (error instanceof ApiError && error.kind === "AUTH") {
        // The session ended. Everything queued stays queued.
        return;
      }
      this.markReachable(false);
      this.scheduleRetry(backoffMs(1));
    } finally {
      this.patch({ syncing: false });
      await this.refreshCounts(true);
    }
  }

  /** How the push ended: drained, waiting out a backoff, or unreachable. */
  private async push(): Promise<"drained" | "deferred" | "failed"> {
    const db = await this.db();
    for (;;) {
      const queued = await listOutbox(db);
      const now = Date.now();
      const ready = queued.filter((entry) => entry.next_attempt_at <= now);
      if (ready.length === 0) {
        if (queued.length > 0) {
          const soonest = Math.min(...queued.map((e) => e.next_attempt_at));
          this.scheduleRetry(Math.max(soonest - now, 1_000));
          return "deferred";
        }
        return "drained";
      }

      const batch = ready.slice(0, PUSH_BATCH_SIZE);
      let results: SyncOperationResult[];
      try {
        results = (await pushOperations(batch.map((entry) => entry.envelope))).results;
      } catch (error) {
        if (error instanceof ApiError && error.kind === "AUTH") throw error;
        const failure =
          error instanceof ApiError
            ? { code: error.code, detail: error.detail }
            : { code: "NETWORK", detail: "network error" };
        // Not a verdict: keep every entry and try again later (P0 §7.3).
        await recordAttemptFailure(
          db,
          batch.map((entry) => entry.operation_id),
          failure,
          backoffMs,
        );
        const attempt = Math.min(...batch.map((e) => e.attempt_count)) + 1;
        this.markReachable(false);
        this.scheduleRetry(backoffMs(attempt));
        return "failed";
      }

      const byId = new Map(batch.map((entry) => [entry.operation_id, entry]));
      for (const result of results) {
        const entry = byId.get(result.operation_id);
        if (!entry) continue;
        await this.applyVerdict(db, entry, result);
      }
      await this.refreshCounts(true);

      if (batch.length === ready.length) return "drained";
    }
  }

  private async applyVerdict(
    db: SyncDatabase,
    entry: OutboxEntry,
    result: SyncOperationResult,
  ): Promise<void> {
    if (result.status === "APPLIED" || result.status === "DUPLICATE") {
      await settleOperation(db, entry.operation_id, result.entity ?? null);
      return;
    }
    // REJECTED / CONFLICT: terminal for automatic retry, and never silently
    // dropped. One transaction moves it from the queue to Needs Attention, so
    // there is no instant in which it exists in neither (SYN-6).
    await promoteToIssue(db, entry, {
      verdict: result.status,
      error: result.error ?? { code: "UNKNOWN", detail: "no error was returned" },
      server_state: result.server_state ?? null,
      resolved_at: null,
    });
  }

  private async pull(): Promise<void> {
    const db = await this.db();

    // The business date is the server's, and it changes daily without any row
    // changing — so it is read directly rather than waited for on the feed.
    const settings: TenantSettings = await getTenantSettings();

    let cursor = await getMeta(db, "sync_cursor");
    const storedFeedVersion = await getMeta(db, "feed_version");

    // A device that has never synchronised only needs the feed's head and its
    // version, not a page of rows it is about to replace by seeding.
    let page = await getChanges(cursor, cursor === 0 ? 1 : CHANGES_PAGE_SIZE);
    const feedVersion = page.feed_version;

    if (storedFeedVersion !== 0 && storedFeedVersion !== feedVersion) {
      // The feed now carries entities this device has already scrolled past.
      // Only a resync from zero can deliver their older rows. The outbox and the
      // issues store are untouched — neither is a cache.
      await resetSnapshot(db);
      cursor = 0;
      page = await getChanges(0, 1);
    }

    const keep = retainedFor(settings.business_date);

    if (cursor === 0) {
      await this.seed(db, settings, page.head);
    } else {
      for (let i = 0; i < MAX_PULL_PAGES; i += 1) {
        await applyChanges(db, page.changes, page.cursor, {}, { retainServiceDates: keep });
        if (!page.has_more) break;
        page = await getChanges(page.cursor, CHANGES_PAGE_SIZE);
      }
    }

    await pruneServiceRecords(db, keep);
    await writeTenantSettings(db, this.tenantId, settings, {
      feed_version: feedVersion,
      business_date: settings.business_date,
      last_synced_at: new Date().toISOString(),
    });
  }

  /**
   * First sync on this device: seed from the ordinary read routes, continue from
   * the head that was read *before* them.
   *
   * Replaying the feed from zero would be correct but would hand a year-old
   * business every service record it has ever produced, to render one day.
   * Reading `head` first and seeding afterwards means anything written in
   * between has a higher version and still arrives on the feed — a superset,
   * never a gap (SYN-10).
   */
  private async seed(
    db: SyncDatabase,
    settings: TenantSettings,
    head: number,
  ): Promise<void> {
    const [customers, day] = await Promise.all([
      listAllCustomers({ status: "ACTIVE" }),
      getDay(settings.business_date),
    ]);
    await seedSnapshot(
      db,
      [
        { entity: "tenant", id: this.tenantId, row_version: 0, data: settings },
        ...customers.map((customer: Customer) => ({
          entity: "customer" as const,
          id: customer.id,
          row_version: customer.row_version,
          data: customer,
        })),
        ...day.items.map((record: ServiceRecord) => ({
          entity: "daily_service_record" as const,
          id: record.id,
          row_version: record.row_version,
          data: record,
        })),
      ],
      head,
    );
  }

  private scheduleRetry(delayMs: number): void {
    if (this.stopped || this.retryTimer !== null) return;
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      void this.syncNow();
    }, delayMs);
  }
}

// --- one engine per tenant, for the lifetime of the tab ----------------------

const engines = new Map<string, SyncEngine>();

export function engineFor(tenantId: string): SyncEngine {
  let engine = engines.get(tenantId);
  if (!engine) {
    engine = new SyncEngine(tenantId);
    engines.set(tenantId, engine);
  }
  return engine;
}

export function resetEngines(): void {
  for (const engine of engines.values()) engine.stop();
  engines.clear();
}
