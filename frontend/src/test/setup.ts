import "@testing-library/jest-dom/vitest";
import "fake-indexeddb/auto";
import { IDBFactory } from "fake-indexeddb";
import { afterEach, beforeEach } from "vitest";
import { cleanup } from "@testing-library/react";

import { clearSession } from "@/auth/session";
import { resetSyncDbCache } from "@/sync/db";
import { resetEngines } from "@/sync/engine";
import { resetHttp } from "./http";

beforeEach(() => {
  // `clearSession` as well as clearing storage: the session module keeps an
  // in-memory fallback for browsers that block site data, and it would otherwise
  // survive into the next test.
  window.localStorage.clear();
  clearSession();
  resetHttp();

  // A brand-new IndexedDB per test. `fake-indexeddb` has no "clear everything"
  // call, so the factory itself is replaced — and the engine and handle caches
  // are dropped with it, or they would go on holding databases that no longer
  // exist.
  resetEngines();
  resetSyncDbCache();
  globalThis.indexedDB = new IDBFactory();
});

afterEach(() => {
  cleanup();
  resetEngines();
});
