/**
 * @module ApiKeysSection
 * Management surface for external-API credentials (plan 06 Phase B).
 *
 * Keys authenticate `/api/v1/external/*` via an `X-API-Key` header —
 * a namespace that accepts no cookie and no bearer token, so these are
 * how a script or a third-party client acts on the owner's behalf.
 * Scopes are `read` and `trade`; there is deliberately no `withdraw`
 * scope, so a compromised key cannot move funds the UI could not.
 *
 * The secret is shown exactly once (only its HMAC digest is stored
 * server-side), which is why creation renders a one-time panel with a
 * copy button instead of re-fetching it.
 */

import { useCallback, useEffect, useState } from "react";
import { getApiErrorMessage } from "../utils/apiError";
import { apiKeyService, ApiKey, ApiKeyScope } from "../services/apiKeyService";
import { accountService } from "../services/accountService";
import { ConfirmModal } from "./ui";

const ALL_SCOPES: ApiKeyScope[] = ["read", "trade"];

function isExpiring(key: ApiKey): boolean {
  if (!key.expires_at) return false;
  const ms = new Date(key.expires_at).getTime() - Date.now();
  return ms > 0 && ms < 14 * 24 * 60 * 60 * 1000;
}

function formatDate(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleDateString();
}

export default function ApiKeysSection() {
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [accounts, setAccounts] = useState<{ id: number; name: string }[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<ApiKeyScope[]>(["read"]);
  const [accountId, setAccountId] = useState<string>("");
  const [expiresInDays, setExpiresInDays] = useState("90");
  /** The one-time secret. Cleared as soon as the panel is dismissed. */
  const [freshSecret, setFreshSecret] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [pendingRevoke, setPendingRevoke] = useState<ApiKey | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [keyRows, accountRows] = await Promise.all([
        apiKeyService.list(),
        // Accounts are advisory: a key may be scoped to one, and an
        // empty list just means the account feature stays unused.
        accountService.listAccounts().catch(() => []),
      ]);
      setKeys(keyRows);
      setAccounts(accountRows.map((a) => ({ id: a.id, name: a.name })));
    } catch (err) {
      setError(getApiErrorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const toggleScope = (scope: ApiKeyScope) => {
    setScopes((current) =>
      current.includes(scope) ? current.filter((s) => s !== scope) : [...current, scope].sort(),
    );
  };

  const createKey = async () => {
    const trimmed = name.trim();
    if (!trimmed || scopes.length === 0 || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const created = await apiKeyService.create({
        name: trimmed,
        scopes,
        expires_in_days: Number(expiresInDays) || null,
        account_id: accountId ? Number(accountId) : null,
      });
      setFreshSecret(created.secret);
      setCopied(false);
      setName("");
      setScopes(["read"]);
      setAccountId("");
      await load();
    } catch (err) {
      setError(getApiErrorMessage(err));
    } finally {
      setSubmitting(false);
    }
  };

  const revokeKey = async (key: ApiKey) => {
    setPendingRevoke(null);
    setError(null);
    try {
      await apiKeyService.revoke(key.id);
      await load();
    } catch (err) {
      setError(getApiErrorMessage(err));
    }
  };

  const copySecret = async () => {
    if (!freshSecret) return;
    try {
      await navigator.clipboard.writeText(freshSecret);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-base font-semibold text-white">External API keys</h3>
        <p className="text-soft text-xs mt-1">
          Keys authenticate <code className="text-primary">/api/v1/external/*</code> with an
          <code className="text-primary"> X-API-Key</code> header. That namespace accepts no cookie
          and no bearer token. Scopes are <code className="text-primary">read</code> and
          <code className="text-primary"> trade</code> — there is no <em>withdraw</em> scope, so a
          key can never move funds the UI could not.
        </p>
      </div>

      {freshSecret && (
        <div className="alert-success p-3 space-y-2">
          <p className="text-sm font-semibold">Copy this key now — it will never be shown again.</p>
          <p className="text-xs">
            Only an HMAC digest is stored server-side. Closing or reloading this page discards the
            secret permanently; if it is lost, revoke the key and mint a new one.
          </p>
          <div className="flex items-center gap-2">
            <code className="flex-1 text-xs bg-black/40 rounded px-2 py-1 break-all select-all">
              {freshSecret}
            </code>
            <button type="button" className="btn-secondary text-xs" onClick={copySecret}>
              {copied ? "Copied" : "Copy"}
            </button>
          </div>
          <button
            type="button"
            className="btn-secondary text-xs"
            onClick={() => setFreshSecret(null)}
          >
            I have stored it
          </button>
        </div>
      )}

      {error && <div className="alert-error p-3 text-sm">{error}</div>}

      {loading ? (
        <p className="text-soft text-sm">Loading keys…</p>
      ) : keys.length === 0 ? (
        <p className="text-soft text-sm">No keys yet. Mint one below to use the external API.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="table-theme w-full">
            <thead>
              <tr>
                <th>Name</th>
                <th>Prefix</th>
                <th>Scopes</th>
                <th>Account</th>
                <th>Expires</th>
                <th>Last used</th>
                <th className="text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {keys.map((key) => (
                <tr key={key.id}>
                  <td>
                    {key.name}
                    {key.revoked && <span className="alert-error ml-2 text-xs">revoked</span>}
                  </td>
                  <td>
                    <code className="text-xs">{key.key_prefix}…</code>
                  </td>
                  <td className="space-x-1">
                    {key.scopes.map((scope) => (
                      <span key={scope} className="text-xs text-primary">
                        {scope}
                      </span>
                    ))}
                  </td>
                  <td className="text-xs text-soft">
                    {accounts.find((a) => a.id === key.account_id)?.name ?? "all accounts"}
                  </td>
                  <td className="text-xs">
                    {formatDate(key.expires_at)}
                    {isExpiring(key) && <span className="text-yellow-400 ml-1">expiring soon</span>}
                  </td>
                  <td className="text-xs text-soft">{formatDate(key.last_used_at)}</td>
                  <td className="text-right">
                    <button
                      type="button"
                      className="btn-secondary text-xs"
                      onClick={() => setPendingRevoke(key)}
                      disabled={key.revoked}
                      aria-label={`Revoke ${key.name}`}
                    >
                      Revoke
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="surface-panel p-4 space-y-3">
        <h4 className="text-sm font-semibold text-white">Mint a key</h4>
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-xs text-soft">
            Name
            <input
              className="input-field mt-1"
              value={name}
              maxLength={100}
              placeholder="trading-bot-prod"
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label className="text-xs text-soft">
            Scopes
            <div className="flex gap-3 mt-2">
              {ALL_SCOPES.map((scope) => (
                <label key={scope} className="flex items-center gap-1 text-white text-xs">
                  <input
                    type="checkbox"
                    checked={scopes.includes(scope)}
                    onChange={() => toggleScope(scope)}
                  />
                  {scope}
                </label>
              ))}
            </div>
          </label>
          <label className="text-xs text-soft">
            Account
            <select
              className="input-field mt-1"
              value={accountId}
              onChange={(e) => setAccountId(e.target.value)}
            >
              <option value="">All accounts</option>
              {accounts.map((account) => (
                <option key={account.id} value={account.id}>
                  {account.name}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-soft">
            Expires in (days)
            <input
              className="input-field mt-1 w-24"
              type="number"
              min={1}
              max={365}
              value={expiresInDays}
              onChange={(e) => setExpiresInDays(e.target.value)}
            />
          </label>
          <button
            type="button"
            className="btn-primary"
            onClick={createKey}
            disabled={submitting || !name.trim() || scopes.length === 0}
          >
            {submitting ? "Minting…" : "Mint key"}
          </button>
        </div>
        {accounts.length === 0 && (
          <p className="text-soft text-xs">
            No accounts available yet — the key will act across everything the owner can reach.
          </p>
        )}
      </div>

      <ConfirmModal
        open={pendingRevoke !== null}
        title="Revoke API key?"
        body={
          pendingRevoke
            ? `Revoking "${pendingRevoke.name}" permanently disables it. Any integration still using this secret will stop working immediately — this cannot be undone.`
            : ""
        }
        confirmLabel="Revoke"
        destructive
        onConfirm={() => {
          if (pendingRevoke) void revokeKey(pendingRevoke);
        }}
        onCancel={() => setPendingRevoke(null)}
      />
    </div>
  );
}
