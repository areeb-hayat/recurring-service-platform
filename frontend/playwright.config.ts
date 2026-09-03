import { defineConfig } from "@playwright/test";

/**
 * P5's acceptance proofs, and nothing more.
 *
 * The suite is deliberately small: four acceptance cases from P0 §7 that are
 * only honestly testable in a browser (durability across a real restart, a real
 * Service Worker, a real offline toggle), plus the PWA shell reopening offline.
 * Everything else is covered where it is cheaper and sharper — Vitest for the
 * client's logic, pytest against PostgreSQL for the server's.
 *
 * Tests run against the **production build**, because that is what has a Service
 * Worker at all.
 *
 * Workers are pinned to one: the tests share a fixture server that holds the
 * business state they assert on, and each resets it.
 */
export default defineConfig({
  testDir: "./e2e",
  testMatch: /.*\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  reporter: process.env.CI ? "list" : [["list"]],
  use: {
    baseURL: "http://localhost:4173",
    trace: "off",
  },
  webServer: {
    command: "node e2e/server.js",
    url: "http://localhost:4173/index.html",
    reuseExistingServer: !process.env.CI,
    stdout: "ignore",
    stderr: "pipe",
    timeout: 60_000,
  },
});
