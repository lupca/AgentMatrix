/** @type {import('tailwindcss').Config} */
export default {
  darkMode: ['selector', '[data-theme="dark"]'],
  content: [
    './index.html',
    './src/**/*.{js,ts,jsx,tsx}',
  ],
  theme: {
    extend: {
      colors: {
        white: 'rgb(var(--color-white) / <alpha-value>)',
        black: 'rgb(var(--color-black) / <alpha-value>)',
        gray: Object.fromEntries([100, 200, 300, 400, 500, 600, 700, 800, 850, 900, 950].map((shade) => [shade, `rgb(var(--color-gray-${shade}) / <alpha-value>)`])),
        slate: Object.fromEntries([400, 500].map((shade) => [shade, `rgb(var(--color-slate-${shade}) / <alpha-value>)`])),
        indigo: Object.fromEntries([200, 300, 400, 500, 600, 700, 800, 950].map((shade) => [shade, `rgb(var(--color-indigo-${shade}) / <alpha-value>)`])),
        purple: Object.fromEntries([200, 300, 400, 500, 600, 800, 950].map((shade) => [shade, `rgb(var(--color-purple-${shade}) / <alpha-value>)`])),
        blue: Object.fromEntries([200, 300, 400, 500, 600, 800, 950].map((shade) => [shade, `rgb(var(--color-blue-${shade}) / <alpha-value>)`])),
        cyan: Object.fromEntries([400, 500].map((shade) => [shade, `rgb(var(--color-cyan-${shade}) / <alpha-value>)`])),
        violet: Object.fromEntries([400, 500].map((shade) => [shade, `rgb(var(--color-violet-${shade}) / <alpha-value>)`])),
        pink: { 500: 'rgb(var(--color-pink-500) / <alpha-value>)' },
        teal: Object.fromEntries([500, 600].map((shade) => [shade, `rgb(var(--color-teal-${shade}) / <alpha-value>)`])),
        emerald: Object.fromEntries([300, 400, 500, 600].map((shade) => [shade, `rgb(var(--color-emerald-${shade}) / <alpha-value>)`])),
        amber: Object.fromEntries([200, 300, 400, 500, 800, 950].map((shade) => [shade, `rgb(var(--color-amber-${shade}) / <alpha-value>)`])),
        red: Object.fromEntries([200, 300, 400, 500, 600, 800, 950].map((shade) => [shade, `rgb(var(--color-red-${shade}) / <alpha-value>)`])),
        rose: Object.fromEntries([400, 500].map((shade) => [shade, `rgb(var(--color-rose-${shade}) / <alpha-value>)`])),
        background: 'rgb(var(--background) / <alpha-value>)',
        foreground: 'rgb(var(--foreground) / <alpha-value>)',
      },
    },
  },
  plugins: [],
};
