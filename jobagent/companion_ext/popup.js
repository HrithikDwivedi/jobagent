// JobAgent Companion popup: talks to JobAgent through the service worker and drives fill.js in the active tab
// (the same flows the on-page card uses, from flow.js).
const $ = (id) => document.getElementById(id);
const api = (path, method, body, binary) => chrome.runtime.sendMessage({ type: "api", path, method, body, binary });
let tab = null, me = null;

function esc(s) { const d = document.createElement("div"); d.textContent = String(s == null ? "" : s); return d.innerHTML; }
function report(html) { $("report").innerHTML = html; }
function busy(btn, on, label) {
  if (on) { btn.dataset.label = btn.textContent; btn.textContent = label; btn.disabled = true; }
  else { btn.textContent = btn.dataset.label || btn.textContent; btn.disabled = false; }
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
  const c = await chrome.storage.local.get("cards");
  $("cards").checked = c.cards !== false;
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
    const r = await JAFlow.fill(api, tab.id, appId(), tab.url, (t) => { btn.textContent = t; });
    if (r.error) { report('<p class="err">' + esc(r.error) + "</p>"); return; }
    let html = '<p class="ok">Filled ' + r.filled + " field" + (r.filled === 1 ? "" : "s") + ".</p>";
    if (r.kept) html += '<p class="muted small">Kept ' + r.kept + " answer" + (r.kept === 1 ? "" : "s") + " you had already typed.</p>";
    if (r.needs.length) {
      html += "<p>" + r.needs.length + " need you (highlighted amber):</p><ul>" +
        r.needs.slice(0, 30).map((n) => "<li>" + esc(n.question) + (n.reason ? "<small>" + esc(n.reason) + "</small>" : "") + "</li>").join("") + "</ul>" +
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
    const items = await JAFlow.collect(tab.id);
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
    const state = { allFrames: true };
    const target = await JAFlow.inject(tab.id, state);
    const res = await JAFlow.inFrames(target, () => globalThis.JACompanion.openQuestions());
    let n = 0, fail = "";
    for (const r of res) {
      for (const q of r.result.slice(0, 5)) {
        const d = await api("/api/companion/draft", "POST", { app_id: appId(), question: q.question });
        if (d && d.ok) { if (await JAFlow.inFrame(tab.id, r.frameId, (k, t) => globalThis.JACompanion.putDraft(k, t), [q.key, d.data.answer])) n++; }
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
    const r = await JAFlow.applied(api, tab.id, appId(), tab.url);
    report(r.ok ? '<p class="ok">Recorded as applied in JobAgent.</p>' : '<p class="err">' + esc(r.error) + "</p>");
  } finally { busy(btn, false); }
}

$("open-options").addEventListener("click", () => chrome.runtime.openOptionsPage());
$("fill").addEventListener("click", fill);
$("learn").addEventListener("click", learn);
$("learn-save").addEventListener("click", learnSave);
$("draft").addEventListener("click", draft);
$("applied").addEventListener("click", applied);
$("cards").addEventListener("change", (e) => chrome.storage.local.set({ cards: e.target.checked }));
init();
