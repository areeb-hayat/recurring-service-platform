import { useState, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ApiError } from "@/api/errors";
import { AuthProvider } from "@/auth/AuthContext";

/**
 * The query client, and why its retry rule is what it is.
 *
 * A **read** may be retried freely — it changes nothing. A **write** is never
 * retried automatically here: retrying is safe only because the operation keeps
 * its `operation_id`, and that decision belongs to the screen that owns the
 * envelope (`usePendingOperation`), not to a library that would silently resend
 * on its own schedule. So mutations are not routed through TanStack Query at all
 * and reads retry only on transport failures — a 404 or a 422 is an answer, and
 * asking again will not change it.
 */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: (attempt, error) =>
          attempt < 2 && error instanceof ApiError && error.isRetryable,
        refetchOnWindowFocus: false,
        staleTime: 30_000,
      },
    },
  });
}

export function AppProviders({ children }: { children: ReactNode }) {
  const [client] = useState(createQueryClient);
  return (
    <QueryClientProvider client={client}>
      <AuthProvider>{children}</AuthProvider>
    </QueryClientProvider>
  );
}
