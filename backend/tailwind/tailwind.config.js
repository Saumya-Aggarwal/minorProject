// Design tokens for the storefront. Build the stylesheet from backend/ with:
//   npx -y tailwindcss@3.4.17 -c tailwind/tailwind.config.js -i tailwind/input.css -o static/css/site.css --minify
// The output is committed, so the running app needs neither Node nor a CDN.
module.exports = {
  content: ["./templates/**/*.html", "./static/live.js", "./static/admin_training.js"],
  theme: {
    extend: {
      colors: {
        ivory: "#FAF7F2",
        sand: "#F1EBE2",       // image backdrops, subtle panels
        line: "#E6DDD1",       // hairline borders
        ink: "#1F1A17",
        muted: "#6B625B",
        maroon: {
          DEFAULT: "#7A1F2B",
          dark: "#5E1520",
          tint: "#F5EAEA",
        },
        gold: "#B08D57",
      },
      fontFamily: {
        serif: ['"Cormorant Garamond"', "Georgia", "Times New Roman", "serif"],
        sans: ["Inter", "system-ui", "-apple-system", "Segoe UI", "Roboto", "sans-serif"],
      },
      maxWidth: { site: "80rem" },
      letterSpacing: { label: "0.14em", eyebrow: "0.2em" },
    },
  },
  plugins: [],
};
