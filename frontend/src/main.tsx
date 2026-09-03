import { registerSW } from "virtual:pwa-register";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import { App } from "./App";
import { AppProviders } from "./providers";
import "./styles.css";

/**
 * Install the Service Worker that keeps the app shell openable offline.
 *
 * No prompt and no forced reload: a waiting worker takes over on the next
 * natural load. Interrupting somebody mid-round to apply an update is the one
 * thing an app used at a doorstep must not do — and it is never necessary here,
 * because queued work lives in IndexedDB rather than in this page.
 */
registerSW({ immediate: true });

const root = document.getElementById("root");
if (!root) throw new Error("#root is missing from index.html");

createRoot(root).render(
  <StrictMode>
    <BrowserRouter>
      <AppProviders>
        <App />
      </AppProviders>
    </BrowserRouter>
  </StrictMode>,
);
