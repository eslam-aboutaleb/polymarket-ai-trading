/**
 * Text input with the shared labelling contract — `<label htmlFor>` association,
 * `aria-invalid` on error, and `aria-describedby` pointing at the error and/or
 * hint text rendered below the control. The caller's `id` always wins; an id is
 * generated only when a `label` is provided without one.
 *
 * @module components/ui/Input
 */

import { useId } from "react";

export interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  label?: string;
  error?: string;
  hint?: string;
}

export default function Input({ label, error, hint, id, className = "", ...rest }: InputProps) {
  const generatedId = useId();
  const baseId = id ?? generatedId;
  const controlId = label ? baseId : id;
  const errorId = error ? `${baseId}-error` : undefined;
  const hintId = hint ? `${baseId}-hint` : undefined;
  const describedBy = [errorId, hintId].filter(Boolean).join(",") || undefined;

  return (
    <div>
      {label && (
        <label htmlFor={controlId} className="block text-sm font-medium mb-1">
          {label}
        </label>
      )}
      <input
        {...rest}
        id={controlId}
        className={`input-theme ${className}`}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy}
      />
      {error && (
        <p id={errorId} className="text-xs text-red-400 mt-1">
          {error}
        </p>
      )}
      {hint && (
        <p id={hintId} className="text-xs text-muted mt-1">
          {hint}
        </p>
      )}
    </div>
  );
}
