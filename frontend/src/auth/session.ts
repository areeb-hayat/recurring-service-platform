/**
 * Where the tokens live, and the one place that knows it.
 *
 * `localStorage` rather than memory, because closing the tab must not sign the
 * owner out mid-round — and rather than a cookie, because P0 §3.3 froze a bearer
 * access token plus an opaque refresh token, not a cookie session. The refresh
 * token is revocable server-side (`user_session`), which is what makes losing a
 * device recoverable.
 *
 * Reads and writes are wrapped: a browser with site data blocked throws on the
 * accessor itself, and that must degrade to a session that lasts as long as the
 * tab rather than a blank screen.
 */

import type { TokenResponse } from "@/api/types";

const KEY = "rsp.session.v1";

export interface StoredSession {
  access_token: string;
  refresh_token: string;
  role: string;
  scope: string;
  tenant_id: string | null;
}

let memoryFallback: StoredSession | null = null;

export function toStoredSession(tokens: TokenResponse): StoredSession {
  return {
    access_token: tokens.access_token,
    refresh_token: tokens.refresh_token,
    role: tokens.role,
    scope: tokens.scope,
    tenant_id: tokens.tenant_id,
  };
}

export function loadSession(): StoredSession | null {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return memoryFallback;
    return JSON.parse(raw) as StoredSession;
  } catch {
    return memoryFallback;
  }
}

export function saveSession(session: StoredSession): void {
  memoryFallback = session;
  try {
    window.localStorage.setItem(KEY, JSON.stringify(session));
  } catch {
    /* storage unavailable; the in-memory copy carries this tab */
  }
}

export function clearSession(): void {
  memoryFallback = null;
  try {
    window.localStorage.removeItem(KEY);
  } catch {
    /* nothing to do */
  }
}
