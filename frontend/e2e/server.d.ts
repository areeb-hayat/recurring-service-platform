/**
 * Types for the plain-JS fixture server, so `first-sync-contract.spec.ts` can
 * import `BUSINESS_DATE` under the project's strict tsc (`allowJs` is off, and
 * `e2e` is in the typecheck scope). Runtime still uses `server.js`.
 */

export const BUSINESS_DATE: string;
export const FEED_VERSION: number;
export const FEED_ENTITIES: readonly string[];
