// JobAgent Companion - service worker. The ONLY place that talks to the JobAgent server: the access token never
// reaches web pages. It also notices when you open a job you are applying to and pops up a small card on it.
try { importScripts("defaults.js"); } catch (e) { /* defaults.js is added when the zip is downloaded from JobAgent */ }
importScripts("flow.js");

// application sites where a form is worth a card even without a JobAgent application behind it
const ATS = /(^|\.)(greenhouse\.io|lever\.co|ashbyhq\.com|myworkdayjobs\.com|myworkdaysite\.com|icims\.com|smartrecruiters\.com|taleo\.net|successfactors\.(com|eu)|jobvite\.com|bamboohr\.com|workable\.com|recruitee\.com|teamtailor\.com|personio\.(de|com)|breezy\.hr|applytojob\.com|oraclecloud\.com|eightfold\.ai|avature\.net|jazzhr\.com|pinpointhq\.com|rippling-ats\.com|dover\.com)$/i;
const TAB_TTL = 45 * 60 * 1000;       // a tab stays linked to its application this long (redirects, multi-page forms)
const APPS_TTL = 10 * 60 * 1000;

async function config() {
  const c = await chrome.storage.local.get(["server", "token", "cards"]);
  return { server: (c.server || self.JA_DEFAULT_SERVER || "").replace(/\/+$/, ""), token: c.token || "",
           cards: c.cards !== false };
}

function toBase64(buf) {
  const bytes = new Uint8Array(buf);
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}

async function api(path, method, body, binary) {
  const { server, token } = await config();
  if (!server || !token) return { ok: false, status: 0, error: "Not connected. Open the Companion options and paste your server address and access token." };
  if (!/^https:\/\//.test(server) && !/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(server))
    return { ok: false, status: 0, error: "The server address must start with https:// (or be http://localhost for development)." };
  let resp;
  try {
    resp = await fetch(server + path, {
      method: method || "GET",
      headers: Object.assign({ "Authorization": "Bearer " + token }, body ? { "Content-Type": "application/json" } : {}),
      body: body ? JSON.stringify(body) : undefined,
      credentials: "omit", cache: "no-store",
    });
  } catch (e) {
    return { ok: false, status: 0, error: "Could not reach JobAgent at " + server + "." };
  }
  if (binary && resp.ok) {
    const name = (resp.headers.get("Content-Disposition") || "").match(/filename="([^"]+)"/);
    return { ok: true, status: resp.status, data: toBase64(await resp.arrayBuffer()), name: name ? name[1] : "Resume.pdf",
             type: resp.headers.get("Content-Type") || "application/pdf" };
  }
  let data = null;
  try { data = await resp.json(); } catch (e) { data = null; }
  if (!resp.ok) {
    const msg = (data && data.error) || (resp.status === 401 ? "Access token invalid or expired. Create a new one on the JobAgent Companion page." : "JobAgent returned HTTP " + resp.status + ".");
    return { ok: false, status: resp.status, error: msg };
  }
  return { ok: true, status: resp.status, data };
}

// ---------------------------------------------------------------- which tab belongs to which application
function urlKey(u) {
  try {
    const x = new URL(u);
    const host = x.hostname.toLowerCase().replace(/^www\./, "");
    const path = x.pathname.toLowerCase().replace(/\/+$/, "").replace(/\/(apply|application|applications)$/, "");
    return { key: host + path, gh: x.searchParams.get("gh_jid") || "", host };
  } catch (e) { return { key: "", gh: "", host: "" }; }
}

async function appsList() {
  const s = await chrome.storage.session.get("apps");
  if (s.apps && Date.now() - s.apps.at < APPS_TTL) return s.apps.list;
  const r = await api("/api/companion/applications");
  const list = r && r.ok ? r.data.applications : [];
  await chrome.storage.session.set({ apps: { at: Date.now(), list } });
  return list;
}

async function matchUrl(url) {
  const { key, gh } = urlKey(url);
  if (!key) return null;
  for (const a of await appsList()) {
    for (const k of a.keys || []) {
      if ((gh && k === "gh:" + gh) || (k && (key === k || key.startsWith(k + "/") || (k.startsWith(key + "/") && key.length > 12))))
        return a;
    }
  }
  return null;
}

async function tabState(tabId) {
  const s = await chrome.storage.session.get("tab:" + tabId);
  const st = s["tab:" + tabId];
  return st && Date.now() - st.since < TAB_TTL ? st : null;
}
async function setTab(tabId, st) { await chrome.storage.session.set({ ["tab:" + tabId]: st }); }

