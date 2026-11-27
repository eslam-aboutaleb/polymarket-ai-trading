/**
 * WebMCP opt-in policy and audit log.
 *
 * Opt-in is stored per wallet address in `localStorage` and defaults to OFF.
 * The audit log is in-memory and holds only tool name, input field NAMES,
 * outcome, duration and time: never input values, results or credentials.
 *
 * @module integrations/webmcp/store
 */

import { create } from "zustand";

export const WEBMCP_OPT_IN_PREFIX = "polymarket_webmcp_optin:";
export const MAX_AUDIT_ENTRIES = 50;

export type WebMcpStatus =
  | "unsupported" // browser has no native WebMCP
  | "signed_out"
  | "disabled" // supported, signed in, but the account has not opted in
  | "active";

export interface WebMcpAuditEntry {
  id: number;
  at: string;
  tool: string;
  origin: string;
  inputFields: string[];
  outcome: "ok" | "error";
  errorCode?: string;
  durationMs: number;
}

interface WebMcpState {
  status: WebMcpStatus;
  toolNames: string[];
  lastCallAt: string | null;
  audit: WebMcpAuditEntry[];
  /** Bumped when opt-in changes so subscribers re-sync. */
  optInVersion: number;
}

interface WebMcpActions {
  setStatus: (status: WebMcpStatus, toolNames?: string[]) => void;
  recordCall: (entry: Omit<WebMcpAuditEntry, "id">) => void;
  clearAudit: () => void;
  bumpOptIn: () => void;
}

let auditSeq = 0;

export const useWebMcpStore = create<WebMcpState & WebMcpActions>()((set) => ({
  status: "signed_out",
  toolNames: [],
  lastCallAt: null,
  audit: [],
  optInVersion: 0,
  setStatus: (status, toolNames = []) => set({ status, toolNames }),
  recordCall: (entry) =>
    set((state) => ({
      lastCallAt: entry.at,
      audit: [{ ...entry, id: ++auditSeq }, ...state.audit].slice(0, MAX_AUDIT_ENTRIES),
    })),
  clearAudit: () => set({ audit: [], lastCallAt: null }),
  bumpOptIn: () => set((state) => ({ optInVersion: state.optInVersion + 1 })),
}));

function optInKey(walletAddress: string): string {
  return `${WEBMCP_OPT_IN_PREFIX}${walletAddress.toLowerCase()}`;
}

export function isOptedIn(walletAddress: string | null): boolean {
  if (!walletAddress) return false;
  try {
    return window.localStorage.getItem(optInKey(walletAddress)) === "1";
  } catch {
    return false;
  }
}

export function setOptIn(walletAddress: string, enabled: boolean): void {
  try {
    if (enabled) window.localStorage.setItem(optInKey(walletAddress), "1");
    else window.localStorage.removeItem(optInKey(walletAddress));
  } catch {
    // Storage failure leaves the default (disabled) in force.
  }
  useWebMcpStore.getState().bumpOptIn();
}
