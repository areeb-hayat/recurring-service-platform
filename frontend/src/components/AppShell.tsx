import { NavLink, Outlet } from "react-router-dom";

import { useAuth } from "@/auth/AuthContext";

/**
 * The authenticated frame: two destinations and a way out.
 *
 * Navigation is a bottom bar on a phone and a top bar on a wider screen (see
 * `styles.css`) — the daily register is used one-handed while standing at a door,
 * so its controls belong within thumb reach.
 */
export function AppShell() {
  const { signOut } = useAuth();

  return (
    <div className="shell">
      <header className="shell-header">
        <span className="shell-title">Daily Register</span>
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
      </nav>
    </div>
  );
}

function navClass({ isActive }: { isActive: boolean }): string {
  return isActive ? "shell-nav-link is-active" : "shell-nav-link";
}
