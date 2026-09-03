/**
 * Customer search and identification — `/api/v1/search/*` (P8).
 *
 * **Two calls, two different questions.**
 *
 *  - {@link searchCustomers} — *"show me who matches this"*. A list, ranked by
 *    the server, for a person to look at.
 *  - {@link resolveCustomer} — *"which customer is this?"*. One of three
 *    answers: RESOLVED with an authoritative id, AMBIGUOUS with candidates to
 *    choose between, or NOT_FOUND.
 *
 * **The client does no matching.** There is no ranking, no threshold and no
 * tie-break on this side of the wire: normalization, tiers and the strong/weak
 * line all live in `app/search/`, so the same words get the same answer whether
 * they were typed here, spoken into a later package, or arrived as a message in
 * the one after that. The only client-side matching that exists anywhere is the
 * offline mirror in `src/search/local.ts`, which is clearly labelled as
 * searching this device's synchronised copy rather than the books.
 *
 * **AMBIGUOUS is not an error and is never resolved locally.** When the server
 * declines to identify somebody, the screen asks; it does not take the first
 * candidate because it happened to sort first. A wrong customer id becomes a
 * wrong delivery, a wrong charge and a wrong balance.
 *
 * **Both are POSTs with a body, deliberately.** The query is a person's name,
 * and names do not belong in a URL, an access log or browser history.
 *
 * **Online only.** Search reads the whole book, which only the server has. The
 * offline path is a different, honestly-labelled thing — see `src/search/`.
 */

import { request } from "./client";
import type { CustomerResolution, CustomerSearchResponse } from "./types";

/** The subset of `CustomerSearchFilter` this client sends. */
export interface CustomerSearchQuery {
  /** The single box: name, alias, code, phone and area. */
  query_text?: string;
  /** Name or alias only. */
  name_contains?: string;
  code?: string;
  phone?: string;
  area?: string;
  customer_status?: "ACTIVE" | "INACTIVE";
  outstanding_min_minor?: number;
  outstanding_max_minor?: number;
  sort?: "RELEVANCE" | "NAME" | "OUTSTANDING";
  limit?: number;
  offset?: number;
  allow_fuzzy?: boolean;
}

/**
 * The filter is validated by the server with `extra="forbid"`, so an unknown
 * field is a 422 rather than a silently ignored one. Undefined keys are stripped
 * here so an optional field never travels as `null` and trips that validator.
 */
export function searchCustomers(
  query: CustomerSearchQuery,
  signal?: AbortSignal,
): Promise<CustomerSearchResponse> {
  const body: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== "") body[key] = value;
  }
  return request<CustomerSearchResponse>("/search/customers", {
    method: "POST",
    body,
    signal,
  });
}

export interface ResolveOptions {
  /** How many candidates an AMBIGUOUS answer may carry. The server caps it. */
  limit?: number;
  include_inactive?: boolean;
  allow_fuzzy?: boolean;
  signal?: AbortSignal;
}

export function resolveCustomer(
  reference: string,
  options: ResolveOptions = {},
): Promise<CustomerResolution> {
  const { signal, ...rest } = options;
  return request<CustomerResolution>("/search/customers/resolve", {
    method: "POST",
    body: { reference, ...rest },
    signal,
  });
}
