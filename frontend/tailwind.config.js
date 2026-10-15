/** @type {import('tailwindcss').Config} */

// Named stacking layers mirrored in `src/lib/zIndex.ts` — the single source of
// truth. `resolveZ()` builds class names at runtime (`z-${level}`), so they are
// safelisted here to guarantee the utilities exist even though the literal
// strings never appear in markup.
const Z_LAYERS = {
  nav: 40,
  navMenu: 60,
  modal: 50,
  modalNested: 80,
  tutorial: 95,
  login: 100,
  drawer: 120,
  toast: 200,
};

export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  safelist: Object.keys(Z_LAYERS).map((layer) => `z-${layer}`),
  theme: {
    extend: {
      colors: {
        primary: {
          50: "#f0f9ff",
          100: "#e0f2fe",
          200: "#bae6fd",
          300: "#7dd3fc",
          400: "#38bdf8",
          500: "#0ea5e9",
          600: "#0284c7",
          700: "#0369a1",
          800: "#075985",
          900: "#0c3d66",
        },
      },
      zIndex: Z_LAYERS,
    },
  },
  plugins: [],
};
