/** POST /api/v1/auth/{login,refresh,logout}. */

import { request } from "./client";
import type { TokenResponse } from "./types";

export function login(email: string, password: string): Promise<TokenResponse> {
  return request<TokenResponse>("/auth/login", {
    method: "POST",
    anonymous: true,
    body: { email, password, device_label: deviceLabel() },
  });
}

export function logout(refresh_token: string): Promise<void> {
  return request<void>("/auth/logout", {
    method: "POST",
    anonymous: true,
    body: { refresh_token },
  });
}

/** Names the session in `user_session` so a device can be revoked individually. */
function deviceLabel(): string {
  const agent = typeof navigator === "undefined" ? "" : navigator.userAgent;
  return agent.slice(0, 120) || "web";
}
