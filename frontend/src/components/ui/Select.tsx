/**
 * Select with the same labelling contract as `Input` — `<label htmlFor>`
 * association, `aria-invalid` on error, and `aria-describedby` pointing at the
 * error and/or hint text rendered below the control. The caller's `id` always
 * wins; an id is generated only when a `label` is provided without one.
 *
 * @module components/ui/Select
 */

import { useId } from "react";

export interface SelectProps extends React.SelectHTMLAttributes<HTMLSelectElement> {
  label?: string;
  error?: string;
  hint?: string;
  children: React.ReactNode;
}

export default function Select({
  label,
  error,
  hint,
  id,
  className = "",
  children,
  ...rest
}: SelectProps) {
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
      <select
        {...rest}
        id={controlId}
        className={`input-theme ${className}`}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy}
      >
        {children}
      </select>
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
