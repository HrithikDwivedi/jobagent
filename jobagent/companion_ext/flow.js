// JobAgent Companion - the fill / learn / applied flows, shared by the popup and the on-page card (run from the
// service worker). `api(path, method, body, binary)` resolves to {ok, data|error}.
(function (root) {
  async function inject(tabId, state) {
    const target = () => (state.allFrames ? { tabId, allFrames: true } : { tabId, frameIds: [0] });
    try {
      await chrome.scripting.executeScript({ target: target(), files: ["extract.js", "fill.js"] });
    } catch (e) {
      if (!state.allFrames) throw e;
      state.allFrames = false;                 // a frame refused injection: use the main frame only
      await chrome.scripting.executeScript({ target: target(), files: ["extract.js", "fill.js"] });
    }
    return target;
  }

  async function inFrames(target, func, args) {
    const res = await chrome.scripting.executeScript({ target: target(), func, args: args || [] });
    return res.filter((r) => r && r.result != null);
  }

  async function inFrame(tabId, frameId, func, args) {
    const res = await chrome.scripting.executeScript({ target: { tabId, frameIds: [frameId] }, func, args: args || [] });
    return res[0] && res[0].result;
  }

  async function scan(tabId) {
    const state = { allFrames: true };
    const target = await inject(tabId, state);
    const scans = await inFrames(target, () => globalThis.JACompanion.scan());
    return { target, frames: scans.map((s) => ({ frameId: s.frameId, fields: s.result.fields })) };
  }

  // -> {filled, kept, needs: [{question, reason}], total} or {error}
  async function fill(api, tabId, appId, url, onStage) {
    const stage = onStage || function () {};
    stage("Reading the form…");
    const { frames } = await scan(tabId);
    const fields = [];
    for (const fr of frames) for (const f of fr.fields) fields.push(Object.assign({}, f, { key: fr.frameId + ":" + f.key }));
    if (!fields.length) return { error: "No form fields found on this page. Open the application form itself (after pressing Apply), then try again." };
    stage("Matching your answers…");
    const plan = await api("/api/companion/plan", "POST", { app_id: appId || null, url: url || "", fields });
    if (!plan || !plan.ok) return { error: (plan && plan.error) || "JobAgent could not plan this form." };
    const files = {};
    if (plan.data.fields.some((d) => d.action === "upload_resume")) {
      stage("Attaching your resume…");
      const pdf = await api("/api/companion/resume" + (appId ? "?app_id=" + appId : ""), "GET", null, true);
      if (pdf && pdf.ok) files.resume = { data: pdf.data, name: pdf.name, type: pdf.type };
    }
    if (plan.data.cover_letter) {
      files.cover = { data: btoa(unescape(encodeURIComponent(plan.data.cover_letter))), name: "Cover_Letter.txt", type: "text/plain" };
    }
    stage("Filling…");
    let filled = 0, kept = 0;
    const needs = [];
    for (const fr of frames) {
      const prefix = fr.frameId + ":";
      const decisions = plan.data.fields.filter((d) => d.key.startsWith(prefix))
        .map((d) => Object.assign({}, d, { key: d.key.slice(prefix.length) }));
      if (!decisions.length) continue;
      const r = await inFrame(tabId, fr.frameId, (f, d, x) => globalThis.JACompanion.apply(f, d, x), [fr.fields, decisions, files]);
      if (r) { filled += r.filled; kept += r.kept || 0; needs.push(...r.needs); }
    }
    return { filled, kept, needs, total: fields.length, job: plan.data.job };
  }

  async function collect(tabId) {
    const state = { allFrames: true };
    const target = await inject(tabId, state);
    const res = await inFrames(target, () => globalThis.JACompanion.collect());
    return res.flatMap((r) => r.result);
  }

  async function confirmation(tabId) {
    try {
      const state = { allFrames: true };
      const target = await inject(tabId, state);
      return (await inFrames(target, () => globalThis.JACompanion.confirmation())).map((r) => r.result).filter(Boolean)[0] || "";
    } catch (e) { return ""; }
  }

  async function applied(api, tabId, appId, url) {
    if (!appId) return { error: "This page is not linked to a JobAgent application." };
    const conf = await confirmation(tabId);
    const r = await api("/api/companion/applied", "POST", { app_id: appId, url: url || "", confirmation_text: conf });
    return r && r.ok ? { ok: true } : { error: (r && r.error) || "Could not record it." };
  }

  async function formSize(tabId) {
    try {
      const { frames } = await scan(tabId);
      let n = 0, contact = false;
      for (const fr of frames) for (const f of fr.fields) {
        n++;
        if (f.kind === "file" || f.kind === "email" || /e ?mail|resume|cv\b|first name/i.test(f.label || "")) contact = true;
      }
      return contact ? n : 0;
    } catch (e) { return 0; }
  }

  root.JAFlow = { scan, fill, collect, applied, confirmation, formSize, inject, inFrames, inFrame };
})(typeof self !== "undefined" ? self : globalThis);
