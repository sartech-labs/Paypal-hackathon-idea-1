/* Shared by the Website and About tabs: the theme switch in the demo bar. */
(() => {
  "use strict";
  const THEMES = ["dark", "light", "system"];
  const get = () => { try { return localStorage.getItem("ct-theme"); } catch (_) { return null; } };
  const set = (v) => { try { v === "dark" ? localStorage.removeItem("ct-theme") : localStorage.setItem("ct-theme", v); } catch (_) { /* storage unavailable */ } };
  function init() {
    const button = document.getElementById("theme-toggle");
    if (!button) return;
    const apply = (pref) => { document.documentElement.dataset.theme = pref; button.textContent = `Theme: ${pref}`; };
    let pref = get() || "dark";
    if (!THEMES.includes(pref)) pref = "dark";
    apply(pref);
    button.addEventListener("click", () => {
      const next = THEMES[(THEMES.indexOf(get() || "dark") + 1) % THEMES.length];
      set(next);
      apply(next);
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
