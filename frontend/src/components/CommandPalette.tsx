/**
 * Voice/chat command palette: ask a question in plain language, watch the
 * orchestrator answer it step by step.
 *
 * The palette is a dialog (`role="dialog"`, `aria-modal="true"`) with a
 * trapped focus ring, Escape to close, focus returned to the trigger, and
 * arrow-key navigation over the query list. It opens with Cmd/Ctrl-K *and*
 * a visible trigger button, because a keyboard-only entry point is not a
 * usable one.
 *
 * Answers stream: `runPaletteQuery` (see `commandPaletteService`) routes the
 * question to one of Plan 01's planners and hands the SSE stream to
 * `OrchestratorTimeline`, so steps appear as they complete and a failure
 * mid-run keeps whatever already arrived. Every suggested next step is a
 * `PaletteActionLink` — a router link to a read-only page. There is no code
 * path from this component to an order endpoint, by type rather than by
 * convention.
 *
 * @module components/CommandPalette
 */

import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";
import OrchestratorTimeline from "./OrchestratorTimeline";
import { useVoiceInput } from "../hooks/useVoiceInput";
import { getApiErrorMessage, sanitizeAIError } from "../utils/apiError";
import {
  EXAMPLE_QUERIES,
  PALETTE_INTENT_LABEL,
  PALETTE_LINKS,
  PALETTE_PLANNED_STEPS,
  classifyQuery,
  loadRecentQueries,
  rememberQuery,
  runPaletteQuery,
  type PaletteActionLink,
  type PalettePlan,
  type PaletteRunCallbacks,
} from "../services/commandPaletteService";
import type { OrchestrationStep } from "../services/orchestrationService";

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

function shortcutLabel(): string {
  if (typeof navigator === "undefined") return "Ctrl+K";
  return navigator.platform.toLowerCase().includes("mac") ? "⌘K" : "Ctrl+K";
}

/* ─── Dialog ─── */

export interface CommandPaletteDialogProps {
  /** Called on Escape, overlay click or the close button. */
  onClose: () => void;
}

/**
 * The palette dialog itself, portal-mounted so no transformed ancestor can
 * break its fixed overlay.
 */
