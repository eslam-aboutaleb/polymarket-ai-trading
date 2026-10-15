/**
 * Human-in-the-loop verdict feedback (plan 04): five stars plus an
 * optional comment, posted to the calibration feedback endpoints.
 *
 * Ratings never feed back automatically — they are a prompt-authoring
 * signal — so the control only reads and writes the caller's own row
 * and renders the community mean alongside it. The star row is a
 * `radiogroup` with roving tabindex (arrow keys move and select,
 * Enter saves, Home/End jump to the ends) so it is reachable without
 * a mouse; the comment textarea and the action buttons appear only
 * once a rating is in flight or already saved.
 *
 * `assessmentId` is `assessments.id`, which `/forecasts` does not yet
 * serialise: while it is missing the control renders disabled and says
 * why, rather than failing on the first save.
 *
 * @module components/VerdictFeedbackControl
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { getApiErrorMessage } from "../utils/apiError";
import { Banner, useToast } from "./ui";
import {
  calibrationService,
  VerdictFeedbackCommunity,
  VerdictFeedbackMine,
} from "../services/calibrationService";

const STARS = [1, 2, 3, 4, 5];

interface VerdictFeedbackControlProps {
  assessmentId: number | null | undefined;
  /** Screen-reader context, e.g. "verdict for market 0x1f…ab". */
  contextLabel?: string;
}

