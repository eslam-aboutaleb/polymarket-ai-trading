/**
 * Settings panel for the opt-in WebMCP read-only tools: scopes, state,
 * last call, origin, an audit list and a disconnect control.
 *
 * @module integrations/webmcp/WebMcpPermissionsPanel
 */

import { useAuthStore } from "../../store/authStore";
import { isOptedIn, setOptIn, useWebMcpStore } from "./store";
import { WEBMCP_CONTRACT_VERSION, WEBMCP_SCOPES, WEBMCP_TOOLS } from "./tools";

const STATUS_COPY: Record<string, string> = {
  unsupported: "Not available: this browser does not expose WebMCP. The app works normally.",
  signed_out: "Inactive: sign in to manage browser tools.",
  disabled: "Off: no tools are exposed to the browser.",
  active: "On: the read-only tools below are exposed to this browser's agent.",
};

export default function WebMcpPermissionsPanel() {
  const walletAddress = useAuthStore((s) => s.walletAddress);
  const status = useWebMcpStore((s) => s.status);
  const toolNames = useWebMcpStore((s) => s.toolNames);
  const lastCallAt = useWebMcpStore((s) => s.lastCallAt);
  const audit = useWebMcpStore((s) => s.audit);
  const clearAudit = useWebMcpStore((s) => s.clearAudit);
  useWebMcpStore((s) => s.optInVersion);

  const optedIn = isOptedIn(walletAddress);
  const supported = status !== "unsupported";

  return (
    <section className="surface-panel p-4 space-y-3" aria-labelledby="webmcp-heading">
      <h3 id="webmcp-heading" className="text-lg font-semibold">
        Browser agent tools (WebMCP)
      </h3>
      <p className="text-sm text-soft" data-testid="webmcp-status">
        {STATUS_COPY[status] ?? STATUS_COPY.disabled}
      </p>
      <p className="text-xs text-soft">
        Read-only market research tools ({WEBMCP_CONTRACT_VERSION}). They can never place or cancel
        orders, sign, or reveal keys. Off by default, per wallet.
      </p>
      <ul className="text-sm space-y-1" aria-label="Read scopes">
        {WEBMCP_SCOPES.map((s) => (
          <li key={s.scope}>
            <code>{s.scope}</code>: {s.label}
          </li>
        ))}
      </ul>
      <p className="text-xs text-soft">
        Tools: {WEBMCP_TOOLS.map((t) => t.name).join(", ")}
        {status === "active" ? ` (registered: ${toolNames.length})` : ""}
      </p>
      <p className="text-xs text-soft">
        Origin: <code>{typeof window === "undefined" ? "" : window.location.origin}</code>. Last
        call: <span data-testid="webmcp-last-call">{lastCallAt ?? "never"}</span>
      </p>
      <div className="flex gap-2">
        <button
          type="button"
          className="btn-muted"
          disabled={!walletAddress || !supported}
          onClick={() => walletAddress && setOptIn(walletAddress, !optedIn)}
        >
          {optedIn ? "Disconnect tools" : "Enable read-only tools"}
        </button>
        {audit.length > 0 && (
          <button type="button" className="btn-muted" onClick={clearAudit}>
            Clear audit log
          </button>
        )}
      </div>
      {audit.length > 0 && (
        <ul
          className="text-xs space-y-1"
          aria-label="Tool call audit log"
          data-testid="webmcp-audit"
        >
          {audit.slice(0, 10).map((e) => (
            <li key={e.id}>
              {e.at} {e.tool} [{e.inputFields.join(",") || "no input"}] {e.outcome}
              {e.errorCode ? ` (${e.errorCode})` : ""} {e.durationMs}ms
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
