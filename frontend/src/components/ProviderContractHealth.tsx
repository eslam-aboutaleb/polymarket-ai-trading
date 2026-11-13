import { FormEvent, useState } from "react";
import { apiClient } from "../services/apiClient";
import { getApiErrorMessage } from "../utils/apiError";

interface Contract {
  provider_id: string;
  normalization_version: string;
  data_granularity: string;
  permitted_use: string;
}

interface Probe {
  provider_id: string;
  source_symbol: string;
  health_state: string;
  canary_status: string;
  observed_coverage: number;
  observed_bars: number;
  expected_bars: number;
  missing_bars: number;
  stale_delay_seconds: number | null;
  raw_checksum_sha256: string | null;
  capture_id: string | null;
  failure_reason: string | null;
}

export default function ProviderContractHealth() {
  const [token, setToken] = useState("");
  const [contract, setContract] = useState<Contract | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [probe, setProbe] = useState<Probe | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function inspect() {
    setBusy(true);
    setError("");
    setContract(null);
    setProbe(null);
    try {
      const result = await apiClient.get<{ contract: Contract; canary_enabled: boolean }>(
        "/api/market-data/provider-contract",
      );
      setContract(result.contract);
      setEnabled(result.canary_enabled);
    } catch (err) {
      setEnabled(false);
      setError(getApiErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function run(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setProbe(null);
    try {
      setProbe(
        await apiClient.post<Probe>(
          `/api/market-data/provider-canary?token_id=${encodeURIComponent(token)}`,
        ),
      );
    } catch (err) {
      setError(getApiErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-label="Historical feed provider" className="space-y-3">
      <h3>Historical feed provider</h3>
      <p>Price history is indicative. It does not prove executable depth or fills.</p>
      <button type="button" className="btn-secondary" disabled={busy} onClick={inspect}>
        Inspect provider contract
      </button>
      {contract && (
        <div role="status">
          <p>Provider: {contract.provider_id}</p>
          <p>
            Version: {contract.normalization_version} · Cadence: {contract.data_granularity}
          </p>
          <p>Permitted use: {contract.permitted_use}</p>
          <p>{enabled ? "Canary enabled" : "Canary disabled"}</p>
        </div>
      )}
      <form onSubmit={run} className="flex flex-wrap items-end gap-3">
        <label>
          Provider token ID
          <input
            className="input block"
            required
            pattern="[0-9]{1,120}"
            value={token}
            onChange={(event) => setToken(event.target.value)}
            disabled={busy}
          />
        </label>
        <button className="btn-secondary" disabled={busy || !enabled}>
          Check official history
        </button>
      </form>
      {error && <p role="alert">{error}</p>}
      {probe && (
        <div role="status" className="break-all">
          <p>Readiness: {probe.health_state}</p>
          <p>Canary: {probe.canary_status}</p>
          <p>
            Source: {probe.provider_id} · Token: {probe.source_symbol}
          </p>
          <p>
            Coverage: {(probe.observed_coverage * 100).toFixed(1)}% · {probe.observed_bars}/
            {probe.expected_bars} hourly samples · Missing: {probe.missing_bars}
          </p>
          <p>Delay: {probe.stale_delay_seconds ?? "Unknown"} seconds</p>
          <p>Response checksum: {probe.raw_checksum_sha256 ?? "No response received"}</p>
          <p>Recorded observation: {probe.capture_id ?? "Unavailable"}</p>
          {probe.failure_reason && <p>Reason: {probe.failure_reason}</p>}
        </div>
      )}
    </section>
  );
}
