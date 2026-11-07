/**
 * Self-hosted IBM Plex Sans and IBM Plex Sans Arabic (OFL-1.1, DS-02): weights 400 / 500 / 600,
 * subsets latin, latin-ext (Turkish) and arabic, font-display: swap (fontsource's default).
 * The browser downloads a file only when the page uses a character of its unicode-range;
 * every file is served from the app's own origin (no font CDN - the CSP forbids one).
 */
import "@fontsource/ibm-plex-sans/latin-400.css";
import "@fontsource/ibm-plex-sans/latin-500.css";
import "@fontsource/ibm-plex-sans/latin-600.css";
import "@fontsource/ibm-plex-sans/latin-ext-400.css";
import "@fontsource/ibm-plex-sans/latin-ext-500.css";
import "@fontsource/ibm-plex-sans/latin-ext-600.css";
import "@fontsource/ibm-plex-sans-arabic/arabic-400.css";
import "@fontsource/ibm-plex-sans-arabic/arabic-500.css";
import "@fontsource/ibm-plex-sans-arabic/arabic-600.css";

export const FONT_FILES = {
  families: ["IBM Plex Sans", "IBM Plex Sans Arabic"],
  weights: [400, 500, 600],
  subsets: ["latin", "latin-ext", "arabic"],
  licence: "OFL-1.1",
} as const;
