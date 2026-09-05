/**
 * The E2E fixture server: the built app, plus a stand-in for the API.
 *
 * **Why a stand-in and not the real backend.** The acceptance cases these tests
 * exist for — A-SYN-5, 6, 7 and 12 — are statements about the *client*: that a
 * queued operation survives a browser restart, that a lost response is not a
 * duplicate, that a conflict becomes a durable issue and is never re-sent. The
 * server semantics they lean on (one effect per `operation_id`, DUPLICATE on
 * replay, CONFLICT on a taken customer/date slot) are proven directly against
 * PostgreSQL in `backend/tests/test_sync_operations.py`; what is *not* provable
 * there is what a browser does with them. This server implements exactly those
 * semantics and adds the two faults a real server cannot be asked to produce on
 * demand: a response dropped after the effect committed, and another device
 * having filled the same slot.
 *
 * It is a test fixture. It has no persistence, no authentication worth the name
 * and no place outside `e2e/`.
 */

import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { extname, join, normalize } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = fileURLToPath(new URL("../dist", import.meta.url));
const PORT = Number(process.env.E2E_PORT ?? 4173);

export const BUSINESS_DATE = "2026-09-03";
const CUSTOMER_COUNT = 12;

/**
 * The feed version this fixture serves, and the entities it names. This file's
 * half of the contract with the real client rotted silently once already: P6
 * took the feed to 2 and added `payment`/`statement`, P8 took it to 3, and this
 * fixture — frozen since P5 — kept saying 1 and served neither seed endpoint, so
 * every test died at sign-in as "not synchronised". `first-sync-contract.spec.ts`
 * now holds the *independent* expectation and fails if the values below drift
 * from it — so bump these when the real feed changes, and that spec will confirm
 * (or catch) it.
 */
export const FEED_VERSION = 3;
export const FEED_ENTITIES = [
  "tenant",
  "customer",
  "daily_service_record",
  "payment",
  "statement",
];

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".webmanifest": "application/manifest+json; charset=utf-8",
  ".png": "image/png",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
};

let state;

function reset() {
  let rowVersion = 1000;
  const customers = Array.from({ length: CUSTOMER_COUNT }, (_, i) => ({
    id: `c0000000-0000-7000-8000-${String(i).padStart(12, "0")}`,
    code: `C-${String(i + 1).padStart(3, "0")}`,
    name: `Customer ${String(i + 1).padStart(2, "0")}`,
    phone_e164: null,
    whatsapp_e164: null,
    address: null,
    area: "G-10",
    default_quantity: "2.000",
    unit_price_minor: 25000,
    status: "ACTIVE",
    row_version: (rowVersion += 1),
    unit_label: "bottle",
    currency: "PKR",
    currency_exponent: 2,
  }));

  state = {
    rowVersion,
    customers,
    records: [],
    operations: new Map(),
    /** Drop the response of the next push *after* applying it (A-SYN-6). */
    dropNextPushResponse: false,
    /** Reject any operation for these customers, as a validation failure. */
    rejectCustomers: new Set(),
    pushCount: 0,
  };
}

reset();

const nextVersion = () => (state.rowVersion += 1);

function settings() {
  return {
    name: "Alpha Business",
    currency: "PKR",
    currency_exponent: 2,
    unit_label: "bottle",
    timezone: "Asia/Karachi",
    business_date: BUSINESS_DATE,
    default_quantity: "2.000",
    default_unit_price_minor: 25000,
  };
}

function tokens() {
  return {
    access_token: "e2e-access",
    refresh_token: "e2e-refresh",
    token_type: "bearer",
    expires_in: 3600,
    role: "OWNER_ADMIN",
    scope: "TENANT",
    tenant_id: "11111111-1111-7111-8111-111111111111",
  };
}

