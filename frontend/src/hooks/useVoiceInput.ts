/**
 * Optional dictation for the command palette, on the browser's Web Speech API.
 *
 * There is no npm package for this: `SpeechRecognition` is browser-native and
 * ships in the DOM typings of some TS versions and not others, so the few
 * members this hook touches are declared structurally below instead of
 * pulling in `@types/dom-speech-recognition`.
 *
 * Browser support matters here, so the hook degrades instead of throwing:
 *
 * - Chrome / Edge: full support, `SpeechRecognition` on `window`.
 * - Safari 14.1+ (also iOS): only `webkitSpeechRecognition`, only on HTTPS or
 *   `localhost`, and only from a user gesture — which is why `start()` is
 *   wired to the mic button and never to an effect. Interim results are less
 *   reliable, so the interim phrase is treated as a preview only and the
 *   final transcript is what lands in the input.
 * - Firefox: no recognition API at all. `supported` stays false, `start()` is
 *   a documented no-op and the palette hides the control.
 *
 * Voice is off by default in principle and enabled by a local flag: no build
 * step, no env var, just `VOICE_INPUT_ENABLED` (or the
 * `polymarket_command_palette_voice` localStorage key) to flip.
 *
 * @module hooks/useVoiceInput
 */

import { useCallback, useEffect, useRef, useState } from "react";

/** Local, env-free feature toggle. Flip to `false` to ship without voice. */
export const VOICE_INPUT_ENABLED = true;

/** localStorage override: `"on"` / `"off"` beats the flag above. */
export const VOICE_INPUT_OVERRIDE_KEY = "polymarket_command_palette_voice";

// ── Structural typings for the parts of the API we use ─────────────

interface SpeechAlternativeLike {
  transcript: string;
  confidence: number;
}

interface SpeechResultLike {
  isFinal: boolean;
  length: number;
  [index: number]: SpeechAlternativeLike;
}

interface SpeechResultListLike {
  length: number;
  [index: number]: SpeechResultLike;
}

interface SpeechRecognitionEventLike {
  resultIndex: number;
  results: SpeechResultListLike;
}

interface SpeechRecognitionErrorLike {
  error: string;
  message?: string;
}

interface SpeechRecognitionLike {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  maxAlternatives: number;
  start(): void;
  stop(): void;
  abort(): void;
  onresult: ((event: SpeechRecognitionEventLike) => void) | null;
  onerror: ((event: SpeechRecognitionErrorLike) => void) | null;
  onend: (() => void) | null;
  onstart: (() => void) | null;
}

type SpeechRecognitionConstructor = new () => SpeechRecognitionLike;

function speechRecognitionConstructor(): SpeechRecognitionConstructor | null {
  if (typeof window === "undefined") return null;
  const scope = window as unknown as {
    SpeechRecognition?: SpeechRecognitionConstructor;
    webkitSpeechRecognition?: SpeechRecognitionConstructor;
  };
  return scope.SpeechRecognition ?? scope.webkitSpeechRecognition ?? null;
}

function readOverride(): boolean | null {
  try {
    const raw = window.localStorage.getItem(VOICE_INPUT_OVERRIDE_KEY);
    if (raw === "off") return false;
    if (raw === "on") return true;
    return null;
  } catch {
    return null;
  }
}

/** Turn a `SpeechRecognitionErrorEvent.error` code into something readable. */
function describeVoiceError(code: string): string | null {
  switch (code) {
    case "aborted":
      return null;
    case "not-allowed":
    case "service-not-allowed":
      return "Microphone access was blocked. Allow it in your browser to use voice input.";
    case "no-speech":
      return "Nothing was heard — try speaking again.";
    case "audio-capture":
      return "No microphone was found on this device.";
    case "network":
      return "Voice input needs a network connection — the transcript is recognised remotely.";
    default:
      return `Voice input failed (${code}).`;
  }
}

export interface VoiceInputResult {
  /** False when the flag is off or the browser has no recognition API. */
  supported: boolean;
  /** Why voice is unavailable, for the control's tooltip. */
  unsupportedReason: string | null;
  /** True between `start()` and the end of the recognition session. */
  listening: boolean;
  /** Finalised phrases for this session. */
  transcript: string;
  /** Live, still-changing phrase — treat as a preview only. */
  interim: string;
  error: string | null;
  start: () => void;
  stop: () => void;
  clear: () => void;
}

/**
 * Start, stop and read a dictation session.
 *
 * `start()` is a no-op when unsupported, and is ignored while already
 * listening, so a double click cannot raise an `InvalidStateError`.
 */
export function useVoiceInput(): VoiceInputResult {
  const override = readOverride();
  const enabled = override ?? VOICE_INPUT_ENABLED;
  const ctor = enabled ? speechRecognitionConstructor() : null;

  const [listening, setListening] = useState(false);
  const [transcript, setTranscript] = useState("");
  const [interim, setInterim] = useState("");
  const [error, setError] = useState<string | null>(null);
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);

  const stop = useCallback(() => {
    const recognition = recognitionRef.current;
    if (!recognition) return;
    // `onend` clears the listening flag; aborting here keeps a Safari
    // session from restarting itself after the user pressed stop.
    recognition.onend = null;
    recognition.abort();
    recognitionRef.current = null;
    setListening(false);
    setInterim("");
  }, []);

  const clear = useCallback(() => {
    setTranscript("");
    setInterim("");
    setError(null);
  }, []);

  const start = useCallback(() => {
    if (!ctor) {
      setError("Voice input is not supported by this browser.");
      return;
    }
    if (recognitionRef.current) return;

    const recognition = new ctor();
    recognition.lang = navigator.language || "en-US";
    recognition.continuous = false;
    recognition.interimResults = true;
    recognition.maxAlternatives = 1;

    recognition.onstart = () => {
      setListening(true);
      setError(null);
      setInterim("");
    };

    recognition.onresult = (event) => {
      let finalText = "";
      let interimText = "";
      for (let index = event.resultIndex; index < event.results.length; index += 1) {
        const result = event.results[index];
        const text = result[0]?.transcript ?? "";
        if (result.isFinal) finalText += text;
        else interimText += text;
      }
      if (finalText) {
        setTranscript((prev) => `${prev}${prev ? " " : ""}${finalText.trim()}`);
      }
      setInterim(interimText.trim());
    };

    recognition.onerror = (event) => {
      const message = describeVoiceError(event.error);
      if (message) setError(message);
      if (event.error === "not-allowed" || event.error === "service-not-allowed") {
        setListening(false);
      }
    };

    recognition.onend = () => {
      recognitionRef.current = null;
      setListening(false);
      setInterim("");
    };

    recognitionRef.current = recognition;
    try {
      recognition.start();
    } catch {
      // A start racing a stop (or a browser that needs the gesture) is not
      // fatal: drop the session and let the user press the button again.
      recognitionRef.current = null;
      setListening(false);
      setError("Voice input could not start. Try pressing the microphone again.");
    }
  }, [ctor]);

  useEffect(
    () => () => {
      const recognition = recognitionRef.current;
      if (!recognition) return;
      recognition.onend = null;
      recognition.abort();
      recognitionRef.current = null;
    },
    [],
  );

  const supported = Boolean(ctor);
  const unsupportedReason = supported
    ? null
    : enabled
      ? "This browser has no speech recognition API (Firefox on desktop does not)."
      : "Voice input is disabled for this browser by the local flag.";

  return {
    supported,
    unsupportedReason,
    listening,
    transcript,
    interim,
    error,
    start,
    stop,
    clear,
  };
}

export default useVoiceInput;
