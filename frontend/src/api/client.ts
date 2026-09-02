/**
 * The single HTTP boundary.
 *
 * Everything the app sends goes through `request()`. That is what makes three
 * separate guarantees hold in one place rather than in every screen:
 *
 *  1. **The tenant is never named by the client.** No request carries a
 *     `tenant_id`; the bearer token decides the scope server-side (SEC-3). There
 *     is no parameter here that could pass one.
 *  2. **A 401 is refreshed once, then the same request is replayed** — the same
 *     method, URL and body, so a mutation keeps its `operation_id` across the
 *     refresh and cannot double-apply. If the refresh itself fails the session is
 *     cleared and the app falls back to the login screen.
 *  3. **A transport failure is not a verdict.** A dropped connection, a timeout
 *     or a 5xx becomes a retryable `ApiError`, never a "rejected" one; only the
 *     server's own error envelope produces a verdict.
 */

import { ApiError } from "./errors";
import type { ApiErrorBody, TokenResponse } from "./types";
import {
  clearSession,
  loadSession,
  saveSession,
  toStoredSession,
} from "@/auth/session";

const BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? "";

/** Notified when the session ends for good, so the shell can show the login screen. */
type SessionEndedListener = () => void;
const sessionEndedListeners = new Set<SessionEndedListener>();

export function onSessionEnded(listener: SessionEndedListener): () => void {
  sessionEndedListeners.add(listener);
  return () => sessionEndedListeners.delete(listener);
}

function endSession(): void {
  clearSession();
  for (const listener of sessionEndedListeners) listener();
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PATCH";
  body?: unknown;
  /** Auth calls carry their own credentials and must not be retried on 401. */
  anonymous?: boolean;
  signal?: AbortSignal;
}

function url(path: string): string {
  return `${BASE_URL}/api/v1${path}`;
}

async function readError(response: Response): Promise<ApiError> {
  let body: ApiErrorBody | undefined;
  try {
    body = (await response.json()) as ApiErrorBody;
  } catch {
    body = undefined;
  }

  // 5xx is not an answer about the operation, whatever the body says.
  if (response.status >= 500) {
    return new ApiError({
      kind: "TRANSPORT",
      code: "SERVER",
      status: response.status,
      detail: "server error",
    });
  }

  const error = body?.error;
  if (!error) {
    return new ApiError({
      kind: "VERDICT",
      code: "VALIDATION",
      status: response.status,
      detail: "unexpected response",
    });
  }

  const { code, detail, field_errors, ...extra } = error;
  return new ApiError({
    kind: response.status === 401 ? "AUTH" : "VERDICT",
    code,
    status: response.status,
    detail,
    fieldErrors: field_errors ?? {},
    extra,
  });
}

async function send(path: string, options: RequestOptions): Promise<Response> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (!options.anonymous) {
    const session = loadSession();
    if (session) headers["Authorization"] = `Bearer ${session.access_token}`;
  }

  try {
    return await fetch(url(path), {
      method: options.method ?? "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
    });
  } catch (cause) {
    // Offline, DNS failure, aborted connection: the request may or may not have
    // reached the server, so this is explicitly retryable and never a verdict.
    throw new ApiError({
      kind: "TRANSPORT",
      code: "NETWORK",
      status: 0,
      detail: cause instanceof Error ? cause.message : "network error",
    });
  }
}

/** One refresh at a time; concurrent 401s share it instead of racing. */
let refreshInFlight: Promise<boolean> | null = null;

async function refreshAccessToken(): Promise<boolean> {
  const session = loadSession();
  if (!session) return false;

  refreshInFlight ??= (async () => {
    try {
      const response = await send("/auth/refresh", {
        method: "POST",
        anonymous: true,
        body: { refresh_token: session.refresh_token },
      });
      if (!response.ok) return false;
      const tokens = (await response.json()) as TokenResponse;
      saveSession(toStoredSession(tokens));
      return true;
    } catch {
      return false;
    } finally {
      refreshInFlight = null;
    }
  })();

  return refreshInFlight;
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  let response = await send(path, options);

  if (response.status === 401 && !options.anonymous) {
    const refreshed = await refreshAccessToken();
    if (!refreshed) {
      endSession();
      throw await readError(response);
    }
    // Same operation, same body, same operation_id — a refresh must not change
    // what is being asked for.
    response = await send(path, options);
    if (response.status === 401) {
      endSession();
      throw await readError(response);
    }
  }

  if (!response.ok) throw await readError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}
