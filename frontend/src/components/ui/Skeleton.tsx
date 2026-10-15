/**
 * Loading placeholder built on `animate-pulse` + `bg-[var(--bg-soft)]`.
 *
 * `aria-hidden` because the skeleton conveys no information — surrounding live
 * regions announce the actual loading state where needed.
 *
 * @module components/ui/Skeleton
 */

export type SkeletonVariant = "text" | "card" | "table" | "circle";

export interface SkeletonProps {
  variant?: SkeletonVariant;
  /** Number of placeholder lines for the `text` variant. */
  lines?: number;
  className?: string;
}

function Shape({ variant, className = "" }: { variant: SkeletonVariant; className?: string }) {
  switch (variant) {
    case "circle":
      return <div className={`animate-pulse rounded-full bg-[var(--bg-soft)] ${className}`} />;
    case "card":
      return (
        <div className={`animate-pulse rounded-lg bg-[var(--bg-soft)] min-h-[8rem] ${className}`} />
      );
    case "table":
      return <div className={`animate-pulse rounded bg-[var(--bg-soft)] h-10 ${className}`} />;
    case "text":
    default:
      return <div className={`animate-pulse rounded bg-[var(--bg-soft)] h-4 ${className}`} />;
  }
}

export default function Skeleton({ variant = "text", lines = 3, className = "" }: SkeletonProps) {
  if (variant === "text") {
    return (
      <div className={`space-y-2 ${className}`} aria-hidden="true">
        {Array.from({ length: lines }).map((_, i) => (
          <Shape key={i} variant="text" className={i === lines - 1 ? "w-2/3" : "w-full"} />
        ))}
      </div>
    );
  }

  return (
    <div className={className} aria-hidden="true">
      <Shape variant={variant} className="w-full" />
    </div>
  );
}
