/** @type {import('tailwindcss').Config} */
export default {
  content: ['./src/**/*.{astro,html,js,jsx,md,mdx,svelte,ts,tsx,vue}'],
  theme: {
    extend: {
      colors: {
        base: '#111111',
        'base-light': '#1A1A1A',
        primary: '#14B8A6',
        accent: '#2DD4BF',
        // --- UPDATED FOR READABILITY ---
        'text-main': '#F9FAFB',  // Bright Cool Gray (almost white)
        'text-muted': '#D1D5DB', // Light Cool Gray
        // ---------------------------------
      },
      fontFamily: {
        sans: ['"Inter"', 'sans-serif'],
        mono: ['"Fira Code"', 'monospace'],
        serif: ['"Cormorant Garamond"', 'serif'],
      },
    },
  },
  plugins: [],
}