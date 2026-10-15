/**
 * Shared button primitive.
 *
 * `variant` maps 1:1 onto the existing `btn-*` classes so migrated call sites
 * look identical, and the hand-rolled loading spinner is standardised here.
 * While `loading` the button stays focusable — no native `disabled` — so
 * screen-reader users can still reach it and hear `aria-busy="true"`;
 * activation is blocked in the click guard (`preventDefault`, and `onClick`
 * only fires when the button is neither loading nor disabled).
 *
 * @module components/ui/Button
 */

export type ButtonVariant = "accent" | "muted" | "danger" | "success";

export type ButtonSize = "sm" | "md" | "lg";

interface ButtonBaseProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  /** Visual treatment — maps 1:1 to the existing `btn-*` classes. */
  variant?: ButtonVariant;
  /** Size preset — text/padding utilities. Defaults to `md`. */
  size?: ButtonSize;
  /** Shows the spinner, sets `aria-busy="true"`, and blocks activation. */
  loading?: boolean;
}

interface IconOnlyButtonProps extends ButtonBaseProps {
  /** Renders only the children (an icon); `aria-label` is then required. */
  iconOnly: true;
  "aria-label": string;
}

interface StandardButtonProps extends ButtonBaseProps {
  iconOnly?: false;
  "aria-label"?: string;
}

export type ButtonProps = IconOnlyButtonProps | StandardButtonProps;

const VARIANT_CLASS: Record<ButtonVariant, string> = {
  accent: "btn-accent",
  muted: "btn-muted",
  danger: "btn-danger",
  success: "btn-success",
};

const SIZE_CLASS: Record<ButtonSize, string> = {
  sm: "text-xs px-3 py-1.5",
  md: "text-sm px-4 py-2",
  lg: "text-base px-5 py-3",
};

export default function Button({
  variant = "accent",
  size = "md",
  loading = false,
  disabled = false,
  iconOnly: _iconOnly,
  className = "",
  children,
  onClick,
  ...rest
}: ButtonProps) {
  const classes = [
    VARIANT_CLASS[variant],
    SIZE_CLASS[size],
    "inline-flex items-center justify-center gap-2",
    className,
  ]
    .filter(Boolean)
    .join(" ");

  function handleClick(event: React.MouseEvent<HTMLButtonElement>) {
    if (loading || disabled) {
      event.preventDefault();
      return;
    }
    onClick?.(event);
  }

  return (
    <button
      {...rest}
      className={classes}
      disabled={disabled}
      aria-busy={loading || undefined}
      onClick={handleClick}
    >
      {loading && (
        <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24" fill="none">
          <circle
            cx="12"
            cy="12"
            r="10"
            stroke="currentColor"
            strokeWidth="3"
            className="opacity-30"
          />
          <path d="M4 12a8 8 0 018-8" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
        </svg>
      )}
      {children}
    </button>
  );
}
