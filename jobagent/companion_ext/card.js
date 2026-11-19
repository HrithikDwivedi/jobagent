// JobAgent Companion - the small card that pops up on an application page. Lives in a closed shadow root so the
// employer's styles cannot break it (and it cannot break their page). Buttons only send a message to the
// extension's service worker; all JobAgent calls happen there, never from the page.
(() => {
  if (globalThis.JACard) return;
  let host = null, root = null;

  const CSS = `
    :host { all: initial; }
    .card { position: fixed; right: 18px; bottom: 18px; z-index: 2147483647; width: 320px; max-width: calc(100vw - 36px);
      font: 13px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; color: #0f172a; background: #fff;
      border: 1px solid #dbe3ee; border-top: 3px solid #0a5bb5; border-radius: 12px;
      box-shadow: 0 18px 50px rgba(15, 35, 65, .25); padding: 14px 14px 12px; animation: up .28s ease-out; }
    @keyframes up { from { transform: translateY(12px); opacity: 0; } to { transform: none; opacity: 1; } }
    @media (prefers-reduced-motion: reduce) { .card { animation: none; } }
    @media (prefers-color-scheme: dark) { .card { background: #111a2b; color: #e2e8f0; border-color: #24324a; } .muted { color: #94a3b8 !important; } }
    .head { display: flex; gap: 10px; align-items: flex-start; }
    .mark { flex: 0 0 auto; width: 28px; height: 28px; border-radius: 8px; background: #0f2341; color: #fff;
      display: grid; place-items: center; font-weight: 700; }
    .title { font-weight: 650; }
    .muted { color: #64748b; }
    .x { margin-left: auto; border: 0; background: none; color: inherit; font-size: 18px; line-height: 1; cursor: pointer;
      opacity: .6; padding: 0 2px; }
    .x:hover { opacity: 1; }
    .row { display: flex; gap: 8px; margin-top: 10px; }
    button.b { flex: 1; padding: 8px 10px; border-radius: 8px; border: 1px solid #cbd5e1; background: transparent;
      color: inherit; font: 600 13px system-ui, sans-serif; cursor: pointer; }
    button.p { background: #0a5bb5; border-color: #0a5bb5; color: #fff; }
    button.g { background: #15803d; border-color: #15803d; color: #fff; }
    button:disabled { opacity: .6; cursor: progress; }
    .msg { margin-top: 8px; }
    .ok { color: #15803d; font-weight: 600; }
    .err { color: #b91c1c; }
    ul { margin: 6px 0 0; padding-left: 16px; max-height: 120px; overflow: auto; }
  `;

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function send(action) {
    return new Promise((resolve) => {
      try { chrome.runtime.sendMessage({ type: "card", action }, (r) => resolve(r || { error: "No answer from the extension." })); }
      catch (e) { resolve({ error: "The extension was updated; reload this page." }); }
    });
  }

  function close() {
    if (host) host.remove();
    host = root = null;
  }

  function show(info) {
    close();
    host = document.createElement("div");
    host.setAttribute("data-jobagent-card", "");
    root = host.attachShadow({ mode: "closed" });
    const style = el("style"); style.textContent = CSS; root.appendChild(style);
    const card = el("div", "card"); card.setAttribute("role", "dialog"); card.setAttribute("aria-label", "JobAgent Companion");
    const head = el("div", "head");
    head.appendChild(el("div", "mark", "J"));
    const t = el("div");
    t.appendChild(el("div", "title", info.job ? "Applying to " + info.job : "Application form detected"));
    t.appendChild(el("div", "muted", info.company ? info.company + " · JobAgent can fill this form for you" : "JobAgent can fill this form from your profile"));
    head.appendChild(t);
    const x = el("button", "x", "×"); x.setAttribute("aria-label", "Close"); x.title = "Not now";
    x.addEventListener("click", () => { send("dismiss"); close(); });
    head.appendChild(x);
    card.appendChild(head);
    const row = el("div", "row");
    const fillBtn = el("button", "b p", "Fill this form");
    const doneBtn = el("button", "b g", "I've applied");
    row.appendChild(fillBtn);
    if (info.appId) row.appendChild(doneBtn);
    card.appendChild(row);
    const msg = el("div", "msg muted", "You review everything and press Submit yourself.");
    card.appendChild(msg);

    fillBtn.addEventListener("click", async () => {
      fillBtn.disabled = true; fillBtn.textContent = "Filling…";
      const r = await send("fill");
      fillBtn.disabled = false; fillBtn.textContent = "Fill again";
      msg.textContent = ""; msg.className = "msg";
      if (r.error) { msg.className = "msg err"; msg.textContent = r.error; return; }
      msg.appendChild(el("div", "ok", "Filled " + r.filled + " field" + (r.filled === 1 ? "" : "s") + (r.kept ? " · kept " + r.kept + " of yours" : "")));
      if (r.needs && r.needs.length) {
        msg.appendChild(el("div", "muted", r.needs.length + " need you (outlined amber):"));
        const ul = el("ul");
        r.needs.slice(0, 6).forEach((n) => ul.appendChild(el("li", "", n.question)));
        msg.appendChild(ul);
      } else {
        msg.appendChild(el("div", "muted", "Everything is filled. Review it, then press Submit on the page."));
      }
    });
    doneBtn.addEventListener("click", async () => {
      doneBtn.disabled = true; doneBtn.textContent = "Saving…";
      const r = await send("applied");
      msg.className = r.error ? "msg err" : "msg ok";
      msg.textContent = r.error || "Recorded as applied in JobAgent. Good luck!";
      doneBtn.textContent = r.error ? "I've applied" : "Applied ✓";
      doneBtn.disabled = !r.error;
    });
    root.appendChild(card);
    (document.body || document.documentElement).appendChild(host);
  }

  globalThis.JACard = { show, close };
})();
