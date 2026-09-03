import { Link } from "react-router-dom";

import { useSync } from "./SyncProvider";

/**
 * The six frozen sync states (P0 §7.5), in one line, in the app frame.
 *
 * `Synced` · `Offline` · `Last synced <time>` · `N changes waiting` · `Syncing`
 * · `Needs Attention`.
 *
 * Needs Attention is shown *beside* the current state rather than instead of it,
 * because a device can perfectly well be syncing normally while an earlier
 * conflict is still waiting for a person. SYN-11 requires it to stay raised
 * across refreshes, restarts and later successful syncs of unrelated work — it
 * is driven by the `issues` count and by nothing else, so a successful sync
 * cannot clear it.
 *
 * Deliberately quiet: no attempt counters, no cursor, no error codes. The one
 * technical fact a person needs is how much work has not reached the server.
 */
export function SyncStatus() {
  const { online, syncing, pending, unresolved, lastSyncedAt, syncNow } = useSync();

  return (
    <div className="sync-status">
      {unresolved > 0 ? (
        <Link className="sync-chip sync-chip-attention" to="/attention">
          Needs Attention
          <span className="sync-count">{unresolved}</span>
        </Link>
      ) : null}

      <span className="sync-chip" role="status">
        {label({ online, syncing, pending, lastSyncedAt })}
      </span>

      {online && (pending > 0 || !syncing) ? (
        <button
          className="btn btn-quiet sync-action"
          type="button"
          onClick={() => void syncNow()}
          disabled={syncing}
        >
          Sync now
        </button>
      ) : null}
    </div>
  );
}

function label({
  online,
  syncing,
  pending,
  lastSyncedAt,
}: {
  online: boolean;
  syncing: boolean;
  pending: number;
  lastSyncedAt: string | null;
}): string {
  if (syncing) return "Syncing";
  if (pending > 0) {
    const waiting = `${pending} ${pending === 1 ? "change" : "changes"} waiting`;
    return online ? waiting : `Offline · ${waiting}`;
  }
  if (!online) return "Offline";
  if (lastSyncedAt) return `Synced · Last synced ${timeOf(lastSyncedAt)}`;
  return "Synced";
}

/** Display only. The clock here decides nothing — no business date comes from it. */
function timeOf(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  return at.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}
