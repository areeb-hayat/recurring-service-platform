import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import {
  addAlias,
  deactivateAlias,
  updateAlias,
  listAliases,
  type AliasDraft,
} from "@/api/aliases";
import { messageFor } from "@/api/errors";
import type { CustomerAlias, OperationResult } from "@/api/types";
import { Loading } from "@/components/Feedback";
import { usePendingOperation } from "@/daily/usePendingOperation";
import { useSync } from "@/sync/SyncProvider";

interface RetireIntent {
  alias_id: string;
  reason: string | null;
}

/**
 * The names this customer is actually called.
 *
 * "Muhammad Ahmed Khan" is what is on the books. The round calls him "Ahmed
 * bhai" and the shop next door calls him "Chacha Ahmed" — and until P8 neither
 * of those found him. Recording them here is what lets a search, and later a
 * spoken or messaged reference, land on the right person without anybody
 * guessing.
 *
 * **Nothing here is generated.** Every alias is typed by the owner. No model
 * suggests one, and none is derived from the name: an invented nickname is an
 * invented identity, and it would be indistinguishable from a real one the
 * moment it entered the search index.
 *
 * **Retire, never delete.** An alias that has fallen out of use is marked
 * inactive and stops matching; the row stays, and every add, correction and
 * retirement is audited with the text before and after. How somebody was known
 * last year is what explains an audit row from last year.
 *
 * **Online only**, exactly as editing the customer is. Offline the section says
 * so rather than queueing a write V1's outbox does not carry.
 */
export function CustomerAliases({ customerId }: { customerId: string }) {
  const queryClient = useQueryClient();
  const { online } = useSync();
  const [draft, setDraft] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editText, setEditText] = useState("");

  const query = useQuery({
    queryKey: ["customer", customerId, "aliases"],
    queryFn: () => listAliases(customerId),
  });

  const refresh = () => {
    setDraft("");
    setEditingId(null);
    setEditText("");
    void queryClient.invalidateQueries({ queryKey: ["customer", customerId, "aliases"] });
    // An alias write bumps the customer's row_version server-side, so the
    // customer this screen is showing is stale the moment one lands.
    void queryClient.invalidateQueries({ queryKey: ["customer", customerId] });
  };

  const add = usePendingOperation<AliasDraft, OperationResult<CustomerAlias>>(
    (envelope) => addAlias(customerId, envelope),
    refresh,
  );
  const rename = usePendingOperation<AliasDraft, OperationResult<CustomerAlias>>(
    (envelope) => updateAlias(customerId, editingId ?? "", envelope),
    refresh,
  );
  // Which alias is being retired travels *in the payload*, not in React state.
  // `start` sends immediately, so a `setState` in the same click would not have
  // landed yet and the request would carry the previously selected row.
  const retire = usePendingOperation<RetireIntent, OperationResult<CustomerAlias>>(
    (envelope) => deactivateAlias(customerId, envelope.payload.alias_id, envelope),
    refresh,
  );

  const aliases = query.data?.items ?? [];
  const busy =
    add.phase === "sending" || rename.phase === "sending" || retire.phase === "sending";
  const failure = add.error ?? rename.error ?? retire.error;

  return (
    <section className="stack">
      <h2 className="section-title">Also known as</h2>

      {query.isPending ? <Loading label="Loading names…" /> : null}

      {aliases.length === 0 && !query.isPending ? (
        <p className="hint">
          No other names recorded. Add the name the round actually uses — searching
          for it will then find this customer.
        </p>
      ) : null}

      <ul className="list">
        {aliases.map((alias) => (
          <li key={alias.id}>
            {editingId === alias.id ? (
              <div className="card stack">
                <label className="field">
                  <span>Correct this name</span>
                  <input
                    type="text"
                    value={editText}
                    onChange={(e) => setEditText(e.target.value)}
                    disabled={busy}
                  />
                </label>
                <div className="form-actions">
                  <button
                    className="btn btn-primary"
                    type="button"
                    disabled={busy || !editText.trim()}
                    onClick={() => {
                      void rename.start("customer.alias.update", {
                        alias: editText.trim(),
                      });
                    }}
                  >
                    Save
                  </button>
                  <button
                    className="btn btn-quiet"
                    type="button"
                    onClick={() => {
                      setEditingId(null);
                      rename.discard();
                    }}
                  >
                    Cancel
                  </button>
                </div>
              </div>
            ) : (
              <div className="row">
                <span className="row-main">{alias.alias}</span>
                <span className="row-meta">
                  <button
                    className="btn btn-quiet"
                    type="button"
                    disabled={!online || busy}
                    onClick={() => {
                      setEditingId(alias.id);
                      setEditText(alias.alias);
                    }}
                  >
                    Correct
                  </button>
                  <button
                    className="btn btn-quiet"
                    type="button"
                    disabled={!online || busy}
                    onClick={() => {
                      void retire.start("customer.alias.deactivate", {
                        alias_id: alias.id,
                        reason: null,
                      });
                    }}
                  >
                    Retire
                  </button>
                </span>
              </div>
            )}
          </li>
        ))}
      </ul>

      {online ? (
        <div className="card stack">
          <label className="field">
            <span>Add another name</span>
            <input
              type="text"
              value={draft}
              placeholder="Ahmed bhai"
              disabled={busy}
              onChange={(e) => setDraft(e.target.value)}
            />
          </label>
          <button
            className="btn btn-secondary"
            type="button"
            disabled={busy || !draft.trim()}
            onClick={() => {
              void add.start("customer.alias.add", { alias: draft.trim() });
            }}
          >
            Add name
          </button>
        </div>
      ) : (
        <p className="hint">
          Offline. Names can be searched on this device, but adding or changing one
          needs a connection.
        </p>
      )}

      {failure ? (
        <p className="notice notice-error" role="alert">
          {messageFor(failure)}
        </p>
      ) : null}
    </section>
  );
}
