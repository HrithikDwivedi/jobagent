// JobAgent Companion popup: talks to JobAgent through the service worker and drives fill.js in the active tab.
const $ = (id) => document.getElementById(id);
const api = (path, method, body, binary) => chrome.runtime.sendMessage({ type: "api", path, method, body, binary });
let tab = null, me = null, lastFrames = [];

function esc(s) { const d = document.createElement("div"); d.textContent = String(s == null ? "" : s); return d.innerHTML; }
function report(html) { $("report").innerHTML = html; }
function busy(btn, on, label) {
  if (on) { btn.dataset.label = btn.textContent; btn.textContent = label; btn.disabled = true; }
  else { btn.textContent = btn.dataset.label || btn.textContent; btn.disabled = false; }
}

// Forms are often embedded in iframes (e.g. Greenhouse on a company site), so every frame is used; if a frame
// refuses injection (sandboxed ads, payment widgets) fall back to the main frame instead of failing.
let allFrames = true;
const target = () => (allFrames ? { tabId: tab.id, allFrames: true } : { tabId: tab.id, frameIds: [0] });
async function inject() {
  try {
    await chrome.scripting.executeScript({ target: target(), files: ["extract.js", "fill.js"] });
  } catch (e) {
    if (!allFrames) throw e;
    allFrames = false;
    await chrome.scripting.executeScript({ target: target(), files: ["extract.js", "fill.js"] });
  }
}
async function inFrames(func, args) {
  const res = await chrome.scripting.executeScript({ target: target(), func, args: args || [] });
  return res.filter((r) => r && r.result != null);
}
async function inFrame(frameId, func, args) {
  const res = await chrome.scripting.executeScript({ target: { tabId: tab.id, frameIds: [frameId] }, func, args: args || [] });
  return res[0] && res[0].result;
}
function appId() { return $("app").value ? parseInt($("app").value, 10) : null; }

