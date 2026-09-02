import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach } from "vitest";
import { cleanup } from "@testing-library/react";

import { clearSession } from "@/auth/session";
import { resetHttp } from "./http";

beforeEach(() => {
  // `clearSession` as well as clearing storage: the session module keeps an
  // in-memory fallback for browsers that block site data, and it would otherwise
  // survive into the next test.
  window.localStorage.clear();
  clearSession();
  resetHttp();
});

afterEach(() => {
  cleanup();
});
