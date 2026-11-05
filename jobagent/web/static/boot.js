/* JobAgent boot: runs synchronously in <head> (tiny, CSP-safe) so the first paint already knows
   that JavaScript is available and which colour theme the person picked. Everything else is in app.js. */
(function () {
  "use strict";
  var root = document.documentElement;
  root.classList.add("js");
  // Light theme is the default for everyone; the toggle stores an explicit choice.
  var t = "light";
  try {
    var saved = window.localStorage.getItem("ja-theme");
    if (saved === "light" || saved === "dark") t = saved;
  } catch (e) { /* storage unavailable: keep the light default */ }
  root.setAttribute("data-theme", t);
})();
