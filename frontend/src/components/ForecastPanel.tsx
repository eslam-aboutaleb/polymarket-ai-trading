/**
 * Collaborative forecast panel (plan 02): one market, one forecaster.
 *
 * A vote slider, an annotation editor and the consensus read for a
 * single `condition_id`. It is deliberately small so the market-detail
 * surfaces and the forecasting tab of the leaderboard can share it, and
 * it carries no market fetching of its own — the caller supplies the
 * `conditionId` and, optionally, the AI/market numbers it already has.
 *
 * Privacy is the design centre: a vote and a note are **private until
 * the forecaster says otherwise**, and the opt-in is an explicit
 * disclosure ("your wallet is the handle") rather than a checkbox that
 * ships ticked. The consensus read below is a *description* — AI
 * probability, weighted consensus, market price and the disagreement
 * gap — with an "informational only" strip, because a crowd average is
 * never a trade instruction.
 *
 * State lives in component `useState` and the panel calls the service
 * directly (no react-query), matching every other surface in this app.
 *
 * @module components/ForecastPanel
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { getApiErrorMessage } from "../utils/apiError";
import { Banner, useToast } from "./ui";
import {
  Annotation,
  ConsensusResponse,
  Visibility,
  collaborationService,
} from "../services/collaborationService";

interface ForecastPanelProps {
  /** Polymarket condition id of the market being forecast on. */
  conditionId: string;
  /** Optional market question, used for the panel heading only. */
  question?: string;
  /** Extra classes for the host surface (drawer, tab, page). */
  className?: string;
}

const DISCLOSURE =
  "Your vote is linked to the wallet you signed in with, and that is the handle other " +
  "forecasters see. Public votes enter the consensus and the accuracy board; private " +
  "votes stay visible to you alone. You can change this at any time by voting again.";

const ADVISORY =
  "Informational only — a consensus is a description of what other forecasters believe, " +
  "not a recommendation to trade.";

