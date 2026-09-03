import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { EmptyState, ErrorNotice, Loading } from "@/components/Feedback";
import { messageFor } from "@/api/errors";
import {
  CandidateList,
  CustomerSearchBox,
  SearchSourceNote,
} from "@/search/CustomerSearch";
import { useCustomerSearch } from "@/search/useCustomerSearch";
import { useLocalData } from "@/sync/useLocalData";

/**
 * Everyone on the books, and a way to find one of them.
 *
 * **P8 changed what the search box is.** It used to filter the rows already
 * loaded — a display filter that could only ever match a name, a code or an
 * area, and only in the spelling on the books. It is now the real thing: online
 * it asks `POST /search/customers`, which searches names, *the nicknames people
 * actually use*, codes, phone numbers and areas with the server's own
 * normalization and ranking; offline it searches this device's synchronised copy
 * with the mirrored rules. The screen always says which of the two answered.
 *
 * With the box empty this is still the plain list, read from the snapshot so it
 * works with no network at all. Nothing here ranks, scores or decides — the
 * order of a search result is the server's, and a weak match is labelled rather
 * than promoted.
 */
export function CustomerListPage() {
  const [term, setTerm] = useState("");
  const navigate = useNavigate();
  const { customers, loading, unavailable } = useLocalData();

  const search = useCustomerSearch(term, {
    customers,
    limit: 50,
    // The list is where somebody who has left the round is looked up, so
    // inactive customers belong in these results. The Daily Register's own
    // search deliberately does the opposite.
    includeInactive: true,
  });
  const searching = term.trim().length > 0;

  const rows = useMemo(
    () =>
      [...customers].sort(
        (a, b) => a.name.localeCompare(b.name) || a.id.localeCompare(b.id),
      ),
    [customers],
  );

  return (
    <div className="stack">
      <header className="day-header">
        <h1 className="day-title">Customers</h1>
        <Link className="btn btn-primary" to="/customers/new">
          Add customer
        </Link>
      </header>

      <CustomerSearchBox value={term} onChange={setTerm} label="Find a customer" />

      {searching ? (
        <>
          <SearchSourceNote
            source={search.source}
            possiblyTruncated={search.possiblyTruncated}
            fellBack={search.fellBack}
          />
          {search.error ? <ErrorNotice message={messageFor(search.error)} /> : null}
          {search.searching && search.results.length === 0 ? (
            <Loading label="Searching…" />
          ) : null}
          <CandidateList
            candidates={search.results}
            onPick={(id) => navigate(`/customers/${id}`)}
            emptyLabel={search.searching ? undefined : "Nobody matches that."}
          />
        </>
      ) : (
        <>
          {loading ? <Loading label="Loading customers…" /> : null}
          {unavailable ? (
            <p className="notice notice-error" role="alert">
              Unavailable offline. This device has not synchronised yet — connect once
              and the customer list will be available without a network.
            </p>
          ) : null}

          {!loading && !unavailable && rows.length === 0 ? (
            <EmptyState>No customers yet. Add the first one.</EmptyState>
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
        </>
      )}
    </div>
  );
}
