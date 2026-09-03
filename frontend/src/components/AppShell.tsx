import { NavLink, Outlet } from "react-router-dom";

import { useAuth } from "@/auth/AuthContext";
import { SyncStatus } from "@/sync/SyncStatus";
import { useSync } from "@/sync/SyncProvider";

/**
 * The authenticated frame: the destinations, and a way out.
 *
 * Navigation is a bottom bar on a phone and a top bar on a wider screen (see
 * `styles.css`) — the daily register is used one-handed while standing at a door,
 * so its controls belong within thumb reach.
 *
 * P6 adds three owner-facing destinations. They are ordered by how often they
 * are opened, not by importance: Today first, because that is the round;
 * Overview and Customers next; Statements and Running costs after, since they
 * are looked at monthly. Running costs is deliberately worded as a business
 * expense and sits nowhere near anything a customer owes.
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
        <NavLink to="/overview" className={navClass}>
          Overview
        </NavLink>
        <NavLink to="/customers" className={navClass}>
          Customers
        </NavLink>
        <NavLink to="/statements" className={navClass}>
          Statements
        </NavLink>
        <NavLink to="/operating-costs" className={navClass}>
          Costs
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
