/**
 * Offline search — over this device's synchronised copy, and nothing more.
 *
 * When there is no network the server cannot be asked, and a round still has to
 * find "Ahmed bhai". So this module applies the *same rules* the server applies,
 * to the customers already in the P5 snapshot. Two things make that safe to do:
 *
 *  1. **The snapshot is complete for customers.** The sync engine seeds it by
 *     walking `GET /customers` to the end and keeps it current from the change
 *     feed, so this searches everyone this device has heard about — not a page.
 *     It is still not "everyone", and the UI says which it is.
 *  2. **Aliases travel inside the customer payload** (P8), so a nickname the
 *     owner recorded on another device is searchable here as soon as the
 *     customer row has arrived. Nothing new had to join the feed for that.
 *
 * **What offline deliberately cannot do.**
 *
 *  - *No typo tolerance.* Trigram similarity is PostgreSQL's; there is no
 *    equivalent here and inventing a different one would mean two definitions of
 *    "close enough". Offline matches exactly, by whole word, by prefix or by
 *    substring — and says so.
 *  - *No balances.* Outstanding is server-derived (FIN-4). A candidate found
 *    offline carries `outstanding_minor: null`, and the UI prints nothing rather
 *    than a figure it cannot vouch for (SYN-9).
 *  - *No claim of completeness.* `useCustomerSearch` labels a result from here
 *    as the device's own, and every screen that shows one says what was
 *    searched.
 *
 * **The identification rule is the server's, restated, not relaxed.** A
 * reference resolves only when the strongest match is *strong* — a code, a
 * phone, an exact name, an exact alias, or every word appearing as a whole word
 * — and exactly one customer holds that strength. Anything else is AMBIGUOUS
 * and a person chooses. Offline is not an excuse to guess.
 */

import type { Customer, CustomerMatch, CustomerStatus, MatchKind } from "@/api/types";
import {
  looksLikePhone,
  normalizePhone,
  normalizeText,
  normalizeTokens,
  phoneSuffix,
} from "./normalize";

/**
 * The tiers, mirroring `app.search.query.MatchTier` value for value.
 *
 * Everything at or above {@link STRONG_TIER_MIN} is an identification; below it
 * is a suggestion, worth showing and never worth acting on unasked. FUZZY has no
 * offline equivalent and is absent rather than approximated.
 */
export const MatchTier = {
  CODE_EXACT: 100,
  PHONE_EXACT: 95,
  PHONE_SUFFIX: 90,
  NAME_EXACT: 85,
  ALIAS_EXACT: 80,
  NAME_TOKENS: 75,
  ALIAS_TOKENS: 70,
  // --- below this line: candidates only, never an identification --------
  NAME_PREFIX: 55,
  ALIAS_PREFIX: 50,
  NAME_CONTAINS: 45,
  ALIAS_CONTAINS: 40,
  AREA: 30,
  NONE: 0,
} as const;

export const STRONG_TIER_MIN = MatchTier.ALIAS_TOKENS;

/**
 * One candidate, whichever side of the wire found it.
 *
 * The online path fills this from the server's `CustomerMatch`; the offline path
 * fills it from the snapshot. The money fields are `null` offline on purpose —
 * see the note above — so the type makes "we do not know the balance here"
 * representable rather than letting a zero stand in for it.
 */
export interface CustomerCandidate {
  customer_id: string;
  code: string;
  name: string;
  area: string | null;
  phone_e164: string | null;
  status: CustomerStatus;
  aliases: string[];
  matched_on: MatchKind;
  matched_value: string | null;
  match_strength: "STRONG" | "WEAK";
  outstanding_minor: number | null;
  currency: string | null;
  currency_exponent: number | null;
}

/** The server's answer, in the shape the screens render. Nothing is recomputed. */
export function candidateFromMatch(match: CustomerMatch): CustomerCandidate {
  return {
    customer_id: match.customer_id,
    code: match.code,
    name: match.name,
    area: match.area,
    phone_e164: match.phone_e164,
    status: match.status,
    aliases: match.aliases ?? [],
    matched_on: match.matched_on,
    matched_value: match.matched_value,
    match_strength: match.match_strength,
    outstanding_minor: match.outstanding_minor,
    currency: match.currency,
    currency_exponent: match.currency_exponent,
  };
}

interface Scored {
  customer: Customer;
  tier: number;
  matchedOn: MatchKind;
  matchedValue: string | null;
}

function wholeWordsPresent(normalized: string, tokens: string[]): boolean {
  if (tokens.length === 0) return false;
  const padded = ` ${normalized} `;
  return tokens.every((token) => padded.includes(` ${token} `));
}

