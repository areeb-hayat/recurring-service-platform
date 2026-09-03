import { NavLink, Outlet } from "react-router-dom";

import { useAuth } from "@/auth/AuthContext";
import { SyncStatus } from "@/sync/SyncStatus";
import { useSync } from "@/sync/SyncProvider";

/**
 * The authenticated frame: two destinations and a way out.
 *
 * Navigation is a bottom bar on a phone and a top bar on a wider screen (see
 * `styles.css`) — the daily register is used one-handed while standing at a door,
 * so its controls belong within thumb reach.
 *
 * The sync status sits in the frame rather than on one screen, because P0 §7.5
 * requires it to be visible wherever the person is. Needs Attention adds a
 * destination to the navigation only while something is actually waiting: an
 * always-present tab for an empty list is noise on a phone.
 */
export function AppShell() {
  const { signOut } = useAuth();
  const { unresolved } = useSync();

  return (
    <div className="shell">
      <header className="shell-header">
        <span className="shell-title">Daily Register</span>
        <SyncStatus />
        <button className="btn btn-quiet" type="button" onClick={() => void signOut()}>
          Sign out
        </button>
      </header>

      <main id="main" className="shell-main">
        <Outlet />
      </main>

      <nav className="shell-nav" aria-label="Main">
        <NavLink to="/today" className={navClass}>
          Today
        </NavLink>
        <NavLink to="/customers" className={navClass}>
          Customers
        </NavLink>
        {unresolved > 0 ? (
          <NavLink to="/attention" className={navClass}>
            Attention
            <span className="sync-count">{unresolved}</span>
          </NavLink>
        ) : null}
      </nav>
    </div>
  );
}

function navClass({ isActive }: { isActive: boolean }): string {
  return isActive ? "shell-nav-link is-active" : "shell-nav-link";
}
