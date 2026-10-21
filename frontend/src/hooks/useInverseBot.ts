/**
 * Inverse-position-bot state for a positions surface.
 *
 * Dashboard and TradeHistory each hand-rolled this money-path logic (list fetch,
 * toggle, manual evaluation, AI explanation) and the copies drifted apart. The
 * hook owns all of it: it fetches the tracked positions through
 * `inverseBotService`, gates every enable/run behind a stored trading key (the
 * stricter of the two previous behaviours), and explains the latest evaluation
 * through `analysisService.quickAnalysis`.
 *
 * @module hooks/useInverseBot
 */

import { useCallback, useRef, useState } from "react";
import { useQuery } from "react-query";
import {
  inverseBotService,
  InverseBotPosition,
  InverseBotSizeOverride,
} from "../services/inverseBotService";
import { analysisService } from "../services/analysisService";
import { getApiErrorMessage } from "../utils/apiError";
import { extractFirstJsonObject } from "../components/PositionAnalysisPopup";
import {
  findInverseBotForPosition,
  indexedWebSourceLabel,
  indexedWebSummaryText,
  inverseBotTokenId,
  PositionRef,
} from "../utils/inverseBot";
import { useDismissableError } from "./useDismissableError";
import { useTradingKeyStatus } from "./useTradingKeyStatus";

/** Query key of the tracked inverse-bot position list. */
export const inverseBotPositionsQueryKey = ["inverse-bot", "positions"] as const;

export type InverseExplanationView = {
  decision: string;
  why: string;
  confidence: string;
  market_signal: string;
  web_signal: string;
  x_signal: string;
  next_checks: string;
  updated_at: string;
};

export interface UseInverseBotOptions {
  /** Fetch the tracked list only when true (auth gate, visible tab). */
  enabled?: boolean;
  /** Receives action errors ("Failed to disable inverse bot", …) for the local banner. */
  onError?: (message: string) => void;
}

export interface UseInverseBotResult {
  /** Tracked inverse-bot rows. */
  positions: InverseBotPosition[];
  /** True when the session has a trading key; null while loading/unknown. */
  hasTradingKey: boolean | null;
  /** List-load failure message for the banner. */
  listError: string | null;
  /** Hides the list error until the next failed fetch. */
  dismissListError: () => void;
  /** Re-fetches the tracked list. */
  refresh: () => void;
  /** Token id of the position with an in-flight action. */
  busyToken: string | null;
  /** Token id whose explanation tooltip is hovered. */
  infoHoverToken: string | null;
  setInfoHoverToken: (tokenId: string | null) => void;
  /** Token id whose explanation tooltip is pinned open. */
  infoPinnedToken: string | null;
  setInfoPinnedToken: (tokenId: string | null) => void;
  /** AI explanation per token id. */
  explanationByToken: Record<string, InverseExplanationView>;
  /** Token id whose explanation is being generated. */
  explainLoadingToken: string | null;
  /** Explanation failure per token id. */
  explainErrorByToken: Record<string, string>;
  /** Creates/updates the bot config for a position. */
  upsertForPosition: (
    pos: PositionRef,
    overrides?: Partial<{
      enabled: boolean;
      size_mode_override: InverseBotSizeOverride;
      fixed_amount_override: number | null;
    }>,
  ) => Promise<boolean | null>;
  /** Enables (creating the row) or disables the bot for a position. */
  toggle: (pos: PositionRef, enabled: boolean) => Promise<void>;
  /** Runs an on-demand evaluation for a position. */
  runNow: (pos: PositionRef) => Promise<void>;
  /** Lazily generates the explanation tooltip content for a token. */
  ensureExplanation: (
    tokenId: string,
    pos: PositionRef,
    inverse?: InverseBotPosition,
  ) => Promise<void>;
  /** True once a gated action asked the user to connect a trading key. */
  tradingKeyPrompt: boolean;
}