function linkTab(a) {
  return { appId: a ? a.id : null, job: a ? a.title : "", company: a ? a.company : "", since: Date.now(),
           dismissed: false, shown: [] };
}

async function showCard(tabId, st) {
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["card.js"] });
    await chrome.scripting.executeScript({ target: { tabId }, func: (info) => globalThis.JACard.show(info),
                                           args: [{ appId: st.appId, job: st.job, company: st.company }] });
    chrome.action.setBadgeBackgroundColor({ tabId, color: "#0a5bb5" });
    chrome.action.setBadgeText({ tabId, text: st.appId ? "1" : "•" });
  } catch (e) { /* pages the browser protects (chrome://, the web store) */ }
}

chrome.tabs.onCreated.addListener(async (tab) => {
  const s = await chrome.storage.session.get("pending");
  const p = s.pending;
  if (p && Date.now() - p.at < 60000 && (!tab.openerTabId || tab.openerTabId === p.fromTab)) {
    await chrome.storage.session.remove("pending");
    await setTab(tab.id, linkTab(p.app));
  }
});

chrome.tabs.onUpdated.addListener(async (tabId, change, tab) => {
  if (change.status !== "complete" || !tab.url || !/^https?:/.test(tab.url)) return;
  const cfg = await config();
  if (!cfg.token || !cfg.cards) return;
  if (cfg.server && tab.url.startsWith(cfg.server)) return;
  let st = await tabState(tabId);
  if (!st) {
    // opened from a JobAgent application page (the click message can arrive after the tab was created)
    const p = (await chrome.storage.session.get("pending")).pending;
    if (p && Date.now() - p.at < 20000 && (!tab.openerTabId || tab.openerTabId === p.fromTab)) {
      await chrome.storage.session.remove("pending");
      st = linkTab(p.app);
    }
  }
  if (!st) {
    const a = await matchUrl(tab.url);
    if (a) st = linkTab(a);
  }
  if (!st && ATS.test(urlKey(tab.url).host)) {
    if ((await JAFlow.formSize(tabId)) >= 3) st = linkTab(null);
  }
  if (!st || st.dismissed) return;
  const k = urlKey(tab.url).key;
  if (st.shown.indexOf(k) !== -1) return;
  st.shown.push(k);
  await setTab(tabId, st);
  await showCard(tabId, st);
});

chrome.tabs.onRemoved.addListener((tabId) => { chrome.storage.session.remove("tab:" + tabId); });

// ---------------------------------------------------------------- messages
chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (!msg || sender.id !== chrome.runtime.id) return false;
  const fromExtensionPage = sender.url && sender.url.startsWith(chrome.runtime.getURL(""));
  if (msg.type === "api") {
    // only the extension's own pages may call the API (never anything running inside a web page)
    if (!fromExtensionPage) { reply({ ok: false, error: "forbidden" }); return false; }
    api(msg.path, msg.method, msg.body, msg.binary).then(reply);
    return true;
  }
  if (msg.type === "will-open" && sender.tab) {
    // the JobAgent application page says "the user is opening the employer's form for application N"
    config().then(async (cfg) => {
      if (!cfg.server || !sender.url || !sender.url.startsWith(cfg.server + "/applications/")) return reply({});
      const id = parseInt(msg.appId, 10);
      const a = (await appsList()).find((x) => x.id === id) || { id, title: msg.title || "", company: msg.company || "" };
      await chrome.storage.session.set({ pending: { at: Date.now(), app: a, fromTab: sender.tab.id } });
      reply({});
    });
    return true;
  }
  if (msg.type === "card" && sender.tab) {
    // a button on the card in THIS tab; the tab's application comes from our own state, never from the page
    const tabId = sender.tab.id;
    (async () => {
      const st = (await tabState(tabId)) || linkTab(null);
      if (msg.action === "dismiss") { st.dismissed = true; await setTab(tabId, st); chrome.action.setBadgeText({ tabId, text: "" }); return reply({}); }
      if (msg.action === "fill") return reply(await JAFlow.fill(api, tabId, st.appId, sender.tab.url));
      if (msg.action === "applied") {
        const r = await JAFlow.applied(api, tabId, st.appId, sender.tab.url);
        if (r.ok) await chrome.storage.session.remove("apps");
        return reply(r);
      }
      reply({ error: "unknown action" });
    })().catch((e) => reply({ error: String(e && e.message || e) }));
    return true;
  }
  return false;
});