/** The strongest way this one customer matches `raw`, or null. */
function score(customer: Customer, raw: string): Scored | null {
  const stripped = raw.trim();
  const normalized = normalizeText(raw);
  const tokens = normalizeTokens(raw);
  let best: Scored | null = null;

  const consider = (
    tier: number,
    matchedOn: MatchKind,
    matchedValue: string | null,
  ) => {
    if (best === null || tier > best.tier) best = { customer, tier, matchedOn, matchedValue };
  };

  if (stripped && customer.code.trim().toLowerCase() === stripped.toLowerCase()) {
    consider(MatchTier.CODE_EXACT, "CODE", customer.code);
  }

  if (looksLikePhone(raw)) {
    const digits = normalizePhone(raw);
    const suffix = phoneSuffix(raw);
    for (const stored of [customer.phone_e164, customer.whatsapp_e164]) {
      if (!stored) continue;
      const storedDigits = normalizePhone(stored);
      if (storedDigits && storedDigits === digits) {
        consider(MatchTier.PHONE_EXACT, "PHONE", stored);
      } else if (suffix && storedDigits.endsWith(suffix)) {
        consider(MatchTier.PHONE_SUFFIX, "PHONE", stored);
      }
    }
  }

  if (normalized) {
    const name = normalizeText(customer.name);
    if (name === normalized) consider(MatchTier.NAME_EXACT, "NAME", customer.name);
    else if (wholeWordsPresent(name, tokens))
      consider(MatchTier.NAME_TOKENS, "NAME", customer.name);
    else if (name.startsWith(normalized))
      consider(MatchTier.NAME_PREFIX, "NAME", customer.name);
    else if (name.includes(normalized))
      consider(MatchTier.NAME_CONTAINS, "NAME", customer.name);

    // A snapshot row written before the P8 feed bump has no `aliases` key. That
    // is "none known on this device", not "none exist" — the feed version bump
    // re-seeds it, and until then the name still matches.
    for (const alias of customer.aliases ?? []) {
      const key = normalizeText(alias);
      if (!key) continue;
      if (key === normalized) consider(MatchTier.ALIAS_EXACT, "ALIAS", alias);
      else if (wholeWordsPresent(key, tokens))
        consider(MatchTier.ALIAS_TOKENS, "ALIAS", alias);
      else if (key.startsWith(normalized))
        consider(MatchTier.ALIAS_PREFIX, "ALIAS", alias);
      else if (key.includes(normalized))
        consider(MatchTier.ALIAS_CONTAINS, "ALIAS", alias);
    }

    const area = normalizeText(customer.area);
    if (area && area.startsWith(normalized)) {
      consider(MatchTier.AREA, "AREA", customer.area);
    }
  }

  return best;
}

export interface LocalSearchOptions {
  limit?: number;
  includeInactive?: boolean;
}

export const DEFAULT_LOCAL_LIMIT = 20;

/** Everything that matches, strongest first, with the tier kept for the resolver. */
function rank(
  customers: Customer[],
  raw: string,
  includeInactive: boolean,
): Scored[] {
  const scored: Scored[] = [];
  for (const customer of customers) {
    if (!includeInactive && customer.status !== "ACTIVE") continue;
    const hit = score(customer, raw);
    if (hit) scored.push(hit);
  }
  // A total order — tier, then name, then id — so the same words give the same
  // list every time, exactly as the server's ORDER BY does.
  scored.sort(
    (a, b) =>
      b.tier - a.tier ||
      a.customer.name.localeCompare(b.customer.name) ||
      a.customer.id.localeCompare(b.customer.id),
  );
  return scored;
}

function toCandidate(hit: Scored): CustomerCandidate {
  return {
    customer_id: hit.customer.id,
    code: hit.customer.code,
    name: hit.customer.name,
    area: hit.customer.area,
    phone_e164: hit.customer.phone_e164,
    status: hit.customer.status,
    aliases: hit.customer.aliases ?? [],
    matched_on: hit.matchedOn,
    matched_value: hit.matchedValue,
    match_strength: hit.tier >= STRONG_TIER_MIN ? "STRONG" : "WEAK",
    // Not known offline, and never guessed at (FIN-4 / SYN-9).
    outstanding_minor: null,
    currency: null,
    currency_exponent: null,
  };
}

/** Rank the device's customers against `raw`, best first. */
export function searchLocalCustomers(
  customers: Customer[],
  raw: string,
  options: LocalSearchOptions = {},
): CustomerCandidate[] {
  if (!raw.trim()) return [];
  return rank(customers, raw, options.includeInactive ?? false)
    .slice(0, options.limit ?? DEFAULT_LOCAL_LIMIT)
    .map(toCandidate);
}

export interface LocalResolution {
  status: "RESOLVED" | "AMBIGUOUS" | "NOT_FOUND";
  query: string;
  customer: CustomerCandidate | null;
  candidates: CustomerCandidate[];
}

/**
 * Identify one customer from the device's own copy — or refuse to.
 *
 * The rule is `app.search.resolver.resolve_customer`'s, restated rather than
 * relaxed:
 *
 *  1. the strongest match is *strong*; and
 *  2. exactly one customer holds that strength.
 *
 * Strict dominance, not "best score wins": an exact name beats a partial one, so
 * typing "Ahmed" when a customer *is* Ahmed identifies him even though "Ahmed
 * Khan" contains the word too — while two people whose names both merely contain
 * it are a question. Offline is not an excuse to guess.
 */
export function resolveLocalCustomer(
  customers: Customer[],
  reference: string,
  options: LocalSearchOptions = {},
): LocalResolution {
  const query = (reference ?? "").trim();
  if (!query) return { status: "NOT_FOUND", query, customer: null, candidates: [] };

  const limit = options.limit ?? 5;
  const ranked = rank(customers, query, options.includeInactive ?? false);
  if (ranked.length === 0) {
    return { status: "NOT_FOUND", query, customer: null, candidates: [] };
  }

  const bestTier = ranked[0]!.tier;
  const atBest = ranked.filter((hit) => hit.tier === bestTier);

  if (bestTier >= STRONG_TIER_MIN && atBest.length === 1) {
    const winner = toCandidate(atBest[0]!);
    return { status: "RESOLVED", query, customer: winner, candidates: [winner] };
  }

  return {
    status: "AMBIGUOUS",
    query,
    customer: null,
    candidates: ranked.slice(0, limit).map(toCandidate),
  };
}
