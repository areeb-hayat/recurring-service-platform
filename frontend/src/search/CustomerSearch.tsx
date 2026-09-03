import { formatMoney } from "@/lib/money";
import type { CustomerCandidate } from "./local";
import type { SearchSource } from "./useCustomerSearch";

/**
 * The pieces every search surface is built from: a box, a labelled source, and a
 * candidate list with enough on each row to tell two people called Ahmed apart.
 *
 * **Why the rows carry so much.** A list of five identical names is not a
 * choice; it is a coin toss with extra steps. So each row prints the canonical
 * name, the alias that matched when that is *not* the name, the customer code
 * and the area — the four things an owner actually uses to know which Ahmed this
 * is. Phone is shown when it is what matched, because that is then the reason
 * the row is there.
 *
 * **Weak matches are marked, not hidden.** A prefix, a substring, an area or a
 * typo-tolerant match is a suggestion; the badge says so, and nothing in the
 * product acts on one without a person tapping it.
 *
 * **Money only when the server said so.** A candidate found offline carries no
 * balance (`outstanding_minor: null`) and the row prints none — never a zero
 * standing in for a figure this device cannot vouch for (SYN-9).
 */

export function CustomerSearchBox({
  value,
  onChange,
  label,
  placeholder = "Name, nickname, code, phone or area",
  autoFocus = false,
  onSubmit,
}: {
  value: string;
  onChange: (value: string) => void;
  label: string;
  placeholder?: string;
  autoFocus?: boolean;
  /** Enter, when the surface has something to do with a whole query. */
  onSubmit?: () => void;
}) {
  return (
    <label className="field">
      <span>{label}</span>
      <input
        type="search"
        value={value}
        placeholder={placeholder}
        autoFocus={autoFocus}
        autoComplete="off"
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && onSubmit) {
            e.preventDefault();
            onSubmit();
          }
        }}
      />
    </label>
  );
}

/**
 * What was actually searched.
 *
 * Never decoration: "everyone on the books" and "the customers already on this
 * device" are different claims, and a round makes different decisions depending
 * on which one it just read.
 */
export function SearchSourceNote({
  source,
  possiblyTruncated = false,
  fellBack = false,
}: {
  source: SearchSource;
  possiblyTruncated?: boolean;
  fellBack?: boolean;
}) {
  if (source === "device") {
    return (
      <p className="hint" role="status">
        {fellBack
          ? "We could not reach the server, so this searched the customers already on this device."
          : "Offline — searching the customers already on this device."}{" "}
        Anyone added or renamed since this device last synchronised will not appear.
      </p>
    );
  }
  return (
    <p className="hint" role="status">
      Searching everyone on the books.
      {possiblyTruncated ? " Showing the closest matches — narrow the search to see fewer." : ""}
    </p>
  );
}

export function CandidateList({
  candidates,
  onPick,
  emptyLabel,
}: {
  candidates: CustomerCandidate[];
  onPick: (customerId: string) => void;
  emptyLabel?: string;
}) {
  if (candidates.length === 0) {
    return emptyLabel ? <p className="empty">{emptyLabel}</p> : null;
  }
  return (
    <ul className="list">
      {candidates.map((candidate) => (
        <li key={candidate.customer_id}>
          <button
            className="row"
            type="button"
            onClick={() => onPick(candidate.customer_id)}
          >
            <span className="row-main">
              {candidate.name}
              {candidate.status === "INACTIVE" ? <span className="badge">Inactive</span> : null}
              {candidate.match_strength === "WEAK" ? (
                <span className="badge">Possible match</span>
              ) : null}
            </span>
            <span className="row-meta">{describe(candidate)}</span>
          </button>
        </li>
      ))}
    </ul>
  );
}

/**
 * The distinguishing line under a candidate's name.
 *
 * Built from what the server said matched, so the row explains *why* it is being
 * offered — "Ahmed bhai · C-014 · G-10" is a different piece of information from
 * "C-014 · G-10", and the first is what settles which brother this is.
 */
export function describe(candidate: CustomerCandidate): string {
  const parts: string[] = [];

  if (candidate.matched_on === "ALIAS" && candidate.matched_value) {
    parts.push(`“${candidate.matched_value}”`);
  } else if (candidate.matched_on === "PHONE" && candidate.matched_value) {
    parts.push(candidate.matched_value);
  } else if (candidate.aliases.length > 0) {
    parts.push(`“${candidate.aliases[0]}”`);
  }

  parts.push(candidate.code);
  if (candidate.area) parts.push(candidate.area);

  // Server-derived, or absent. Never computed here (FIN-4).
  if (
    candidate.outstanding_minor !== null &&
    candidate.outstanding_minor > 0 &&
    candidate.currency !== null &&
    candidate.currency_exponent !== null
  ) {
    parts.push(
      `${formatMoney(candidate.outstanding_minor, candidate.currency, candidate.currency_exponent)} due`,
    );
  }

  return parts.join(" · ");
}
