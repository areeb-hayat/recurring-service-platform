/**
 * One search hook, two honest sources.
 *
 * Online it asks the server, which searches the whole book with the
 * authoritative rules. Offline it searches this device's synchronised copy with
 * the mirrored ones. Which of the two answered is returned as `source`, and every
 * screen prints it — "everyone on the books" and "the customers this device has"
 * are different claims and the person deserves to know which they are reading.
 *
 * **Typing is not a request.** Keystrokes are debounced and each new query
 * aborts the one in flight, so a name typed at speed is one search, not eight.
 * An aborted request is not an error and never reaches the screen.
 *
 * **A network failure falls back to the device, and says so.** The alternative —
 * an empty result — would read as "this person does not exist", which is a lie
 * that a round would act on.
 */

import { useEffect, useRef, useState } from "react";

import { ApiError } from "@/api/errors";
import { searchCustomers } from "@/api/search";
import type { Customer } from "@/api/types";
import { useSync } from "@/sync/SyncProvider";
import { candidateFromMatch, searchLocalCustomers, type CustomerCandidate } from "./local";

/** Where the answer came from. Displayed, never inferred by a caller. */
export type SearchSource = "server" | "device";

export interface CustomerSearchState {
  results: CustomerCandidate[];
  source: SearchSource;
  searching: boolean;
  /** A full page from the server: there may be more than is shown. */
  possiblyTruncated: boolean;
  /** Set only when the server refused for a reason worth showing. */
  error: ApiError | null;
  /** True when the network was tried and the device answered instead. */
  fellBack: boolean;
}

const IDLE: CustomerSearchState = {
  results: [],
  source: "device",
  searching: false,
  possiblyTruncated: false,
  error: null,
  fellBack: false,
};

/** Long enough that a typed name is one query, short enough to feel immediate. */
export const SEARCH_DEBOUNCE_MS = 200;

export interface CustomerSearchOptions {
  /** The device's own customers, for the offline path. */
  customers: Customer[];
  limit?: number;
  includeInactive?: boolean;
}

export function useCustomerSearch(
  term: string,
  options: CustomerSearchOptions,
): CustomerSearchState {
  const { customers, limit = 20, includeInactive = false } = options;
  const { online } = useSync();
  const [state, setState] = useState<CustomerSearchState>(IDLE);
  // Kept in a ref so a snapshot refresh mid-flight does not restart the search.
  const customersRef = useRef(customers);
  customersRef.current = customers;

  useEffect(() => {
    const query = term.trim();
    if (!query) {
      setState(IDLE);
      return;
    }

    const local = (fellBack: boolean): CustomerSearchState => ({
      results: searchLocalCustomers(customersRef.current, query, {
        limit,
        includeInactive,
      }),
      source: "device",
      searching: false,
      possiblyTruncated: false,
      error: null,
      fellBack,
    });

    if (!online) {
      setState(local(false));
      return;
    }

    const controller = new AbortController();
    let cancelled = false;
    setState((previous) => ({ ...previous, searching: true }));

    const timer = setTimeout(() => {
      void (async () => {
        try {
          const page = await searchCustomers(
            {
              query_text: query,
              limit,
              customer_status: includeInactive ? undefined : "ACTIVE",
            },
            controller.signal,
          );
          if (cancelled) return;
          setState({
            results: page.items.map(candidateFromMatch),
            source: "server",
            searching: false,
            possiblyTruncated: page.possibly_truncated,
            error: null,
            fellBack: false,
          });
        } catch (cause) {
          if (cancelled || controller.signal.aborted) return;
          const failure = cause instanceof ApiError ? cause : null;
          // A dropped connection is not a verdict about who exists: answer from
          // the device and label it, rather than showing an empty list.
          if (failure === null || failure.isRetryable) {
            setState(local(true));
            return;
          }
          setState({ ...IDLE, error: failure });
        }
      })();
    }, SEARCH_DEBOUNCE_MS);

    return () => {
      cancelled = true;
      clearTimeout(timer);
      controller.abort();
    };
  }, [term, online, limit, includeInactive]);

  return state;
}
