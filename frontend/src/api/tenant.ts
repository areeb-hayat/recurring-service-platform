/** GET /api/v1/tenant/settings. */

import { request } from "./client";
import type { TenantSettings } from "./types";

export function getTenantSettings(): Promise<TenantSettings> {
  return request<TenantSettings>("/tenant/settings");
}
