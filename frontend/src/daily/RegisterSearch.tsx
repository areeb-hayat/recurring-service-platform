import { useEffect, useState } from "react";

import { ApiError, messageFor } from "@/api/errors";
import { resolveCustomer } from "@/api/search";
import type { Customer } from "@/api/types";
import {
  CandidateList,
  CustomerSearchBox,
  SearchSourceNote,
} from "@/search/CustomerSearch";
import { candidateFromMatch, resolveLocalCustomer, type CustomerCandidate } from "@/search/local";
import { useCustomerSearch } from "@/search/useCustomerSearch";
import { useSync } from "@/sync/SyncProvider";

/**
 * Find somebody on today's round without scrolling to them.
 *
 * A round of two hundred houses is a long list on a phone held one-handed at a
 * gate. Typing "Ahmed bhai" and landing on his card is the difference between
 * this screen being usable and being scrolled.
 *
 * **It jumps; it never records.** Selecting a customer opens that customer's
 * existing card, and the card is the only thing that writes. There is no second
 * Daily Register state machine here — untouched, Done, Waiting to sync and Needs
 * attention are exactly as they were, and this component neither reads nor
 * changes any of them.
 *
 * **Enter is the interesting case, and it is where P8's contract shows.**
 * Pressing Enter asks the server to *identify* the person rather than to list
 * matches:
 *
 *  - RESOLVED — one authoritative customer; the card opens.
 *  - AMBIGUOUS — two Ahmeds, or two people whose brother is also "Ahmed bhai".
 *    The screen asks which; it never takes the first one because it sorted
 *    first.
 *  - NOT_FOUND — nobody matches, said plainly.
 *
 * That is the same resolver a spoken reference and an inbound message will use
 * in later packages, reached over the same endpoint with the same three answers.
 *
 * **Offline it resolves locally with the same rule**, over the customers this
 * device has synchronised, and says that is what it searched. Offline is not an
 * excuse to guess: two candidates on the device are still a question.
 *
 * **Only people on today's round can be jumped to.** Somebody inactive, or
 * otherwise not on the round, is reported as such rather than silently ignored —
 * "we found her, she is not on today's round" is a different fact from "no such
 * person" and the operator needs to be able to tell them apart.
 */
export function RegisterSearch({
  customers,
  onPick,
  onRound,
}: {
  /** Every customer this device knows — the offline search's population. */
  customers: Customer[];
  onPick: (customerId: string) => void;
  /** True when this customer has a card on today's round. */
  onRound: (customerId: string) => boolean;
}) {
  const [term, setTerm] = useState("");
  const { online } = useSync();
  const [asked, setAsked] = useState<Ask | null>(null);
  const search = useCustomerSearch(term, { customers, limit: 8 });

  // A new query invalidates the previous identification: the answer to "who is
  // Ahmed?" must never be left on screen under the word "Bilal".
  useEffect(() => setAsked(null), [term]);

  function choose(customerId: string, name: string) {
    if (!onRound(customerId)) {
      setAsked({ kind: "OFF_ROUND", name });
      return;
    }
    setAsked(null);
    setTerm("");
    onPick(customerId);
  }

  async function identify() {
    const reference = term.trim();
    if (!reference) return;
    setAsked({ kind: "ASKING" });

    if (!online) {
      settle(resolveLocalCustomer(customers, reference));
      return;
    }
    try {
      const resolution = await resolveCustomer(reference);
      settle({
        status: resolution.status,
        customer: resolution.customer ? candidateFromMatch(resolution.customer) : null,
        candidates: resolution.candidates.map(candidateFromMatch),
      });
    } catch (cause) {
      // A dropped connection is not a verdict about who exists: fall back to the
      // device's own copy, exactly as the live search does, and label it.
      if (cause instanceof ApiError && !cause.isRetryable) {
        setAsked({ kind: "ERROR", message: messageFor(cause) });
        return;
      }
      settle(resolveLocalCustomer(customers, reference), true);
    }
  }

  function settle(
    resolution: {
      status: "RESOLVED" | "AMBIGUOUS" | "NOT_FOUND";
      customer: CustomerCandidate | null;
      candidates: CustomerCandidate[];
    },
    fellBack = false,
  ) {
    if (resolution.status === "RESOLVED" && resolution.customer) {
      choose(resolution.customer.customer_id, resolution.customer.name);
      return;
    }
    if (resolution.status === "NOT_FOUND") {
      setAsked({ kind: "NOT_FOUND", fellBack });
      return;
    }
    setAsked({ kind: "AMBIGUOUS", candidates: resolution.candidates, fellBack });
  }

  const showLive = term.trim().length > 0 && asked === null;

  return (
    <section className="stack register-search">
      <CustomerSearchBox
        value={term}
        onChange={setTerm}
        label="Jump to a customer"
        placeholder="Name, nickname, code or phone"
        onSubmit={() => void identify()}
      />

      {showLive ? (
        <>
          <SearchSourceNote
            source={search.source}
            possiblyTruncated={search.possiblyTruncated}
            fellBack={search.fellBack}
          />
          <CandidateList
            candidates={search.results}
            onPick={(id) => choose(id, nameOf(search.results, id))}
            emptyLabel={search.searching ? undefined : "Nobody on this round matches that."}
          />
        </>
      ) : null}

      {asked?.kind === "ASKING" ? (
        <p className="notice" role="status">
          Looking that up…
        </p>
      ) : null}

      {asked?.kind === "AMBIGUOUS" ? (
        <div className="stack">
          <p className="notice notice-warn" role="alert">
            More than one customer could be “{term.trim()}”. Which one?
            {asked.fellBack
              ? " We could not reach the server, so this is from the customers on this device."
              : ""}
          </p>
          <CandidateList
            candidates={asked.candidates}
            onPick={(id) => choose(id, nameOf(asked.candidates, id))}
          />
        </div>
      ) : null}

      {asked?.kind === "NOT_FOUND" ? (
        <p className="notice notice-warn" role="alert">
          No customer matches “{term.trim()}”.
          {asked.fellBack
            ? " We could not reach the server, so only the customers on this device were searched."
            : ""}
        </p>
      ) : null}

      {asked?.kind === "OFF_ROUND" ? (
        <p className="notice notice-warn" role="alert">
          {asked.name} is not on today’s round.
        </p>
      ) : null}

      {asked?.kind === "ERROR" ? (
        <p className="notice notice-error" role="alert">
          {asked.message}
        </p>
      ) : null}
    </section>
  );
}

type Ask =
  | { kind: "ASKING" }
  | { kind: "AMBIGUOUS"; candidates: CustomerCandidate[]; fellBack: boolean }
  | { kind: "NOT_FOUND"; fellBack: boolean }
  | { kind: "OFF_ROUND"; name: string }
  | { kind: "ERROR"; message: string };

function nameOf(candidates: CustomerCandidate[], customerId: string): string {
  return candidates.find((c) => c.customer_id === customerId)?.name ?? "That customer";
}
