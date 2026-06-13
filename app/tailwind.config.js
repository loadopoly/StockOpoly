/** @type {import('tailwindcss').Config} */
// Mirrors the Loadopoly-OCR design system: dark-slate surfaces, blue primary.
export default {
  content: ['./index.html', './src/**/*.{js,ts,tsx,jsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        slate: { 850: '#1e293b', 900: '#0f172a', 950: '#020617' },
        primary: {
          50: '#eff6ff', 100: '#dbeafe', 200: '#bfdbfe', 300: '#93c5fd',
          400: '#60a5fa', 500: '#3b82f6', 600: '#2563eb', 700: '#1d4ed8',
          800: '#1e40af', 900: '#1e3a8f', 950: '#172554',
        },
        success: { 50: '#ecfdf5', 500: '#10b981', 600: '#059669' },
        warning: { 50: '#fffbeb', 500: '#f59e0b', 600: '#d97706' },
        error: { 50: '#fef2f2', 500: '#ef4444', 600: '#dc2626' },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'ui-monospace', 'monospace'],
      },
      fontSize: { '2xs': ['0.625rem', { lineHeight: '0.875rem' }] },
    },
  },
  plugins: [],
};