function makeRecord({ customerId, serviceDate, quantity, kind, operationId }) {
  const isSkip = kind === "SKIP";
  const qty = isSkip ? "0.000" : String(quantity ?? "0");
  return {
    id: `r0000000-0000-7000-8000-${String(state.records.length).padStart(12, "0")}`,
    customer_id: customerId,
    service_date: serviceDate,
    quantity: qty,
    unit_price_minor: 25000,
    unit_label: "bottle",
    charge_minor: isSkip ? 0 : Math.round(Number(qty) * 25000),
    kind: isSkip ? "SKIP" : "SERVICE",
    status: "ACTIVE",
    corrects_id: null,
    superseded_by_id: null,
    adjustment_minor: 0,
    reason: null,
    source: "SYNC",
    input_method: "BUTTON",
    operation_id: operationId,
    recorded_at: "2026-09-03T05:00:00+00:00",
    row_version: nextVersion(),
    currency: "PKR",
    currency_exponent: 2,
  };
}

function activeRecordFor(customerId, serviceDate) {
  return state.records.find(
    (r) =>
      r.customer_id === customerId &&
      r.service_date === serviceDate &&
      r.status === "ACTIVE",
  );
}

/** The four verdicts, with the same rules the real dispatcher applies. */
function applyOperation(envelope) {
  const { operation_id: id, payload } = envelope;

  const seen = state.operations.get(id);
  if (seen) return { operation_id: id, status: "DUPLICATE", entity: seen };

  if (state.rejectCustomers.has(payload.customer_id)) {
    return {
      operation_id: id,
      status: "REJECTED",
      error: { code: "VALIDATION", detail: "customer is not active" },
    };
  }

  const serviceDate = payload.service_date ?? BUSINESS_DATE;
  const existing = activeRecordFor(payload.customer_id, serviceDate);
  if (existing) {
    return {
      operation_id: id,
      status: "CONFLICT",
      error: {
        code: "SERVICE_ALREADY_RECORDED",
        detail: "an active service record already exists for this customer and date",
      },
      server_state: existing,
    };
  }

  const record = makeRecord({
    customerId: payload.customer_id,
    serviceDate,
    quantity: payload.quantity,
    kind: payload.kind,
    operationId: id,
  });
  state.records.push(record);
  state.operations.set(id, record);
  return { operation_id: id, status: "APPLIED", entity: record };
}

function changesSince(since, limit) {
  const rows = [
    ...state.customers.map((c) => ({
      entity: "customer",
      id: c.id,
      row_version: c.row_version,
      data: c,
    })),
    ...state.records.map((r) => ({
      entity: "daily_service_record",
      id: r.id,
      row_version: r.row_version,
      data: r,
    })),
  ]
    .filter((row) => row.row_version > since)
    .sort((a, b) => a.row_version - b.row_version);

  const page = rows.slice(0, limit);
  return {
    since,
    cursor: page.length ? page[page.length - 1].row_version : since,
    has_more: rows.length > limit,
    head: state.rowVersion,
    feed_version: FEED_VERSION,
    entities: FEED_ENTITIES,
    changes: page,
  };
}

function json(res, status, body) {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
  });
  res.end(payload);
}

async function readBody(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  if (chunks.length === 0) return {};
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    return {};
  }
}

