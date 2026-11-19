// JobAgent Companion - runs inside the application page (isolated world) only when you press a button in the
// popup. It reads the form (extract.js, the same discovery auto-apply uses), fills the answers JobAgent returned,
// and highlights what still needs you. It never presses Submit, never touches passwords, never solves CAPTCHAs.
(() => {
  if (globalThis.JACompanion) return;
  const COLORS = { filled: "#15803d", needs: "#d97706", draft: "#2563eb" };
  const SENSITIVE = /password|passcode|\bssn\b|social security|card number|cvv|iban|bank account/i;
  const state = { filled: new Map(), notes: [] };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const txt = (e) => (e ? (e.innerText || e.textContent || "") : "").replace(/\s+/g, " ").trim();
  const norm = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  const byKey = (k) => document.querySelector('[data-ja-id="' + CSS.escape(k) + '"]');
  const visible = (e) => {
    if (!e || !e.isConnected) return false;
    const s = getComputedStyle(e);
    if (s.display === "none" || s.visibility === "hidden") return false;
    const r = e.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  };

  function mark(el, kind, title) {
    if (!el) return;
    const target = (el.type === "file" || !visible(el)) ? (el.closest("label, div, fieldset") || el) : el;
    target.style.outline = "2px solid " + COLORS[kind];
    target.style.outlineOffset = "2px";
    target.dataset.jaMark = kind;
    if (title) target.title = "JobAgent: " + title;
  }
  function clearMarks() {
    document.querySelectorAll("[data-ja-mark]").forEach((e) => {
      e.style.outline = ""; e.style.outlineOffset = ""; delete e.dataset.jaMark;
      if ((e.title || "").startsWith("JobAgent: ")) e.title = "";
    });
  }

  // Set a value the way typing does, so React/Angular/Vue forms register it.
  function setNative(el, value) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype
      : el.tagName === "SELECT" ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, "value");
    el.focus({ preventScroll: true });
    if (desc && desc.set) desc.set.call(el, value); else el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new FocusEvent("blur", { bubbles: true }));
  }
  function pick(options, wanted) {
    const w = norm(wanted);
    let best = null, score = 0;
    for (const o of options) {
      const t = norm(o.label);
      const s = t === w ? 3 : (t.startsWith(w) || w.startsWith(t)) && t ? 2 : (t.includes(w) && w) ? 1 : 0;
      if (s > score) { best = o; score = s; }
    }
    return best;
  }
  function ownLabel(e) {
    if (e.labels && e.labels.length) return txt(e.labels[0]);
    return e.getAttribute("aria-label") || e.value || "";
  }
  function groupMembers(f) {
    return (f.members || [f.key]).map(byKey).filter(Boolean);
  }

  async function chooseCombobox(el, value) {
    setNative(el, value);
    el.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    for (let i = 0; i < 8; i++) {
      await sleep(150);
      const opts = Array.from(document.querySelectorAll('[role="option"], li[id*="option"], .select__option'))
        .filter(visible).map((o) => ({ el: o, label: txt(o) }));
      const hit = pick(opts, value);
      if (hit) { hit.el.click(); return true; }
    }
    el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    return false;
  }

  function attach(el, file) {
    if (!el || el.type !== "file" || !file || !file.data) return false;
    const bin = atob(file.data);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const dt = new DataTransfer();
    dt.items.add(new File([bytes], file.name || "Resume.pdf", { type: file.type || "application/pdf" }));
    el.files = dt.files;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return el.files.length === 1;
  }

  // Apply one decision. Returns "filled" | "needs" | "kept" (left the person's own different answer) | "skipped".
  async function applyOne(f, d, files) {
    const el = byKey(f.key);
    if (!el) return "skipped";
    if (SENSITIVE.test(f.label || "") || el.type === "password") return "skipped";
    const a = d.action;
    try {
      if (a === "fill") {
        if (el.value && el.value.trim() && el.value.trim() !== String(d.value).trim() && !d.overwrite) {
          return "kept";             // the person already typed something different: never overwrite it
        }
        setNative(el, String(d.value));
      } else if (a === "choose") {
        const want = Array.isArray(d.value) ? d.value : [d.value];
        if (f.kind === "select") {
          const opts = Array.from(el.options).map((o) => ({ el: o, label: o.text.trim() }));
          const hit = pick(opts, want[0]);
          if (!hit) { mark(el, "needs", "No option matched: " + want[0]); return "needs"; }
          setNative(el, hit.el.value);
        } else if (f.kind === "buttons") {
          const hit = pick(Array.from(el.querySelectorAll("button")).map((b) => ({ el: b, label: txt(b) })), want[0]);
          if (!hit) return "needs";
          hit.el.click();
        } else if (f.kind === "combobox") {
          if (!(await chooseCombobox(el, want[0]))) { mark(el, "needs", "Pick: " + want[0]); return "needs"; }
        } else {
          const members = groupMembers(f).map((m) => ({ el: m, label: ownLabel(m) }));
          for (const w of want) {
            const hit = pick(members, w);
            if (!hit) { mark(el, "needs", "No option matched: " + w); return "needs"; }
            if (!hit.el.checked) hit.el.click();
          }
        }
      } else if (a === "type_choose") {
        if (!(await chooseCombobox(el, String(d.value)))) { mark(el, "needs", "Choose: " + d.value); return "needs"; }
      } else if (a === "check") {
        if (!el.checked) el.click();
      } else if (a === "upload_resume") {
        if (!attach(el, files && files.resume)) { mark(el, "needs", "Attach your resume here."); return "needs"; }
      } else if (a === "upload_cover") {
        if (!attach(el, files && files.cover)) { mark(el, "needs", "Attach your cover letter here."); return "needs"; }
      } else if (a === "unknown" || a === "needs_consent" || (a === "skip" && f.required)) {
        mark(el, "needs", d.reason || "Needs your answer.");
        return "needs";
      } else {
        return "skipped";
      }
    } catch (e) {
      mark(el, "needs", "Could not fill automatically.");
      return "needs";
    }
    state.filled.set(f.key, d);
    mark(el.type === "radio" || el.type === "checkbox" ? (el.closest("fieldset") || el.parentElement) : el,
         d.source === "ai_draft" ? "draft" : "filled", d.source === "ai_draft" ? "AI draft - review it" : "Filled by JobAgent");
    return "filled";
  }

  function scan() {
    const fields = (globalThis.__jaExtract ? globalThis.__jaExtract() : []).map((f) => {
      const el = byKey(f.key);
      let hint = "";
      if (f.kind === "file") {
        const l = norm(f.label + " " + (f.name || "") + " " + (el && el.id || ""));
        hint = /cover/.test(l) ? "cover_letter" : /resume|cv\b|curriculum/.test(l) ? "resume" : "";
      }
      return Object.assign({}, f, { hint });
    });
    return { url: location.href, title: document.title, fields };
  }

  async function apply(fields, decisions, files) {
    const byK = new Map(decisions.map((d) => [d.key, d]));
    const report = { filled: 0, needs: [], skipped: 0, kept: 0 };
    for (const f of fields) {
      const d = byK.get(f.key);
      if (!d) continue;
      const r = await applyOne(f, d, files);
      if (r === "filled") report.filled++;
      else if (r === "needs") report.needs.push({ key: f.key, question: d.question || f.label, reason: d.reason || "" });
      else if (r === "kept") report.kept++;
      else report.skipped++;
      await sleep(25);   // let dependent questions appear (conditional forms)
    }
    return report;
  }

  function currentValue(f) {
    const el = byKey(f.key);
    if (!el) return "";
    if (f.kind === "select") return el.selectedIndex >= 0 && el.value ? el.options[el.selectedIndex].text.trim() : "";
    if (f.kind === "radio" || f.kind === "checkbox_group") {
      return groupMembers(f).filter((m) => m.checked).map(ownLabel).join(", ");
    }
    if (f.kind === "checkbox" || f.kind === "file" || f.kind === "buttons") return "";
    return (el.value || "").trim();
  }

  // Questions the person answered themselves (not filled by JobAgent): candidates for the answer library.
  function collect() {
    const out = [];
    for (const f of scan().fields) {
      if (SENSITIVE.test(f.label || "")) continue;
      const v = currentValue(f);
      if (!v || v.length > 2000) continue;
      const mine = state.filled.get(f.key);
      if (mine && String(Array.isArray(mine.value) ? mine.value.join(", ") : mine.value).trim() === v) continue;
      out.push({ key: f.key, question: (f.label || "").replace(/\s*[*✱]\s*$/, "").trim(), answer: v });
    }
    return out;
  }

  function openQuestions() {
    return scan().fields.filter((f) => f.kind === "textarea" && !currentValue(f) && f.label && !SENSITIVE.test(f.label))
      .map((f) => ({ key: f.key, question: f.label }));
  }

  function putDraft(key, text) {
    const el = byKey(key);
    if (!el || (el.value || "").trim()) return false;
    setNative(el, text);
    state.filled.set(key, { value: text, source: "ai_draft" });
    mark(el, "draft", "AI draft - review and edit before submitting");
    return true;
  }

  function confirmation() {
    const body = txt(document.body).slice(0, 20000);
    const m = body.match(/(thank(s| you) for (applying|your application|submitting)[^.!]{0,200}[.!]?|(your )?application (has been |was )?(successfully )?(received|submitted|sent)[^.!]{0,200}[.!]?|we(’|')?ve received your application[^.!]{0,200}[.!]?)/i);
    return m ? m[0] : "";
  }

  globalThis.JACompanion = { scan, apply, collect, openQuestions, putDraft, confirmation, clearMarks };
})();