export default function VerdictFeedbackControl({
  assessmentId,
  contextLabel,
}: VerdictFeedbackControlProps) {
  const [mine, setMine] = useState<VerdictFeedbackMine | null>(null);
  const [community, setCommunity] = useState<VerdictFeedbackCommunity | null>(null);
  const [draft, setDraft] = useState<number | null>(null);
  const [comment, setComment] = useState("");
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const [error, setError] = useState<string | null>(null);
  const starRefs = useRef<(HTMLButtonElement | null)[]>([]);

  useEffect(() => {
    if (assessmentId == null) {
      setMine(null);
      setCommunity(null);
      setDraft(null);
      setComment("");
      return;
    }
    let active = true;
    setLoading(true);
    calibrationService
      .getVerdictFeedback(assessmentId)
      .then((data) => {
        if (!active) return;
        setMine(data.mine);
        setCommunity(data.community);
        setDraft(data.mine?.rating ?? null);
        setComment(data.mine?.comment ?? "");
      })
      .catch((err) => {
        if (active) setError(getApiErrorMessage(err, "Could not load your rating"));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [assessmentId]);

  const save = useCallback(async () => {
    if (assessmentId == null || draft == null) return;
    setBusy(true);
    setError(null);
    try {
      const result = await calibrationService.submitVerdictFeedback(assessmentId, {
        rating: draft,
        comment: comment.trim() || null,
      });
      setMine({ rating: result.rating, comment: result.comment });
      setDraft(result.rating);
      setComment(result.comment ?? "");
      toast.success(`Saved your ${result.rating}-star rating`);
      const refreshed = await calibrationService.getVerdictFeedback(assessmentId);
      setCommunity(refreshed.community);
    } catch (err) {
      setError(getApiErrorMessage(err, "Could not save your rating"));
    } finally {
      setBusy(false);
    }
  }, [assessmentId, comment, draft]);

  const withdraw = useCallback(async () => {
    if (assessmentId == null) return;
    setBusy(true);
    setError(null);
    try {
      await calibrationService.withdrawVerdictFeedback(assessmentId);
      setMine(null);
      setDraft(null);
      setComment("");
      toast.success("Rating withdrawn");
      const refreshed = await calibrationService.getVerdictFeedback(assessmentId);
      setCommunity(refreshed.community);
    } catch (err) {
      setError(getApiErrorMessage(err, "Could not withdraw your rating"));
    } finally {
      setBusy(false);
    }
  }, [assessmentId]);

  const groupLabel = contextLabel
    ? `Rate this verdict — ${contextLabel}`
    : "Rate this verdict from 1 to 5 stars";
  const disabled = assessmentId == null || busy;
  // Nothing saved means nothing checked; focus still lands on the first
  // star so the group is reachable with a single Tab.
  const selectedRating = draft ?? mine?.rating ?? null;
  const focusTarget = selectedRating ?? 1;
  const showDetails = draft != null || mine != null;

  const handleStarKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (disabled) return;
    const current = selectedRating ?? 0;
    let next: number | null = null;
    switch (event.key) {
      case "ArrowRight":
      case "ArrowUp":
        next = Math.min(STARS.length, (current || 0) + 1);
        break;
      case "ArrowLeft":
      case "ArrowDown":
        next = Math.max(1, (current || 1) - 1);
        break;
      case "Home":
        next = 1;
        break;
      case "End":
        next = STARS.length;
        break;
      case "Enter":
        event.preventDefault();
        void save();
        return;
      default:
        return;
    }
    event.preventDefault();
    setError(null);
    setDraft(next);
    starRefs.current[next - 1]?.focus();
  };

  return (
    <div className="min-w-[170px] space-y-1.5">
      <div role="radiogroup" aria-label={groupLabel} className="flex items-center gap-0.5">
        {STARS.map((star) => {
          const filled = (selectedRating ?? 0) >= star;
          return (
            <button
              key={star}
              ref={(node) => {
                starRefs.current[star - 1] = node;
              }}
              type="button"
              role="radio"
              aria-checked={selectedRating === star}
              aria-disabled={disabled || undefined}
              disabled={disabled}
              tabIndex={star === focusTarget ? 0 : -1}
              aria-label={`${star} star${star === 1 ? "" : "s"}`}
              title={assessmentId == null ? "Rating needs the verdict id" : `${star}/5`}
              onClick={() => {
                setError(null);
                setDraft(star);
              }}
              onKeyDown={handleStarKeyDown}
              className={`p-0.5 rounded transition disabled:cursor-not-allowed ${
                filled ? "text-[var(--accent)]" : "text-muted hover:text-[var(--accent-strong)]"
              }`}
            >
              <span aria-hidden="true" className="text-lg leading-none">
                {filled ? "★" : "☆"}
              </span>
            </button>
          );
        })}
        <span className="text-xs text-soft ml-1">
          {loading ? "…" : mine ? `you: ${mine.rating}/5` : "not rated"}
        </span>
      </div>

      {assessmentId == null && (
        <p className="text-xs text-muted">
          Rating needs the verdict id, which the forecasts list does not return yet.
        </p>
      )}

      {showDetails && (
        <>
          <textarea
            value={comment}
            onChange={(event) => setComment(event.target.value)}
            rows={2}
            maxLength={2000}
            disabled={disabled}
            aria-label="Optional comment about this verdict"
            placeholder="Optional comment — why this rating?"
            className="w-full px-2 py-1 rounded-md bg-[var(--bg-soft)] border border-[var(--line)] text-white text-xs mono focus:border-[var(--accent)] focus:outline-none disabled:opacity-60"
          />
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => void save()}
              disabled={disabled || draft == null}
              className="btn-primary text-xs px-2 py-1"
            >
              {busy ? "Saving…" : "Save"}
            </button>
            {mine && (
              <button
                type="button"
                onClick={() => void withdraw()}
                disabled={disabled}
                className="btn-muted text-xs px-2 py-1"
              >
                Withdraw
              </button>
            )}
          </div>
        </>
      )}

      <p className="text-xs text-soft">
        {community && community.count > 0
          ? `Community ${community.mean_rating == null ? "—" : community.mean_rating.toFixed(1)}/5` +
            ` from ${community.count} rating${community.count === 1 ? "" : "s"}`
          : "No community ratings yet"}
      </p>

      {error && (
        <Banner variant="error" className="text-xs" onDismiss={() => setError(null)}>
          {error}
        </Banner>
      )}
    </div>
  );
}