async function serveStatic(url, res) {
  const clean = normalize(decodeURIComponent(url.pathname)).replace(/^(\.\.[/\\])+/, "");
  const candidate = join(ROOT, clean);
  const file = existsSync(candidate) && extname(candidate) ? candidate : join(ROOT, "index.html");
  try {
    const body = await readFile(file);
    res.writeHead(200, {
      "Content-Type": TYPES[extname(file)] ?? "application/octet-stream",
      // The Service Worker is the caching authority here; HTTP caching would only
      // make a test's second load ambiguous.
      "Cache-Control": "no-cache",
    });
    res.end(body);
  } catch {
    res.writeHead(404).end("not found");
  }
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url ?? "/", `http://localhost:${PORT}`);
  const path = url.pathname;

  // --- test controls --------------------------------------------------------
  if (path.startsWith("/__test/")) {
    const body = await readBody(req);
    if (path === "/__test/reset") {
      reset();
      return json(res, 200, { ok: true });
    }
    if (path === "/__test/drop-next-push") {
      state.dropNextPushResponse = true;
      return json(res, 200, { ok: true });
    }
    if (path === "/__test/reject-customer") {
      state.rejectCustomers.add(body.customer_id);
      return json(res, 200, { ok: true });
    }
    if (path === "/__test/record-elsewhere") {
      // Another device got there first.
      const record = makeRecord({
        customerId: body.customer_id,
        serviceDate: body.service_date ?? BUSINESS_DATE,
        quantity: body.quantity ?? "9",
        kind: "SERVICE",
        operationId: "00000000-0000-7000-8000-000000000001",
      });
      state.records.push(record);
      return json(res, 200, record);
    }
    if (path === "/__test/state") {
      return json(res, 200, {
        records: state.records,
        operations: [...state.operations.keys()],
        push_count: state.pushCount,
        customers: state.customers,
      });
    }
    return json(res, 404, { error: { code: "NOT_FOUND", detail: path } });
  }

  // --- api ------------------------------------------------------------------
  if (path.startsWith("/api/v1/")) {
    const body = await readBody(req);

    if (path === "/api/v1/auth/login") return json(res, 200, tokens());
    if (path === "/api/v1/auth/refresh") return json(res, 200, tokens());
    if (path === "/api/v1/auth/logout") return res.writeHead(204).end();

    if (path === "/api/v1/tenant/settings") return json(res, 200, settings());

    if (path === "/api/v1/customers") {
      const limit = Number(url.searchParams.get("limit") ?? 100);
      const offset = Number(url.searchParams.get("offset") ?? 0);
      return json(res, 200, {
        items: state.customers.slice(offset, offset + limit),
      });
    }

    if (path.startsWith("/api/v1/service/day/")) {
      const date = path.split("/").pop();
      return json(res, 200, {
        service_date: date,
        business_date: BUSINESS_DATE,
        items: state.records.filter(
          (r) => r.service_date === date && r.status === "ACTIVE",
        ),
      });
    }

    // P6 added payment and statement to the first-sync seed (`SyncEngine.seed`
    // reads them to the end of their pagination). This fixture has no financial
    // history to serve — the offline acceptance cases are about service records —
    // but the endpoints must exist and return the empty list shape, or the seed's
    // `Promise.all` rejects on a 404 and the app never leaves "not synchronised".
    if (path === "/api/v1/payments") return json(res, 200, { items: [] });
    if (path === "/api/v1/statements") return json(res, 200, { items: [] });

    if (path === "/api/v1/sync/changes") {
      return json(
        res,
        200,
        changesSince(
          Number(url.searchParams.get("since") ?? 0),
          Number(url.searchParams.get("limit") ?? 500),
        ),
      );
    }

    if (path === "/api/v1/sync/operations") {
      state.pushCount += 1;
      const results = (body.operations ?? []).map(applyOperation);
      if (state.dropNextPushResponse) {
        // The effect is committed; the answer never arrives. This is the exact
        // shape of the failure `operation_id` exists to survive (A-SYN-6).
        state.dropNextPushResponse = false;
        req.socket.destroy();
        return;
      }
      return json(res, 200, { results });
    }

    return json(res, 404, { error: { code: "NOT_FOUND", detail: path } });
  }

  await serveStatic(url, res);
});

// Bind the port only when run directly (`node e2e/server.js`, as the Playwright
// webServer does). Importing this module for its constants — the contract test —
// must not try to listen on a port the running fixture already holds.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  server.listen(PORT, () => {
    process.stdout.write(`e2e fixture server on http://localhost:${PORT}\n`);
  });
}
