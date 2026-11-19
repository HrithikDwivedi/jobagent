// JobAgent Companion - runs only on your own JobAgent application pages. When you click "Open application page",
// it tells the extension which application the new tab belongs to, so the card pops up there even after the job
// board redirects you to the employer's form.
(() => {
  const m = location.pathname.match(/^\/applications\/(\d+)/);
  if (!m) return;
  document.addEventListener("click", (e) => {
    const a = e.target && e.target.closest ? e.target.closest("a[data-apply-link]") : null;
    if (!a) return;
    const title = (document.querySelector("h1") || {}).textContent || "";
    const company = (document.querySelector(".page-head strong") || {}).textContent || "";
    try {
      chrome.runtime.sendMessage({ type: "will-open", appId: m[1], title: title.trim(), company: company.trim() });
    } catch (err) { /* extension reloaded; nothing to do */ }
  }, true);
})();