function pct(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

function signedPct(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${(value * 100).toFixed(1)}pp`;
}

function gapTone(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "text-muted";
  if (Math.abs(value) < 0.02) return "text-soft";
  return value > 0 ? "status-good" : "status-bad";
}

export default function ForecastPanel({
  conditionId,
  question,
  className = "",
}: ForecastPanelProps) {
  const toast = useToast();
  const [probability, setProbability] = useState(50);
  const [visibility, setVisibility] = useState<Visibility>("private");
  const [body, setBody] = useState("");
  const [annotation, setAnnotation] = useState<Annotation | null>(null);
  const [consensus, setConsensus] = useState<ConsensusResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [savingNote, setSavingNote] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [noteError, setNoteError] = useState<string | null>(null);
  const [noteSaved, setNoteSaved] = useState(false);
  const mounted = useRef(true);

  useEffect(
    () => () => {
      mounted.current = false;
    },
    [],
  );

  const load = useCallback(async () => {
    if (!conditionId) return;
    setLoading(true);
    setError(null);
    try {
      const [mine, board] = await Promise.allSettled([
        collaborationService.getAnnotations(conditionId),
        collaborationService.getConsensus(conditionId),
      ]);
      if (mine.status === "fulfilled") {
        const own = mine.value.annotations.find((row) => row.mine) ?? null;
        setAnnotation(own);
        setBody(own?.body ?? "");
        setVisibility(own?.visibility ?? "private");
      }
      if (board.status === "fulfilled") {
        setConsensus(board.value);
      } else {
        setConsensus(null);
        setError(getApiErrorMessage(board.reason, "Failed to load the consensus"));
      }
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Failed to load forecasts"));
    } finally {
      if (mounted.current) setLoading(false);
    }
  }, [conditionId]);

  useEffect(() => {
    setAnnotation(null);
    setBody("");
    setVisibility("private");
    setConsensus(null);
    load();
  }, [load]);

  const submitVote = async (event: React.FormEvent) => {
    event.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const vote = await collaborationService.putVote(conditionId, {
        probability: probability / 100,
        visibility,
      });
      if (!mounted.current) return;
      toast.success?.(`Forecast saved at ${(vote.probability * 100).toFixed(1)}%`);
      await load();
    } catch (err: unknown) {
      if (mounted.current) setError(getApiErrorMessage(err, "Failed to save your forecast"));
    } finally {
      if (mounted.current) setSaving(false);
    }
  };

  const submitNote = async (event: React.FormEvent) => {
    event.preventDefault();
    setSavingNote(true);
    setNoteError(null);
    setNoteSaved(false);
    try {
      const saved = await collaborationService.putAnnotation(conditionId, {
        body,
        visibility,
      });
      if (!mounted.current) return;
      setAnnotation(saved);
      setNoteSaved(true);
    } catch (err: unknown) {
      if (mounted.current) setNoteError(getApiErrorMessage(err, "Failed to save your note"));
    } finally {
      if (mounted.current) setSavingNote(false);
    }
  };

  const withdrawNote = async () => {
    setNoteError(null);
    try {
      await collaborationService.deleteAnnotation(conditionId);
      if (!mounted.current) return;
      setAnnotation(null);
      setBody("");
      setNoteSaved(false);
    } catch (err: unknown) {
      if (mounted.current) setNoteError(getApiErrorMessage(err, "Failed to withdraw your note"));
    }
  };

  return (
    <section
      className={`surface-panel p-4 space-y-4 ${className}`}
      aria-label="Collaborative forecast"
    >
      <header className="flex items-start justify-between gap-3 flex-wrap">
        <div>
          <h3 className="text-sm font-semibold text-white">Your forecast</h3>
          <p className="text-muted text-xs">{question ? question : conditionId}</p>
        </div>
        <span className="chip chip-accent">
          {consensus ? `${consensus.n_votes} public` : "no votes"}
        </span>
      </header>

      {error && (
        <Banner variant="error" onDismiss={() => setError(null)} onRetry={load}>
          {error}
        </Banner>
      )}

      <form onSubmit={submitVote} className="space-y-3">
        <div>
          <div className="flex items-center justify-between mb-1">
            <label htmlFor={`forecast-p-${conditionId}`} className="text-soft text-xs font-medium">
              Probability YES resolves true
            </label>
            <span className="mono text-sm text-white" data-testid="forecast-value">
              {(probability / 100).toFixed(2)}
            </span>
          </div>
          <input
            id={`forecast-p-${conditionId}`}
            type="range"
            min={1}
            max={99}
            step={1}
            value={probability}
            onChange={(event) => setProbability(Number(event.target.value))}
            className="w-full accent-[var(--accent)]"
          />
          <div className="flex justify-between text-muted text-[10px] mono">
            <span>1%</span>
            <span>99%</span>
          </div>
        </div>

        <fieldset className="space-y-2">
          <legend className="text-soft text-xs font-medium mb-1">
            Visibility — applies to the next vote and note you save
          </legend>
          <label className="flex items-center gap-2 text-xs text-soft">
            <input
              type="radio"
              name={`visibility-${conditionId}`}
              value="private"
              checked={visibility === "private"}
              onChange={() => setVisibility("private")}
              className="accent-[var(--accent)]"
            />
            Private — yours alone (default)
          </label>
          <label className="flex items-center gap-2 text-xs text-soft">
            <input
              type="radio"
              name={`visibility-${conditionId}`}
              value="public"
              checked={visibility === "public"}
              onChange={() => setVisibility("public")}
              className="accent-[var(--accent)]"
            />
            Public — enters the consensus and the accuracy board
          </label>
          {visibility === "public" && (
            <p className="status-warn text-[11px] border border-yellow-500/30 bg-yellow-500/10 rounded p-2">
              {DISCLOSURE}
            </p>
          )}
        </fieldset>

        <div className="flex items-center gap-2">
          <button type="submit" className="btn-accent text-xs" disabled={saving}>
            {saving ? "Saving…" : annotation ? "Revise forecast" : "Cast forecast"}
          </button>
          {consensus?.excluded_self && (
            <span className="text-muted text-[11px]">
              Your own vote is excluded from the consensus you see.
            </span>
          )}
        </div>
      </form>

      <form onSubmit={submitNote} className="space-y-2">
        <label htmlFor={`forecast-note-${conditionId}`} className="text-soft text-xs font-medium">
          Research note {annotation ? "(yours)" : ""}
        </label>
        <textarea
          id={`forecast-note-${conditionId}`}
          value={body}
          onChange={(event) => setBody(event.target.value)}
          rows={3}
          maxLength={20000}
          className="input-field text-sm"
          placeholder="Why this number? What would change your mind?"
        />
        <div className="flex items-center gap-2">
          <button type="submit" className="btn-muted text-xs" disabled={savingNote || !body.trim()}>
            {savingNote ? "Saving…" : annotation ? "Update note" : "Save note"}
          </button>
          {annotation && (
            <button type="button" className="btn-danger text-xs" onClick={withdrawNote}>
              Withdraw
            </button>
          )}
          {noteSaved && <span className="text-soft text-[11px]">Saved.</span>}
        </div>
        {noteError && (
          <Banner variant="error" onDismiss={() => setNoteError(null)}>
            {noteError}
          </Banner>
        )}
      </form>

      <div className="space-y-2 border-t border-[var(--line)] pt-3">
        <div className="flex items-center justify-between">
          <h4 className="text-soft text-xs font-semibold">Consensus</h4>
          {consensus?.cached && <span className="text-muted text-[10px]">cached</span>}
        </div>

        {loading ? (
          <p className="text-muted text-xs">Loading consensus…</p>
        ) : (
          <dl className="grid grid-cols-2 md:grid-cols-4 gap-2">
            <Stat label="AI" value={pct(consensus?.ai_probability)} />
            <Stat label="Consensus" value={pct(consensus?.consensus)} emphasis />
            <Stat label="Market" value={pct(consensus?.market_price)} />
            <Stat
              label="Gap"
              value={signedPct(consensus?.gap)}
              tone={gapTone(consensus?.gap)}
              testId="forecast-gap"
            />
          </dl>
        )}

        {consensus && consensus.n_votes === 0 && (
          <p className="text-muted text-xs">
            No public forecasts yet — the AI probability above is the only estimate.
          </p>
        )}

        {consensus && consensus.n_votes > 0 && (
          <>
            <p className="text-soft text-xs">
              Crowd vs price{" "}
              <strong className={gapTone(consensus.gap)}>{signedPct(consensus.gap)}</strong>
              {" · "}crowd vs AI{" "}
              <strong className={gapTone(consensus.ai_gap)}>{signedPct(consensus.ai_gap)}</strong>
              {" · "}spread {pct(consensus.disagreement)}
              {consensus.disagreement !== null && consensus.disagreement > 0.3 ? " (wide)" : ""}
            </p>
            <ul className="space-y-1">
              {consensus.contributors.slice(0, 8).map((row) => (
                <li key={row.user_id} className="flex items-center justify-between text-xs">
                  <span className="text-soft truncate">
                    {row.display_name || row.wallet_address || `forecaster #${row.user_id}`}
                  </span>
                  <span className="mono text-white">{pct(row.probability)}</span>
                  <span className="text-muted text-[10px] mono">w {row.weight.toFixed(2)}</span>
                </li>
              ))}
            </ul>
          </>
        )}

        {consensus?.narrative && <p className="text-muted text-[11px]">{consensus.narrative}</p>}
        <p className="text-muted text-[11px] border-t border-[var(--line)] pt-2">{ADVISORY}</p>
      </div>
    </section>
  );
}

function Stat({
  label,
  value,
  tone = "text-white",
  emphasis = false,
  testId,
}: {
  label: string;
  value: string;
  tone?: string;
  emphasis?: boolean;
  testId?: string;
}) {
  return (
    <div className="rounded-xl p-2.5 border border-[var(--line)] bg-[var(--bg-soft)]">
      <dt className="text-muted text-[10px] font-medium">{label}</dt>
      <dd
        className={`${emphasis ? "text-base" : "text-sm"} font-bold ${tone}`}
        data-testid={testId}
      >
        {value}
      </dd>
    </div>
  );
}
