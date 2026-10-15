/**
 * Single source of truth for stacking order.
 *
 * Replaces the hardcoded `z-40`/`z-50`/`z-[N]` values scattered across the app so
 * layers can be reasoned about in one place. Values are mirrored into
 * `tailwind.config.js` as `z-<name>` utilities.
 *
 * @module lib/zIndex
 */

export const zIndex = {
  nav: 40,
  navMenu: 60,
  modal: 50,
  modalNested: 80,
  tutorial: 95,
  login: 100,
  drawer: 120,
  toast: 200,
} as const;

export type ZLayer = keyof typeof zIndex;

/** Returns the Tailwind utility class for a named layer, e.g. `z-toast`. */
export function resolveZ(level: ZLayer): string {
  return `z-${level}`;
}
