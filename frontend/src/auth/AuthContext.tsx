/**
 * Who is signed in, and the two ways that ends.
 *
 * A person signs out (revoking the refresh token server-side), or the session
 * expires and the refresh fails — `onSessionEnded` fires from the HTTP client and
 * the shell drops to the login screen with a message. Both paths clear the same
 * storage, so there is no state where the app believes it is authenticated and
 * the API disagrees.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useQueryClient } from "@tanstack/react-query";

import * as authApi from "@/api/auth";
import { onSessionEnded } from "@/api/client";
import {
  clearSession,
  loadSession,
  saveSession,
  toStoredSession,
  type StoredSession,
} from "./session";

interface AuthValue {
  session: StoredSession | null;
  /** Set when the session ended on its own rather than by signing out. */
  expired: boolean;
  signIn: (email: string, password: string) => Promise<void>;
  signOut: () => Promise<void>;
  dismissExpiry: () => void;
}

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<StoredSession | null>(() => loadSession());
  const [expired, setExpired] = useState(false);
  const queryClient = useQueryClient();

  useEffect(
    () =>
      onSessionEnded(() => {
        setSession(null);
        setExpired(true);
        queryClient.clear();
      }),
    [queryClient],
  );

  const signIn = useCallback(async (email: string, password: string) => {
    const tokens = await authApi.login(email, password);
    const stored = toStoredSession(tokens);
    saveSession(stored);
    setSession(stored);
    setExpired(false);
  }, []);

  const signOut = useCallback(async () => {
    const current = loadSession();
    // Clear locally first: a failed logout call must never strand someone in a
    // session they asked to leave. The refresh token stays revocable server-side.
    clearSession();
    setSession(null);
    setExpired(false);
    queryClient.clear();
    if (current) {
      try {
        await authApi.logout(current.refresh_token);
      } catch {
        /* already signed out locally */
      }
    }
  }, [queryClient]);

  const value = useMemo<AuthValue>(
    () => ({ session, expired, signIn, signOut, dismissExpiry: () => setExpired(false) }),
    [session, expired, signIn, signOut],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}
