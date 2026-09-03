import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";
import { fileURLToPath, URL } from "node:url";

/**
 * P5 makes this a PWA — and only that.
 *
 * The Service Worker caches **the application shell and the build's static
 * assets**. It is deliberately not a data layer: there is no `runtimeCaching`
 * entry of any kind, so no API response is ever served from a cache. A cached
 * `GET /customers/{id}` would look exactly like a fresh one while carrying last
 * week's outstanding balance, and a stale balance presented as current is worse
 * than no balance at all (SYN-9). Business data offline comes from the IndexedDB
 * snapshot, which is versioned, explicitly synchronised, and honest about what it
 * does not have ("Unavailable offline").
 *
 * `navigateFallbackDenylist` keeps `/api` out of the navigation fallback, so an
 * API request can never be answered with `index.html`.
 *
 * `registerType: "prompt"` rather than `autoUpdate`: an auto-updating worker
 * reloads the page as soon as a new build lands, which on this product means
 * doing it to somebody standing at a door mid-round. A new version installs in
 * the background and takes over on the next natural load instead. Nothing is at
 * risk either way — the outbox is durable — but an interrupted round is still
 * rude.
 */
export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: "prompt",
      includeAssets: ["apple-touch-icon.png"],
      workbox: {
        globPatterns: ["**/*.{js,css,html,svg,png,ico,webmanifest}"],
        navigateFallback: "index.html",
        navigateFallbackDenylist: [/^\/api\//],
        // No API caching. See the note above.
        runtimeCaching: [],
        cleanupOutdatedCaches: true,
        // The pair that gives "installs quietly, never interrupts": the first
        // worker claims the page that registered it, so one online load is
        // enough to make the app openable offline; a *later* worker does not
        // skip waiting, so an update can never swap the code under somebody
        // mid-round — it takes over once the tabs are gone.
        clientsClaim: true,
        skipWaiting: false,
      },
      manifest: {
        name: "Daily Register",
        short_name: "Register",
        description: "Record the daily round, online or off.",
        start_url: "/",
        scope: "/",
        display: "standalone",
        orientation: "portrait",
        background_color: "#1a2044",
        theme_color: "#1a2044",
        icons: [
          { src: "icon-192.png", sizes: "192x192", type: "image/png" },
          { src: "icon-512.png", sizes: "512x512", type: "image/png" },
          {
            src: "icon-512.png",
            sizes: "512x512",
            type: "image/png",
            purpose: "maskable",
          },
        ],
      },
      devOptions: { enabled: false },
    }),
  ],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.VITE_DEV_API_TARGET ?? "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    restoreMocks: true,
    exclude: ["node_modules/**", "e2e/**", "dist/**"],
  },
});
