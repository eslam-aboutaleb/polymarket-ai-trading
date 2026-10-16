/**
 * Dismissable error message for a react-query result.
 *
 * The error banners pair a message with a dismiss (✕) button, but react-query keeps
 * `error` until the next successful fetch. This maps a query failure to the existing
 * `getApiErrorMessage` copy and hides it again once dismissed, until the *next*
 * failure (tracked by `errorUpdatedAt`) surfaces a fresh one.
 *
 * @module hooks/useDismissableError
 */

import { useCallback, useState } from "react";
import { getApiErrorMessage } from "../utils/apiError";

export interface DismissableError {
  /** Message to render, or null when there is no (undismissed) failure. */
  message: string | null;
  /** Hides the current message until the next failure. */
  dismiss: () => void;
}

export function useDismissableError(
  error: unknown,
  isError: boolean,
  errorUpdatedAt: number,
  fallbackMessage: string,
): DismissableError {
  const [dismissedAt, setDismissedAt] = useState(0);

  const message =
    isError && errorUpdatedAt !== dismissedAt ? getApiErrorMessage(error, fallbackMessage) : null;

  const dismiss = useCallback(() => setDismissedAt(errorUpdatedAt), [errorUpdatedAt]);

  return { message, dismiss };
}
