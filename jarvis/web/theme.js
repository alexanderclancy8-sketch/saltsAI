/* JARVIS console - light / dark theme.
 *
 * Loaded in <head> on both the console and the sign-in page so the right theme is applied before first paint.
 * The choice is Auto (follow the device's prefers-color-scheme), Light or Dark, kept in localStorage under
 * "jarvis.theme". Auto is stored as the absence of a data-theme attribute so the CSS media query decides; Light and
 * Dark set data-theme on <html>. Every storage access is wrapped: private windows and blocked site data throw. */
(() => {
  "use strict";
  const KEY = "jarvis.theme";
  const root = document.documentElement;
  const dark = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function get() {
    try { const v = localStorage.getItem(KEY); return v === "light" || v === "dark" ? v : "auto"; } catch { return "auto"; }
  }
  // What is actually showing right now: "light" or "dark".
  function effective() {
    const c = get();
    return c === "auto" ? (dark && !dark.matches ? "light" : "dark") : c;
  }
  function apply() {
    const c = get();
    if (c === "auto") root.removeAttribute("data-theme"); else root.setAttribute("data-theme", c);
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", effective() === "light" ? "#e8eff9" : "#060d1a");
    window.dispatchEvent(new CustomEvent("jarvis-theme", { detail: effective() }));
  }
  function set(choice) {
    try { localStorage.setItem(KEY, choice === "light" || choice === "dark" ? choice : "auto"); } catch { /* private mode */ }
    apply();
  }
  window.JarvisTheme = { get, set, effective, apply };
  apply();
  if (dark && dark.addEventListener) dark.addEventListener("change", () => { if (get() === "auto") apply(); });
})();
