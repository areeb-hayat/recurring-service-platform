import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { listAllCustomers } from "@/api/customers";
import { messageFor } from "@/api/errors";
import { EmptyState, ErrorNotice, Loading } from "@/components/Feedback";

/**
 * Everyone on the books.
 *
 * The search box filters the rows already loaded. It is a display filter, not a
 * query: the backend has no customer name search in V1 (P0 §12.1 puts that behind
 * the search package). Filtering the loaded rows is only honest because the list
 * is loaded in full — `listAllCustomers` walks the pagination to its end — so the
 * filter is over everyone, not over a first page.
 */
export function CustomerListPage() {
  const [term, setTerm] = useState("");
  const query = useQuery({
    queryKey: ["customers", {}],
    queryFn: () => listAllCustomers(),
  });

  const rows = useMemo(() => {
    const items = query.data ?? [];
    const needle = term.trim().toLowerCase();
    if (!needle) return items;
    return items.filter(
      (c) =>
        c.name.toLowerCase().includes(needle) ||
        c.code.toLowerCase().includes(needle) ||
        (c.area ?? "").toLowerCase().includes(needle),
    );
  }, [query.data, term]);

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

      {query.isPending ? <Loading label="Loading customers…" /> : null}
      {query.isError ? (
        <ErrorNotice message={messageFor(query.error)} onRetry={() => void query.refetch()} />
      ) : null}

      {query.isSuccess && rows.length === 0 ? (
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