async function init() {
  // popup.html?tab=<id> targets a specific tab (opening the Companion in its own window, and automated tests)
  const forced = parseInt(new URLSearchParams(location.search).get("tab") || "", 10);
  if (forced) tab = await chrome.tabs.get(forced);
  else [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const r = await api("/api/companion/me");
  if (!r || !r.ok) {
    $("who").textContent = (r && r.error) || "Not connected";
    $("setup").hidden = false;
    return;
  }
  me = r.data;
  $("who").textContent = "Signed in as " + (me.user.full_name || me.user.username);
  $("main").hidden = false;
  $("draft").hidden = !me.ai.allowed;
  if (me.profile.missing.length)
    $("match-note").textContent = "Add to Settings for better filling: " + me.profile.missing.join(", ").replace(/_/g, " ") + ".";
  const apps = await api("/api/companion/applications?url=" + encodeURIComponent(tab.url || ""));
  if (apps && apps.ok) {
    for (const a of apps.data.applications) {
      const o = document.createElement("option");
      o.value = a.id;
      o.textContent = a.title + " · " + a.company;
      $("app").appendChild(o);
    }
    if (apps.data.match) {
      $("app").value = String(apps.data.match.id);
      $("match-note").textContent = "Matched to your prepared application: tailored resume, cover letter and reviewed answers will be used.";
    }
  }
}

async function fill() {
  const btn = $("fill");
  busy(btn, true, "Reading the form…");
  try {
    await inject();
    const scans = await inFrames(() => globalThis.JACompanion.scan());
    const fields = [];
    lastFrames = scans.map((s) => ({ frameId: s.frameId, fields: s.result.fields }));
    for (const s of lastFrames) for (const f of s.fields) fields.push(Object.assign({}, f, { key: s.frameId + ":" + f.key }));
    if (!fields.length) { report('<p class="err">No form fields found on this page. Open the application form itself (after "Apply"), then try again.</p>'); return; }
    btn.textContent = "Matching your answers…";
    const plan = await api("/api/companion/plan", "POST", { app_id: appId(), url: tab.url, fields });
    if (!plan || !plan.ok) { report('<p class="err">' + esc(plan && plan.error) + "</p>"); return; }
    let files = {};
    if (plan.data.fields.some((d) => d.action === "upload_resume")) {
      btn.textContent = "Getting your resume…";
      const pdf = await api("/api/companion/resume" + (appId() ? "?app_id=" + appId() : ""), "GET", null, true);
      if (pdf && pdf.ok) files.resume = { data: pdf.data, name: pdf.name, type: pdf.type };
    }
    if (plan.data.cover_letter) {
      files.cover = { data: btoa(unescape(encodeURIComponent(plan.data.cover_letter))), name: "Cover_Letter.txt", type: "text/plain" };
    }
    btn.textContent = "Filling…";
    let filled = 0, kept = 0; const needs = [];
    for (const fr of lastFrames) {
      const decisions = plan.data.fields.filter((d) => d.key.startsWith(fr.frameId + ":"))
        .map((d) => Object.assign({}, d, { key: d.key.slice(String(fr.frameId).length + 1) }));
      if (!decisions.length) continue;
      const r = await inFrame(fr.frameId, (f, d, x) => globalThis.JACompanion.apply(f, d, x), [fr.fields, decisions, files]);
      if (r) { filled += r.filled; kept += r.kept || 0; needs.push(...r.needs); }
    }
    let html = '<p class="ok">Filled ' + filled + " field" + (filled === 1 ? "" : "s") + ".</p>";
    if (kept) html += '<p class="muted small">Kept ' + kept + " answer" + (kept === 1 ? "" : "s") + " you had already typed.</p>";
    if (needs.length) {
      html += "<p>" + needs.length + " need you (highlighted amber):</p><ul>" +
        needs.slice(0, 30).map((n) => "<li>" + esc(n.question) + (n.reason ? "<small>" + esc(n.reason) + "</small>" : "") + "</li>").join("") + "</ul>" +
        '<p class="muted small">Answer them on the page, then press "Save my answers" so next time they fill automatically.</p>';
    } else {
      html += "<p>Everything is filled. Review the form and press Submit on the page.</p>";
    }
    report(html);
  } catch (e) {
    report('<p class="err">This page cannot be filled (' + esc(e.message || e) + "). Browser pages and the Chrome Web Store are off-limits to extensions.</p>");
  } finally { busy(btn, false); }
}

async function learn() {
  const btn = $("learn");
  busy(btn, true, "Reading your answers…");
  try {
    await inject();
    const res = await inFrames(() => globalThis.JACompanion.collect());
    const items = res.flatMap((r) => r.result);
    const list = $("learn-list");
    list.innerHTML = "";
    if (!items.length) { report("<p>No new answers found on this page.</p>"); $("learn-box").hidden = true; return; }
    items.slice(0, 60).forEach((it, i) => {
      const li = document.createElement("li");
      li.innerHTML = '<input type="checkbox" id="l' + i + '" checked><span><label for="l' + i + '">' + esc(it.question) + "</label><em>" + esc(it.answer.slice(0, 160)) + "</em></span>";
      li.dataset.q = it.question; li.dataset.a = it.answer;
      list.appendChild(li);
    });
    $("learn-box").hidden = false;
    report("");
  } catch (e) {
    report('<p class="err">' + esc(e.message || e) + "</p>");
  } finally { busy(btn, false); }
}

async function learnSave() {
  const items = Array.from($("learn-list").querySelectorAll("li")).filter((li) => li.querySelector("input").checked)
    .map((li) => ({ question: li.dataset.q, answer: li.dataset.a }));
  if (!items.length) return;
  const r = await api("/api/companion/learn", "POST", { items });
  $("learn-box").hidden = true;
  if (r && r.ok) report('<p class="ok">Saved ' + r.data.saved + " answer(s) to your library" +
    (r.data.skipped ? " (" + r.data.skipped + " personal/sensitive skipped)" : "") +
    (r.data.requeued ? "; " + r.data.requeued + " held-back application(s) re-queued for auto-apply" : "") + ".</p>");
  else report('<p class="err">' + esc(r && r.error) + "</p>");
}

async function draft() {
  const btn = $("draft");
  busy(btn, true, "Drafting…");
  try {
    await inject();
    const res = await inFrames(() => globalThis.JACompanion.openQuestions());
    let n = 0, fail = "";
    for (const r of res) {
      for (const q of r.result.slice(0, 5)) {
        const d = await api("/api/companion/draft", "POST", { app_id: appId(), question: q.question });
        if (d && d.ok) { if (await inFrame(r.frameId, (k, t) => globalThis.JACompanion.putDraft(k, t), [q.key, d.data.answer])) n++; }
        else fail = (d && d.error) || "failed";
      }
    }
    report(n ? '<p class="ok">Drafted ' + n + " answer(s) in blue. Read and edit each before submitting.</p>"
      : "<p>" + (fail ? esc(fail) : "No empty open questions found.") + "</p>");
  } catch (e) {
    report('<p class="err">' + esc(e.message || e) + "</p>");
  } finally { busy(btn, false); }
}

async function applied() {
  if (!appId()) { report("<p>Pick the JobAgent application this page belongs to first (open the job in JobAgent and press Apply to create it).</p>"); return; }
  const btn = $("applied");
  busy(btn, true, "Recording…");
  try {
    let conf = "";
    try { await inject(); conf = (await inFrames(() => globalThis.JACompanion.confirmation())).map((r) => r.result).filter(Boolean)[0] || ""; } catch (e) { conf = ""; }
    const r = await api("/api/companion/applied", "POST", { app_id: appId(), url: tab.url, confirmation_text: conf });
    report(r && r.ok ? '<p class="ok">Recorded as submitted in JobAgent.</p>' : '<p class="err">' + esc(r && r.error) + "</p>");
  } finally { busy(btn, false); }
}

$("open-options").addEventListener("click", () => chrome.runtime.openOptionsPage());
$("fill").addEventListener("click", fill);
$("learn").addEventListener("click", learn);
$("learn-save").addEventListener("click", learnSave);
$("draft").addEventListener("click", draft);
$("applied").addEventListener("click", applied);
init();