export function CommandPaletteDialog({ onClose }: CommandPaletteDialogProps) {
  const titleId = useId();
  const hintId = useId();
  const statusId = useId();
  const inputId = useId();

  const dialogRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  /** True once the current stream was cancelled, so its error is not shown. */
  const abortedRef = useRef(false);
  /** Synchronous double-submit guard: state updates are async, a click and
   *  an Enter in the same tick would both pass a `streaming` check. */
  const inFlightRef = useRef(false);

  const [query, setQuery] = useState("");
  const [steps, setSteps] = useState<OrchestrationStep[]>([]);
  const [plannedSteps, setPlannedSteps] = useState<string[]>([]);
  const [synthesis, setSynthesis] = useState("");
  const [partial, setPartial] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [plan, setPlan] = useState<PalettePlan | null>(null);
  const [recent, setRecent] = useState<string[]>(() => loadRecentQueries());
  const [activeIndex, setActiveIndex] = useState(-1);

  const voice = useVoiceInput();
  const voiceBaseRef = useRef("");

  const finishRun = useCallback(() => {
    inFlightRef.current = false;
    setStreaming(false);
  }, []);

  const callbacks = useMemo<PaletteRunCallbacks>(
    () => ({
      onStep: (step) => setSteps((prev) => [...prev, step]),
      onRecommendation: (text, isPartial) => {
        setSynthesis(text);
        setPartial(isPartial);
      },
      onDone: () => finishRun(),
      onError: (message) => {
        // `streamSSE` reports an aborted stream as an error; a deliberate
        // cancel or a closed dialog must not raise an alert.
        if (abortedRef.current) {
          finishRun();
          return;
        }
        setError(sanitizeAIError(message));
        finishRun();
      },
    }),
    [finishRun],
  );

  // Suggestions only make sense before the first run of a query; afterwards
  // the timeline and the answer own the panel.
  const suggestions = useMemo(() => {
    const seen = new Set<string>();
    return [...recent, ...EXAMPLE_QUERIES].filter((item) => {
      const key = item.toLowerCase();
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }, [recent]);

  const submit = useCallback(
    async (raw?: string) => {
      const text = (raw ?? query).trim();
      if (!text || inFlightRef.current) return;

      // Classify up front so the routing and the pending steps are visible
      // while the sweep seed fetch runs. `runPaletteQuery` classifies the
      // same pure function again — cheap, and keeps one source of truth.
      const preview = classifyQuery(text);
      if (preview.blocked) {
        setError(preview.blocked);
        return;
      }

      abortRef.current?.abort();
      inFlightRef.current = true;
      abortedRef.current = false;
      setSteps([]);
      setSynthesis("");
      setPartial(false);
      setError(null);
      setPlan(null);
      setActiveIndex(-1);
      setPlannedSteps([...PALETTE_PLANNED_STEPS[preview.intent]]);
      setStreaming(true);

      try {
        const run = await runPaletteQuery(text, callbacks);
        abortRef.current = run.controller;
        setPlan(run.plan);
        setRecent(rememberQuery(text));
      } catch (err) {
        setError(getApiErrorMessage(err, "That question could not be started."));
        finishRun();
      }
    },
    [callbacks, query],
  );

  const stop = useCallback(() => {
    abortedRef.current = true;
    abortRef.current?.abort();
    abortRef.current = null;
    finishRun();
  }, [finishRun]);

  // Dictation replaces the query while it is listening, then leaves the
  // final transcript in place for the user to confirm and submit.
  useEffect(() => {
    if (!voice.listening) return;
    const base = voiceBaseRef.current;
    const spoken = [voice.transcript, voice.interim].filter(Boolean).join(" ");
    setQuery(base && spoken ? `${base} ${spoken}` : spoken);
  }, [voice.listening, voice.transcript, voice.interim]);

  // Never leave a stream running behind a closed dialog.
  useEffect(
    () => () => {
      abortedRef.current = true;
      abortRef.current?.abort();
      abortRef.current = null;
    },
    [],
  );

  // Focus the input on open; trap Tab and close on Escape.
  useEffect(() => {
    inputRef.current?.focus();

    const node = dialogRef.current;
    if (!node) return;

    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;

      const focusable = Array.from(node.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
      if (focusable.length === 0) return;

      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;

      if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      } else if (event.shiftKey && (active === first || !node.contains(active))) {
        event.preventDefault();
        last.focus();
      }
    };

    node.addEventListener("keydown", onKeyDown);
    return () => node.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const onInputKeyDown = (event: ReactKeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      // Before a run the arrows walk the query list; after one they walk the
      // result links, so the answer's next steps are keyboard reachable too.
      if (!showSuggestions) {
        focusAction(event.key === "ArrowDown" ? 0 : -1);
        return;
      }
      if (suggestions.length === 0) return;
      const delta = event.key === "ArrowDown" ? 1 : -1;
      setActiveIndex((prev) => {
        const next = prev + delta;
        if (next < -1) return suggestions.length - 1;
        if (next >= suggestions.length) return -1;
        return next;
      });
      return;
    }

    if (event.key === "Enter") {
      event.preventDefault();
      void submit(activeIndex >= 0 ? suggestions[activeIndex] : query);
    }
  };

  const actions: readonly PaletteActionLink[] = plan?.actions ?? [];
  const routingLabel = plan ? PALETTE_INTENT_LABEL[plan.classification.intent] : null;
  const shortcut = useMemo(() => shortcutLabel(), []);
  // Suggestions stay available while nothing has run yet — including after a
  // question was rejected — and give way to the timeline once a run starts.
  const hasResults = steps.length > 0 || streaming || synthesis.length > 0;
  const showSuggestions = !hasResults;

  const actionsRef = useRef<HTMLDivElement | null>(null);

  const actionLinks = (): HTMLElement[] =>
    Array.from(actionsRef.current?.querySelectorAll<HTMLElement>("a[href]") ?? []);

  /** Focus one of the result links; a negative index means "the last one". */
  const focusAction = (index: number) => {
    const links = actionLinks();
    if (links.length === 0) return;
    const target = index < 0 ? links.length - 1 : Math.min(index, links.length - 1);
    links[target].focus();
  };

  const onActionsKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    const links = actionLinks();
    if (links.length === 0) return;
    event.preventDefault();

    const current = links.indexOf(document.activeElement as HTMLElement);
    if (event.key === "ArrowDown") {
      if (current < links.length - 1) links[current + 1].focus();
      else inputRef.current?.focus();
      return;
    }
    if (current > 0) links[current - 1].focus();
    else inputRef.current?.focus();
  };

  const statusText = streaming
    ? `Running ${routingLabel ?? "the plan"} — ${steps.length} of ${plannedSteps.length} steps complete.`
    : error
      ? `The answer could not be completed: ${error}`
      : synthesis
        ? partial
          ? "Answer ready, partial — one or more steps failed."
          : "Answer ready."
        : "";

  return createPortal(
    <div className="fixed inset-0 z-[90] flex items-start justify-center px-4 pt-[6vh] pb-4">
      <div className="absolute inset-0 modal-overlay" onClick={onClose} aria-hidden="true" />

      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={hintId}
        aria-busy={streaming}
        className="relative z-[91] w-full max-w-3xl surface-panel flex flex-col max-h-[88vh] overflow-hidden"
      >
        <div className="p-4 border-b border-[var(--line)] space-y-3">
          <div className="flex items-start justify-between gap-3">
            <div>
              <h2 id={titleId} className="text-lg font-semibold">
                Ask the AI
              </h2>
              <p id={hintId} className="text-xs text-soft mt-0.5">
                Plain-language questions are routed through the AI Orchestrator
                {" — "}
                {shortcut} reopens this palette from anywhere in the app.
              </p>
            </div>
            <button
              type="button"
              onClick={onClose}
              className="btn-muted text-xs"
              aria-label="Close the command palette"
            >
              Esc
            </button>
          </div>

          <div className="flex items-center gap-2">
            <label className="sr-only" htmlFor={inputId}>
              Ask a question about a market, a trader or an opportunity
            </label>
            <input
              id={inputId}
              ref={inputRef}
              className="input-field text-sm"
              type="text"
              autoComplete="off"
              enterKeyHint="search"
              placeholder="Should I buy YES on the Fed rate decision market?"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={onInputKeyDown}
              disabled={streaming}
              role="combobox"
              aria-expanded={showSuggestions && activeIndex >= 0}
              aria-controls={showSuggestions ? `${inputId}-suggestions` : undefined}
              aria-activedescendant={
                showSuggestions && activeIndex >= 0
                  ? `${inputId}-suggestion-${activeIndex}`
                  : undefined
              }
            />

            {voice.supported ? (
              <button
                type="button"
                onClick={() => {
                  if (voice.listening) {
                    voice.stop();
                    return;
                  }
                  voiceBaseRef.current = query;
                  voice.clear();
                  voice.start();
                }}
                className={
                  voice.listening
                    ? "btn-danger text-xs whitespace-nowrap"
                    : "btn-muted text-xs whitespace-nowrap"
                }
                aria-pressed={voice.listening}
                disabled={streaming}
                title={voice.listening ? "Stop listening" : "Ask with your voice"}
              >
                {voice.listening ? (
                  <>
                    <span
                      className="inline-block w-2 h-2 rounded-full bg-[var(--danger)] animate-pulse mr-1.5 align-middle"
                      aria-hidden="true"
                    />
                    Listening…
                  </>
                ) : (
                  "🎙 Voice"
                )}
              </button>
            ) : (
              <button
                type="button"
                className="btn-muted text-xs whitespace-nowrap"
                disabled
                title={voice.unsupportedReason ?? "Voice input is unavailable in this browser."}
              >
                🎙 Voice
              </button>
            )}

            <button
              type="button"
              className="btn-primary text-xs whitespace-nowrap"
              onClick={() => void submit()}
              disabled={streaming || query.trim().length === 0}
            >
              {streaming ? "Streaming…" : "Ask"}
            </button>
            {streaming && (
              <button type="button" className="btn-muted text-xs whitespace-nowrap" onClick={stop}>
                Cancel
              </button>
            )}
          </div>

          <div className="flex items-center gap-2 flex-wrap text-xs text-soft">
            {routingLabel && <span className="chip chip-accent">{routingLabel}</span>}
            {streaming && <span className="chip">streaming</span>}
            {partial && <span className="chip chip-warning">partial</span>}
            {plan && !streaming && (
              <span>
                {plan.classification.reason}
                {plan.marketCount > 0 && ` Seeded with the ${plan.marketCount} busiest markets.`}
              </span>
            )}
          </div>
        </div>

        <div className="flex-1 overflow-y-auto scroll-soft p-4 space-y-4">
          {showSuggestions && (
            <section className="space-y-3">
              <div>
                <h3 className="text-xs font-bold uppercase tracking-wider text-muted mb-2">
                  {recent.length > 0 ? "Recent & examples" : "Try one of these"}
                </h3>
                <ul id={`${inputId}-suggestions`} role="listbox" aria-label="Example questions">
                  {suggestions.map((item, index) => (
                    <li key={item}>
                      <button
                        type="button"
                        id={`${inputId}-suggestion-${index}`}
                        role="option"
                        aria-selected={activeIndex === index}
                        className={`w-full text-left px-3 py-2 rounded-md text-sm transition ${
                          activeIndex === index
                            ? "bg-[var(--accent-soft)] text-[var(--accent)]"
                            : "text-soft hover:bg-[var(--bg-soft)] hover:text-[var(--text-primary)]"
                        }`}
                        onClick={() => void submit(item)}
                      >
                        {item}
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
              <p className="text-xs text-muted">
                Voice input is optional and, where the browser supports it, sends audio to the
                browser vendor's recognition service. The palette only ever analyses — it never
                places an order.
              </p>
            </section>
          )}

          {(steps.length > 0 || streaming) && (
            <section className="space-y-2" aria-label="Plan steps">
              <OrchestratorTimeline
                steps={steps}
                running={streaming}
                partial={partial}
                plannedSteps={plannedSteps}
              />
            </section>
          )}

          {synthesis && (
            <section className="space-y-2" aria-live="polite" aria-label="Answer">
              <h3 className="text-sm font-semibold">Answer</h3>
              <pre className="text-xs whitespace-pre-wrap break-words mono surface-soft p-3">
                {synthesis}
              </pre>
            </section>
          )}

          {error && (
            <div className="alert-error p-3 rounded text-sm" role="alert">
              <p>{error}</p>
              <button
                type="button"
                className="btn-muted text-xs mt-2"
                onClick={() => void submit()}
                disabled={streaming}
              >
                Try again
              </button>
            </div>
          )}

          {voice.error && (
            <div className="alert-error p-3 rounded text-sm" role="alert">
              {voice.error}
            </div>
          )}
        </div>

        <div className="p-4 border-t border-[var(--line)] space-y-3">
          {actions.length > 0 && (
            <div
              ref={actionsRef}
              onKeyDown={onActionsKeyDown}
              className="flex flex-wrap gap-2"
              aria-label="Suggested next steps"
              role="group"
            >
              {actions.map((action) => (
                <Link
                  key={action.id}
                  to={action.to}
                  onClick={onClose}
                  title={action.hint}
                  className="btn-muted text-xs"
                >
                  {action.label}
                </Link>
              ))}
            </div>
          )}

          <div className="flex items-center justify-between gap-3 flex-wrap">
            <p id={statusId} role="status" aria-live="polite" className="text-xs text-soft">
              {statusText}
            </p>
            <div className="flex items-center gap-2">
              <span className="chip chip-success">read-only</span>
              <Link to={PALETTE_LINKS.orchestrator} onClick={onClose} className="text-xs">
                Open the Orchestrator
              </Link>
            </div>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}

/* ─── Page ─── */

/**
 * The palette page: a visible trigger plus the dialog it opens.
 *
 * The Cmd/Ctrl-K listener is registered here, so the shortcut lives as long
 * as this route is mounted — the keyboard path and the button path open the
 * exact same dialog.
 */
export default function CommandPalette() {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const shortcut = useMemo(() => shortcutLabel(), []);

  const closePalette = useCallback(() => {
    setOpen(false);
    // Keyboard focus must not be dropped at the top of the document.
    triggerRef.current?.focus();
  }, []);

  useEffect(() => {
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (!(event.metaKey || event.ctrlKey)) return;
      if (event.key?.toLowerCase() !== "k") return;
      event.preventDefault();
      setOpen((prev) => {
        if (prev) {
          // Closing from the keyboard must also hand focus back to the
          // trigger, exactly like Escape and the close button do.
          triggerRef.current?.focus();
        }
        return !prev;
      });
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  return (
    <div className="page-enter space-y-6">
      <div>
        <h1 className="text-3xl font-bold">Ask the AI</h1>
        <p className="text-soft text-sm mt-1">
          A voice/chat front end for the AI Orchestrator: type or dictate a question and the answer
          streams back one analysis step at a time.
        </p>
      </div>

      <section className="surface-panel p-5 space-y-4">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div>
            <h2 className="text-lg font-semibold">Command palette</h2>
            <p className="text-xs text-soft mt-1">
              Press{" "}
              <span className="mono text-xs bg-[var(--bg-soft)] px-1.5 py-0.5 rounded">
                {shortcut}
              </span>{" "}
              anywhere on this page, or use the button below.
            </p>
          </div>
          <button
            ref={triggerRef}
            type="button"
            className="btn-primary text-sm"
            onClick={() => setOpen(true)}
            aria-keyshortcuts="Meta+K Control+K"
            aria-haspopup="dialog"
          >
            Open palette
          </button>
        </div>

        <ul className="text-sm text-soft space-y-1.5 list-disc list-inside">
          <li>
            Questions are routed to a Market Deep Dive, Opportunity Sweep or Trader Diligence plan.
          </li>
          <li>Steps stream in as they finish, so a partial answer is visible immediately.</li>
          <li>Every suggested next step is a link to an existing page — nothing here can trade.</li>
          <li>Voice input is optional and only appears where your browser supports it.</li>
        </ul>
      </section>

      <section className="surface-panel p-5 space-y-2">
        <h2 className="text-lg font-semibold">What it cannot do</h2>
        <p className="text-sm text-soft">
          The palette reads and explains. It cannot place, size, cancel or approve an order — the
          orchestration route it calls synthesises analysis and stops at a written trade plan. Act
          on an answer by opening the{" "}
          <Link to={PALETTE_LINKS.orchestrator} className="underline">
            Orchestrator
          </Link>{" "}
          or{" "}
          <Link to={PALETTE_LINKS.markets} className="underline">
            Markets
          </Link>
          .
        </p>
      </section>

      {open && <CommandPaletteDialog onClose={closePalette} />}
    </div>
  );
}
