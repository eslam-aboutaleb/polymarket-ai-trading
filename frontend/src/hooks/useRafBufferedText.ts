import { useCallback, useEffect, useRef, useState } from "react";

type Options = {
  initialText?: string;
};

type UseRafBufferedTextResult = {
  text: string;
  append: (chunk: string) => void;
  reset: (nextText?: string) => void;
  flushNow: () => void;
};

/**
 * Buffers frequent text chunks and applies them at most once per animation frame.
 * This reduces render pressure for streaming UIs.
 */
export function useRafBufferedText(
  options: Options = {},
): UseRafBufferedTextResult {
  const { initialText = "" } = options;
  const [text, setText] = useState(initialText);
  const pendingRef = useRef("");
  const rafIdRef = useRef<number | null>(null);

  const flush = useCallback(() => {
    rafIdRef.current = null;
    if (!pendingRef.current) return;
    const pending = pendingRef.current;
    pendingRef.current = "";
    setText((prev) => prev + pending);
  }, []);

  const scheduleFlush = useCallback(() => {
    if (rafIdRef.current != null) return;
    rafIdRef.current = window.requestAnimationFrame(flush);
  }, [flush]);

  const append = useCallback(
    (chunk: string) => {
      if (!chunk) return;
      pendingRef.current += chunk;
      scheduleFlush();
    },
    [scheduleFlush],
  );

  const flushNow = useCallback(() => {
    if (rafIdRef.current != null) {
      window.cancelAnimationFrame(rafIdRef.current);
      rafIdRef.current = null;
    }
    flush();
  }, [flush]);

  const reset = useCallback((nextText = "") => {
    if (rafIdRef.current != null) {
      window.cancelAnimationFrame(rafIdRef.current);
      rafIdRef.current = null;
    }
    pendingRef.current = "";
    setText(nextText);
  }, []);

  useEffect(
    () => () => {
      if (rafIdRef.current != null) {
        window.cancelAnimationFrame(rafIdRef.current);
      }
    },
    [],
  );

  return { text, append, reset, flushNow };
}
