/**
 * The HTTP boundary, mocked.
 *
 * `fetch` *is* the boundary — the app has no other way out — so stubbing it
 * tests exactly what would leave the browser: the method, the URL, the headers
 * and the body. That matters more than usual here, because the guarantee under
 * test is about what is *in* a request (the same `operation_id` on a retry), not
 * merely about what a screen renders afterwards.
 *
 * Deliberately hand-rolled rather than a service-worker mocking library: this is
 * about forty lines, has no transport of its own to go wrong, and records every
 * call so a test can assert on the second attempt as easily as the first.
 */

export interface RecordedRequest {
  method: string;
  url: string;
  path: string;
  headers: Record<string, string>;
  body: unknown;
}

export interface StubbedResponse {
  status?: number;
  body?: unknown;
  /** Throw instead of answering — a dropped connection, not a verdict. */
  networkError?: boolean;
}

type Handler = (request: RecordedRequest, callIndex: number) => StubbedResponse;

const handlers = new Map<string, Handler>();
export const requests: RecordedRequest[] = [];

function key(method: string, path: string): string {
  return `${method.toUpperCase()} ${path}`;
}

/** Answer `METHOD /path` with a fixed response, or with one chosen per attempt. */
export function stub(
  method: string,
  path: string,
  response: StubbedResponse | Handler,
): void {
  handlers.set(
    key(method, path),
    typeof response === "function" ? response : () => response,
  );
}

/** Every request the app made, oldest first. */
export function requestsTo(method: string, path: string): RecordedRequest[] {
  return requests.filter(
    (r) => r.method === method.toUpperCase() && r.path === path,
  );
}

export function resetHttp(): void {
  handlers.clear();
  requests.length = 0;
  globalThis.fetch = mockFetch as typeof fetch;
}

const mockFetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const url = typeof input === "string" ? input : input.toString();
  const path = url.split("?")[0] ?? url;
  const method = (init?.method ?? "GET").toUpperCase();

  const headers: Record<string, string> = {};
  for (const [name, value] of Object.entries((init?.headers ?? {}) as Record<string, string>)) {
    headers[name.toLowerCase()] = value;
  }

  const request: RecordedRequest = {
    method,
    url,
    path,
    headers,
    body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
  };
  requests.push(request);

  const handler = handlers.get(key(method, path));
  if (!handler) {
    throw new Error(`no stub for ${method} ${path}`);
  }

  const callIndex = requestsTo(method, path).length - 1;
  const stubbed = handler(request, callIndex);
  if (stubbed.networkError) throw new TypeError("Failed to fetch");

  const status = stubbed.status ?? 200;
  return new Response(status === 204 ? null : JSON.stringify(stubbed.body ?? {}), {
    status,
    headers: { "Content-Type": "application/json" },
  });
};
