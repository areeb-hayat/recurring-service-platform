import { useState, type FormEvent } from "react";

import { messageFor } from "@/api/errors";
import { useAuth } from "./AuthContext";

/**
 * The only unauthenticated screen. There is no signup and no password reset:
 * P0 §4 makes tenant provisioning a platform action, so a public registration
 * form would be an entry point the backend does not have.
 */
export function LoginPage() {
  const { signIn, expired, dismissExpiry } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    dismissExpiry();
    try {
      await signIn(email.trim(), password);
    } catch (cause) {
      setError(messageFor(cause, "login"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="centered">
      <form className="card stack" onSubmit={onSubmit} noValidate>
        <h1>Sign in</h1>

        {expired && !error ? (
          <p className="notice notice-warn" role="status">
            Your session has ended. Please sign in again.
          </p>
        ) : null}
        {error ? (
          <p className="notice notice-error" role="alert">
            {error}
          </p>
        ) : null}

        <label className="field">
          <span>Email</span>
          <input
            name="email"
            type="email"
            autoComplete="username"
            autoCapitalize="none"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>

        <label className="field">
          <span>Password</span>
          <input
            name="password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>

        <button className="btn btn-primary btn-lg" type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
