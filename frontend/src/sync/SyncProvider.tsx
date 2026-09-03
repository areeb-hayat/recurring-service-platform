/**
 * The engine, wired to the signed-in session and to the browser's own signals.
 *
 * One engine per tenant, started when a session exists and stopped when it ends.
 * Stopping closes the database handle and cancels the retry timer; it deletes
 * nothing, so signing out mid-round and back in resumes with the queue intact.
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

import type { OperationEnvelope } from "@/api/operation";
import type { ServiceIntent } from "@/api/service";
import { useAuth } from "@/auth/AuthContext";
import { engineFor, type SyncEngine, type SyncState } from "./engine";
import type { OperationContext } from "./types";

interface SyncValue extends SyncState {
  engine: SyncEngine | null;
  enqueue: (
    envelope: OperationEnvelope<ServiceIntent>,
    context: OperationContext,
  ) => Promise<void>;
  syncNow: () => Promise<void>;
  resolveIssue: (operationId: string) => Promise<void>;
}

const IDLE: SyncState = {
  online: true,
  syncing: false,
  pending: 0,
  unresolved: 0,
  lastSyncedAt: null,
  businessDate: null,
  hydrated: false,
  revision: 0,
};

const SyncContext = createContext<SyncValue | null>(null);

export function SyncProvider({ children }: { children: ReactNode }) {
  const { session } = useAuth();
  const tenantId = session?.tenant_id ?? null;
  const engine = useMemo(() => (tenantId ? engineFor(tenantId) : null), [tenantId]);
  const [state, setState] = useState<SyncState>(engine?.getState() ?? IDLE);

  useEffect(() => {
    if (!engine) {
      setState(IDLE);
      return;
    }
    const unsubscribe = engine.subscribe(setState);
    void engine.start();
    return () => {
      unsubscribe();
      engine.stop();
    };
  }, [engine]);

  // Reconnecting is the single most valuable moment to sync, and the browser
  // tells us about it. `visibilitychange` covers the phone that was in a pocket
  // while the network came back.
  useEffect(() => {
    if (!engine) return;
    const online = () => engine.setOnline(true);
    const offline = () => engine.setOnline(false);
    const wake = () => {
      if (document.visibilityState === "visible" && navigator.onLine !== false) {
        void engine.syncNow();
      }
    };
    window.addEventListener("online", online);
    window.addEventListener("offline", offline);
    document.addEventListener("visibilitychange", wake);
    return () => {
      window.removeEventListener("online", online);
      window.removeEventListener("offline", offline);
      document.removeEventListener("visibilitychange", wake);
    };
  }, [engine]);

  const enqueue = useCallback(
    async (envelope: OperationEnvelope<ServiceIntent>, context: OperationContext) => {
      if (!engine) throw new Error("no tenant session");
      await engine.enqueue(envelope, context);
    },
    [engine],
  );

  const syncNow = useCallback(async () => {
    if (engine) await engine.syncNow();
  }, [engine]);

  const resolveIssue = useCallback(
    async (operationId: string) => {
      if (engine) await engine.resolveIssue(operationId);
    },
    [engine],
  );

  const value = useMemo<SyncValue>(
    () => ({ ...state, engine, enqueue, syncNow, resolveIssue }),
    [state, engine, enqueue, syncNow, resolveIssue],
  );

  return <SyncContext.Provider value={value}>{children}</SyncContext.Provider>;
}

export function useSync(): SyncValue {
  const value = useContext(SyncContext);
  if (!value) throw new Error("useSync must be used inside SyncProvider");
  return value;
}
