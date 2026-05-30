/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "#0f1117",
        panel: "#161922",
        panel2: "#1d2230",
        line: "#262b39",
        accent: "#34d399",
        warn: "#fbbf24",
        crit: "#f43f5e",
        info: "#60a5fa",
      },
    },
  },
  plugins: [],
};