export function useInverseBot(options: UseInverseBotOptions = {}): UseInverseBotResult {
  const { enabled = true, onError } = options;
  const { hasTradingKey } = useTradingKeyStatus();

  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;
  const reportError = useCallback((message: string) => {
    onErrorRef.current?.(message);
  }, []);

  const listQuery = useQuery<InverseBotPosition[]>(
    inverseBotPositionsQueryKey,
    () => inverseBotService.listInverseBotPositions(),
    { enabled, staleTime: 0 },
  );
  const { data: positions = [], refetch } = listQuery;
  const listErrorState = useDismissableError(
    listQuery.error,
    listQuery.isError,
    listQuery.errorUpdatedAt,
    "Failed to load inverse bot positions",
  );

  // Mutations read the freshest list without re-creating the action callbacks.
  const positionsRef = useRef(positions);
  positionsRef.current = positions;

  const [busyToken, setBusyToken] = useState<string | null>(null);
  const [infoHoverToken, setInfoHoverToken] = useState<string | null>(null);
  const [infoPinnedToken, setInfoPinnedToken] = useState<string | null>(null);
  const [explanationByToken, setExplanationByToken] = useState<
    Record<string, InverseExplanationView>
  >({});
  const [explainLoadingToken, setExplainLoadingToken] = useState<string | null>(null);
  const [explainErrorByToken, setExplainErrorByToken] = useState<Record<string, string>>({});
  const [tradingKeyPrompt, setTradingKeyPrompt] = useState(false);

  const clearExplanation = useCallback((tokenId: string) => {
    setExplanationByToken((prev) => {
      const next = { ...prev };
      delete next[tokenId];
      return next;
    });
    setExplainErrorByToken((prev) => {
      const next = { ...prev };
      delete next[tokenId];
      return next;
    });
  }, []);

  const refresh = useCallback(() => {
    void refetch();
  }, [refetch]);

  const upsertForPosition = useCallback<UseInverseBotResult["upsertForPosition"]>(
    async (pos, overrides) => {
      const tokenId = inverseBotTokenId(pos);
      const conditionId = String(pos.condition_id || pos.conditionId || "");
      if (!tokenId || !conditionId) {
        reportError("Missing token or condition id for inverse bot.");
        return null;
      }

      const existing = findInverseBotForPosition(positionsRef.current, pos);
      const enabled = overrides?.enabled ?? existing?.enabled ?? true;
      // Enabling the bot without a trading key would arm trades that can never
      // execute — prompt for the key instead (same gate on both surfaces).
      if (enabled && hasTradingKey === false) {
        setTradingKeyPrompt(true);
        return null;
      }

      const payload = {
        token_id: tokenId,
        condition_id: conditionId,
        market_title: String(pos.title || pos.market || ""),
        outcome: String(pos.outcome || ""),
        enabled,
        size_mode_override:
          overrides?.size_mode_override ?? existing?.size_mode_override ?? "inherit",
        fixed_amount_override:
          overrides?.fixed_amount_override ?? existing?.fixed_amount_override ?? null,
      };

      setBusyToken(tokenId);
      try {
        await inverseBotService.upsertInverseBotPosition(payload);
        refresh();
        return true;
      } catch (err: unknown) {
        reportError(getApiErrorMessage(err, "Failed to update inverse bot settings"));
        return false;
      } finally {
        setBusyToken(null);
      }
    },
    [hasTradingKey, refresh, reportError],
  );

  const toggle = useCallback<UseInverseBotResult["toggle"]>(
    async (pos, enabled) => {
      const tokenId = inverseBotTokenId(pos);
      const existing = findInverseBotForPosition(positionsRef.current, pos);
      if (!enabled) {
        clearExplanation(tokenId);
      }
      if (!enabled && existing?.id) {
        setBusyToken(tokenId);
        try {
          await inverseBotService.disableInverseBotPosition(existing.id);
          refresh();
        } catch (err: unknown) {
          reportError(getApiErrorMessage(err, "Failed to disable inverse bot"));
        } finally {
          setBusyToken(null);
        }
        return;
      }
      await upsertForPosition(pos, { enabled });
    },
    [clearExplanation, refresh, reportError, upsertForPosition],
  );

  const runNow = useCallback<UseInverseBotResult["runNow"]>(
    async (pos) => {
      const tokenId = inverseBotTokenId(pos);
      if (hasTradingKey === false) {
        setTradingKeyPrompt(true);
        return;
      }
      clearExplanation(tokenId);
      setBusyToken(tokenId);
      try {
        let rowId = findInverseBotForPosition(positionsRef.current, pos)?.id;
        if (!rowId) {
          const conditionId = String(pos.condition_id || pos.conditionId || "");
          const created = await inverseBotService.upsertInverseBotPosition({
            token_id: tokenId,
            condition_id: conditionId,
            market_title: String(pos.title || pos.market || ""),
            outcome: String(pos.outcome || ""),
            enabled: true,
            size_mode_override: "inherit",
            fixed_amount_override: null,
          });
          rowId = created.id;
        }
        if (!rowId) {
          throw new Error("Position is not enabled for inverse bot");
        }
        const result = await inverseBotService.evaluateInverseBotPosition(rowId);
        if (!result.success) {
          throw new Error(result.detail || "Manual evaluation failed");
        }
        refresh();
      } catch (err: unknown) {
        reportError(getApiErrorMessage(err, "Failed to run inverse evaluation"));
      } finally {
        setBusyToken(null);
      }
    },
    [clearExplanation, hasTradingKey, refresh, reportError],
  );

  const ensureExplanation = useCallback<UseInverseBotResult["ensureExplanation"]>(
    async (tokenId, pos, inverse) => {
      if (!inverse) return;
      if (explanationByToken[tokenId] || explainLoadingToken === tokenId) {
        return;
      }
      if (
        !inverse.last_evaluated_at &&
        inverse.last_confidence == null &&
        !inverse.last_reasoning
      ) {
        setExplanationByToken((prev) => ({
          ...prev,
          [tokenId]: {
            decision: "Not evaluated yet",
            why: "Click Run Now and the bot will generate the first evaluation.",
            confidence: "N/A",
            market_signal: "N/A",
            web_signal: "N/A",
            x_signal: "N/A",
            next_checks: "Run a manual evaluation.",
            updated_at: "N/A",
          },
        }));
        return;
      }

      const fallbackExplain: InverseExplanationView = {
        decision: String(inverse.last_recommendation || "hold"),
        why: String(inverse.last_reasoning || "No model reasoning available."),
        confidence:
          inverse.last_confidence != null ? `${inverse.last_confidence.toFixed(0)}%` : "Unknown",
        market_signal: String(inverse.last_signal || "No signal captured"),
        web_signal: String(inverse.last_web_summary || "No web summary"),
        x_signal: [
          indexedWebSourceLabel(inverse),
          String(inverse.last_x_summary || indexedWebSummaryText(inverse)),
        ].join(" — "),
        next_checks: "Monitor next cycle and confirm confidence stays stable.",
        updated_at: inverse.last_evaluated_at
          ? new Date(inverse.last_evaluated_at).toLocaleString()
          : "Unknown",
      };

      const prompt = [
        "Convert this inverse position bot evaluation into strict JSON only.",
        "Do NOT use markdown and do NOT use bullet points.",
        "Use concise plain-English text for each field.",
        "Return exactly this JSON schema:",
        '{"decision":"", "why":"", "confidence":"", "market_signal":"", "web_signal":"", "x_signal":"", "next_checks":"", "updated_at":""}',
        `Market: ${String(pos.title || pos.market || inverse.market_title || "Unknown market")}`,
        `Held outcome: ${String(pos.outcome || inverse.outcome || "Unknown")}`,
        `Bot recommendation: ${String(inverse.last_recommendation || "unknown")}`,
        `Confidence: ${inverse.last_confidence != null ? `${inverse.last_confidence.toFixed(0)}%` : "unknown"}`,
        `Status: ${String(inverse.status || "unknown")}`,
        `Reasoning: ${String(inverse.last_reasoning || "No model reasoning available.")}`,
        `Web summary: ${String(inverse.last_web_summary || "None")}`,
        `Indexed web summary (partial coverage, not an authenticated X feed): ${String(
          inverse.last_x_summary || indexedWebSummaryText(inverse),
        )}`,
        `Last evaluated at: ${inverse.last_evaluated_at || "unknown"}`,
      ].join("\n");

      setExplainLoadingToken(tokenId);
      setExplainErrorByToken((prev) => ({ ...prev, [tokenId]: "" }));
      try {
        const resp = await analysisService.quickAnalysis({
          question: prompt,
          current_price: Math.max(0, Math.min(1, (inverse.last_confidence ?? 50) / 100)),
        });
        const text = String(resp?.data?.analysis || "");
        const parsed = extractFirstJsonObject(text);
        setExplanationByToken((prev) => ({
          ...prev,
          [tokenId]:
            parsed && typeof parsed === "object"
              ? {
                  decision: String(parsed.decision || fallbackExplain.decision),
                  why: String(parsed.why || fallbackExplain.why),
                  confidence: String(parsed.confidence || fallbackExplain.confidence),
                  market_signal: String(parsed.market_signal || fallbackExplain.market_signal),
                  web_signal: String(parsed.web_signal || fallbackExplain.web_signal),
                  x_signal: String(parsed.x_signal || fallbackExplain.x_signal),
                  next_checks: String(parsed.next_checks || fallbackExplain.next_checks),
                  updated_at: String(parsed.updated_at || fallbackExplain.updated_at),
                }
              : fallbackExplain,
        }));
      } catch (err: unknown) {
        setExplanationByToken((prev) => ({
          ...prev,
          [tokenId]: fallbackExplain,
        }));
        setExplainErrorByToken((prev) => ({
          ...prev,
          [tokenId]: getApiErrorMessage(err, "Failed to generate explanation"),
        }));
      } finally {
        setExplainLoadingToken((prev) => (prev === tokenId ? null : prev));
      }
    },
    [explanationByToken, explainLoadingToken],
  );

  return {
    positions,
    hasTradingKey,
    listError: listErrorState.message,
    dismissListError: listErrorState.dismiss,
    refresh,
    busyToken,
    infoHoverToken,
    setInfoHoverToken,
    infoPinnedToken,
    setInfoPinnedToken,
    explanationByToken,
    explainLoadingToken,
    explainErrorByToken,
    upsertForPosition,
    toggle,
    runNow,
    ensureExplanation,
    tradingKeyPrompt,
  };
}
