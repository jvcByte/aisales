// No "use client" here, and that is the whole point of this file.
//
// `THEME_SCRIPT` is consumed by the root layout, which is a server
// component. Importing it from `theme-toggle.tsx` meant a server component
// reading a value out of a module marked "use client", where every export
// is a client reference -- and the page hydrates against a tree React
// considers different. It fails silently as a hydration mismatch, which is
// why the two live apart.

export const THEME_KEY = "aisales-theme";

/** Interpolated, not written twice: the script and the component have to
 * agree on the storage key or a stored choice is read by one and not the
 * other, which looks exactly like a broken toggle. */
const KEY = THEME_KEY;

export const THEME_SCRIPT = `
(function () {
  var KEY = ${JSON.stringify(KEY)};
  function resolve() {
    var stored = null;
    try { stored = localStorage.getItem(KEY); } catch (e) {}
    if (stored === "light" || stored === "dark") return stored;
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  function apply(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    document.documentElement.style.colorScheme = theme;
  }
  apply(resolve());
  // Follow the OS while the choice is "system", and stop the moment it is not.
  try {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
      var stored = null;
      try { stored = localStorage.getItem(KEY); } catch (e) {}
      if (stored !== "light" && stored !== "dark") apply(resolve());
    });
  } catch (e) {}
})();
`;
