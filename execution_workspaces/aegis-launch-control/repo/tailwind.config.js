/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        'mission': {
          'dark': '#0a0e1a',
          'darker': '#050810',
          'blue': '#1e3a8a',
          'cyan': '#06b6d4',
          'amber': '#f59e0b',
          'green': '#10b981',
          'red': '#ef4444'
        }
      }
    },
  },
  plugins: [],
}
