import { Navigate, useLocation } from "react-router-dom";
import type { ReactNode } from "react";

import { useAuth } from "@/auth/AuthContext";

/**
 * The routing gate. It only mirrors client-side session state — the backend
 * refuses every request without a valid token regardless, and nothing here
 * relaxes that. Its job is to show the login screen instead of a wall of 401s.
 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { session } = useAuth();
  const location = useLocation();

  if (!session) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  return <>{children}</>;
}
