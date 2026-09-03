import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { EmptyState, Loading } from "@/components/Feedback";
import { useLocalData } from "@/sync/useLocalData";

/**
 * Everyone on the books.
 *
 * The search box filters the rows already loaded. It is a display filter, not a
 * query: the backend has no customer name search in V1 (P0 §12.1 puts that behind
 * the search package), and P0 §7.1 says local search runs against `snapshot`
 * only. Filtering is only honest because the snapshot holds *every* customer —
 * the sync engine seeds it by walking `GET /customers` to the end and then keeps
 * it current from the change feed — so the filter is over everyone, not over a
 * first page.
 */
export function CustomerListPage() {
  const [term, setTerm] = useState("");
  const { customers, loading, unavailable } = useLocalData();

  const rows = useMemo(() => {
    const items = [...customers].sort(
      (a, b) => a.name.localeCompare(b.name) || a.id.localeCompare(b.id),
    );
    const needle = term.trim().toLowerCase();
    if (!needle) return items;
    return items.filter(
      (c) =>
        c.name.toLowerCase().includes(needle) ||
        c.code.toLowerCase().includes(needle) ||
        (c.area ?? "").toLowerCase().includes(needle),
    );
  }, [customers, term]);

  return (
    <div className="stack">
      <header className="day-header">
        <h1 className="day-title">Customers</h1>
        <Link className="btn btn-primary" to="/customers/new">
          Add customer
        </Link>
      </header>

      <label className="field">
        <span>Search this list</span>
        <input
          type="search"
          value={term}
          placeholder="Name, code or area"
          onChange={(e) => setTerm(e.target.value)}
        />
      </label>

      {loading ? <Loading label="Loading customers…" /> : null}
      {unavailable ? (
        <p className="notice notice-error" role="alert">
          Unavailable offline. This device has not synchronised yet — connect once
          and the customer list will be available without a network.
        </p>
      ) : null}

      {!loading && !unavailable && rows.length === 0 ? (
        <EmptyState>
          {term ? "Nobody here matches that." : "No customers yet. Add the first one."}
        </EmptyState>
      ) : null}

      <ul className="list">
        {rows.map((customer) => (
          <li key={customer.id}>
            <Link className="row" to={`/customers/${customer.id}`}>
              <span className="row-main">
                {customer.name}
                {customer.status === "INACTIVE" ? (
                  <span className="badge">Inactive</span>
                ) : null}
              </span>
              <span className="row-meta">
                {customer.code}
                {customer.area ? ` · ${customer.area}` : ""}
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}
