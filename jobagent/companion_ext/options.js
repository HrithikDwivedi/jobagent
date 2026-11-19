const $ = (id) => document.getElementById(id);
const status = (msg, ok) => { $("status").textContent = msg; $("status").style.color = ok ? "#15803d" : "#b91c1c"; };
chrome.storage.local.get(["server", "token"]).then((c) => {
  $("server").value = c.server || self.JA_DEFAULT_SERVER || "";
  $("token").value = c.token || "";
});
$("save").addEventListener("click", async () => {
  const server = $("server").value.trim().replace(/\/+$/, "");
  const token = $("token").value.trim();
  if (!/^https:\/\/[^\s/]+/.test(server) && !/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(server))
    return status("Enter the JobAgent address starting with https://", false);
  if (!/^jac_[A-Za-z0-9_-]{20,}$/.test(token)) return status("That does not look like a JobAgent access token (starts with jac_).", false);
  await chrome.storage.local.set({ server, token });
  const r = await chrome.runtime.sendMessage({ type: "api", path: "/api/companion/me" });
  if (r && r.ok) status("Connected as " + (r.data.user.full_name || r.data.user.username) + ".", true);
  else status((r && r.error) || "Connection failed.", false);
});
$("forget").addEventListener("click", async () => {
  await chrome.storage.local.remove(["token"]);
  $("token").value = "";
  status("Disconnected. The token was removed from this browser (revoke it in JobAgent too).", true);
});
