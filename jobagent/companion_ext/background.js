// JobAgent Companion - service worker. The ONLY place that talks to the JobAgent server: the access token never
// reaches web pages. Requests go to the server address saved in Options.
try { importScripts("defaults.js"); } catch (e) { /* defaults.js is added when the zip is downloaded from JobAgent */ }

async function config() {
  const c = await chrome.storage.local.get(["server", "token"]);
  return { server: (c.server || self.JA_DEFAULT_SERVER || "").replace(/\/+$/, ""), token: c.token || "" };
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

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (!msg || msg.type !== "api") return false;
  // only the extension's own pages may call the API (never content scripts running in web pages)
  if (sender.id !== chrome.runtime.id || (sender.url && !sender.url.startsWith(chrome.runtime.getURL("")))) {
    reply({ ok: false, error: "forbidden" });
    return false;
  }
  api(msg.path, msg.method, msg.body, msg.binary).then(reply);
  return true;
});
