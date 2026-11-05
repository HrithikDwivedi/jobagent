"""ATS form drivers (Playwright sync API). They read the EMPLOYER'S hosted form, fill fields from Matcher decisions,
click submit, and detect the employer's confirmation. They never solve CAPTCHAs and never navigate off the ATS."""
from __future__ import annotations

import random
import re
import time
from urllib.parse import urlsplit

from .matcher import Decision, Field, choose_option, norm

# Generic, label-based field discovery. Every field gets a stable `data-ja-id` handle.
EXTRACT_JS = r"""() => {
  window.__jaN = window.__jaN || 0;
  const tag = (e) => { if (!e.dataset.jaId) e.dataset.jaId = 'f' + (window.__jaN++); return e.dataset.jaId; };
  const txt = (e) => (e ? (e.innerText || e.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const visible = (e) => {
    if (!e || !e.isConnected) return false;
    const s = getComputedStyle(e);
    if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity || '1') === 0) return false;
    const r = e.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  };
  const LABELISH = 'legend, .application-label, [class*="question-title"], [class*="question_title"], ' +
                   '[class*="label"], label, [id$="-label"], .text';
  const groupLabel = (e, stopAt) => {
    let a = e.parentElement;
    for (let i = 0; a && i < 7; i++, a = a.parentElement) {
      if (a === stopAt || a.tagName === 'FORM' || a.tagName === 'BODY') break;
      for (const l of a.querySelectorAll(LABELISH)) {
        if (l.contains(e) || l.querySelector('input,select,textarea,button')) continue;
        if (l.tagName === 'LABEL' && l.htmlFor && document.getElementById(l.htmlFor) &&
            ['radio', 'checkbox'].includes(document.getElementById(l.htmlFor).type)) continue;
        const t = txt(l);
        if (t) return t;
      }
    }
    return '';
  };
  const ownLabel = (e) => {
    let t = '';
    const lb = e.getAttribute('aria-labelledby');
    if (lb) t = lb.split(/\s+/).map(id => txt(document.getElementById(id))).join(' ').trim();
    if (!t && e.labels && e.labels.length) t = txt(e.labels[0]);
    if (!t && e.getAttribute('aria-label')) t = e.getAttribute('aria-label').trim();
    return t;
  };
  const isRequired = (els, label) => {
    for (const e of els) {
      if (e.required || e.getAttribute('aria-required') === 'true') return true;
      let a = e.parentElement;
      for (let i = 0; a && i < 4; i++, a = a.parentElement) {
        if (a.getAttribute && a.getAttribute('aria-required') === 'true') return true;
        if (/(^|[\s_-])required($|[\s_-])|_required/i.test(a.className || '')) return true;
      }
    }
    return /[*✱]/.test(label || '');
  };
  const skip = (e) => {
    const t = (e.type || '').toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image'].includes(t)) return true;
    if (/recaptcha|h-captcha|turnstile/i.test(e.name || '') || /recaptcha|h-captcha/i.test(e.id || '')) return true;
    if (e.closest('[role="listbox"], .iti__dropdown-content, .iti__country-list')) return true;
    if (e.getAttribute('aria-hidden') === 'true' && t !== 'file') return true;
    if (t === 'search') return true;
    if (e.disabled || e.readOnly && t !== 'file' && e.getAttribute('role') !== 'combobox') return true;
    return false;
  };
  const out = [];
  const seenGroups = new Set();
  // Yes/No button groups (Ashby style)
  const btnContainers = new Set();
  document.querySelectorAll('button').forEach(b => {
    const p = b.parentElement;
    if (!p || btnContainers.has(p)) return;
    const bs = Array.from(p.querySelectorAll(':scope > button'));
    const labels = bs.map(x => txt(x).toLowerCase());
    if (bs.length === 2 && labels.includes('yes') && labels.includes('no') && visible(p)) {
      btnContainers.add(p);
      const label = groupLabel(p, null);
      const hidden = p.parentElement ? Array.from(p.parentElement.querySelectorAll('input')) : [];
      hidden.forEach(h => { h.dataset.jaSkip = '1'; });
      out.push({key: tag(p), label, kind: 'buttons', required: isRequired([p, ...hidden], label),
                options: bs.map(x => txt(x)), name: ''});
    }
  });
  const els = Array.from(document.querySelectorAll('input, textarea, select'));
  for (const e of els) {
    if (e.dataset.jaSkip === '1' || skip(e)) continue;
    const t = (e.type || e.tagName).toLowerCase();
    if (t !== 'file' && !visible(e)) continue;
    if (t === 'radio' || t === 'checkbox') {
      const gname = e.name || ('__' + tag(e));
      if (seenGroups.has(gname)) continue;
      seenGroups.add(gname);
      const group = e.name ? els.filter(x => x.name === e.name && x.type === e.type && visible(x)) : [e];
      const own = group.map(x => ownLabel(x) || x.value || '');
      const fs = e.closest('fieldset');
      let q = (fs && fs.querySelector('legend')) ? txt(fs.querySelector('legend')) : groupLabel(e, null);
      let kind = t === 'radio' ? 'radio' : (group.length > 1 ? 'checkbox_group' : 'checkbox');
      let label = q;
      if (kind === 'checkbox') {
        const o = own[0];
        label = (o && o.length > 40) ? o : (q + ' ' + o).trim();
      }
      group.forEach(x => tag(x));
      out.push({key: tag(e), label, kind, required: isRequired(group, q + ' ' + (kind === 'checkbox' ? own[0] : '')),
                options: own, name: e.name || '', members: group.map(x => x.dataset.jaId)});
      continue;
    }
    let label = ownLabel(e) || groupLabel(e, null) || e.placeholder || '';
    let kind = e.tagName === 'SELECT' ? 'select' : e.tagName === 'TEXTAREA' ? 'textarea' : t;
    if (e.getAttribute('role') === 'combobox' && e.tagName !== 'SELECT') kind = 'combobox';
    const options = e.tagName === 'SELECT'
      ? Array.from(e.options).filter(o => o.value !== '' && !o.disabled).map(o => o.text.trim()) : [];
    out.push({key: tag(e), label, kind, required: isRequired([e], label), options, name: e.name || '',
              id: e.id || ''});
  }
  return out;
}"""

CONFIRM_TEXT = re.compile(
    r"(thank(s| you) for (applying|your application|submitting)[^.!\n]*[.!]?|"
    r"(your )?application (has been |was )?(successfully )?(received|submitted|sent)[^.!\n]*[.!]?|"
    r"we(’|')?ve received your application[^.!\n]*[.!]?|we have received your application[^.!\n]*[.!]?|"
    r"application submitted[^.!\n]*[.!]?)", re.I)
CONFIRM_URL = re.compile(r"/(confirmation|thanks|thank-you|thank_you|thankyou|success|submitted|applied)(\b|/|$|\?)",
                         re.I)


CONFIRM_LOOSE = re.compile(r"\b(thank|thanks|received|success|successfully|submitted|applied)\b", re.I)
CONFIRM_ERROR = re.compile(r"\b(not found|error|went wrong|try again|failed|invalid|required)\b", re.I)


class DriverError(Exception):
    pass


class BaseDriver:
    name = "generic"
    hosts: tuple[str, ...] = ()
    form_selectors = ("form", "input[type=file]", "input[type=email]")
    submit_selectors = ("button[type=submit]", "input[type=submit]")
    submit_text = re.compile(r"^\s*(submit( application| my application)?|apply( now)?|send application)\s*$", re.I)
    success_selectors: tuple[str, ...] = ()

    @classmethod
    def matches(cls, host: str) -> bool:
        return any(host == h or host.endswith("." + h) for h in cls.hosts)

    def form_url(self, url: str) -> str:
        return url

    def family(self, url: str) -> tuple[str, ...]:
        """Hosts this application may navigate within (the ATS domain, or exactly the given host)."""
        host = (urlsplit(url).hostname or "").lower()
        return self.hosts or (host,)

    # ---------------------------------------------------------------- page helpers
    def form_frame(self, page, timeout_ms: int = 15000):
        """The frame containing the application form (main page or an embedded ATS iframe)."""
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            for frame in [page.main_frame] + [f for f in page.frames if f is not page.main_frame]:
                if frame is not page.main_frame and not self._frame_allowed(frame.url):
                    continue
                for sel in self.form_selectors:
                    try:
                        if frame.locator(sel).count():
                            return frame
                    except Exception:
                        pass
            if time.monotonic() > deadline:
                return None
            page.wait_for_timeout(250)

    def _frame_allowed(self, url: str) -> bool:
        host = (urlsplit(url or "").hostname or "").lower()
        return any(host == h or host.endswith("." + h) for h in (self.hosts or ()))

    def prepare_form(self, frame, has_cover: bool) -> None:
        """ATS-specific UI toggles (e.g. 'enter cover letter manually'). Must not type any data."""

    def hint(self, raw: dict) -> str:
        return ""

    def extract(self, frame) -> list[Field]:
        raw = frame.evaluate(EXTRACT_JS)
        fields = []
        for r in raw:
            f = Field(key=r["key"], label=r.get("label") or "", kind=r["kind"], required=bool(r.get("required")),
                      options=[o for o in r.get("options") or [] if o], name=r.get("name") or "", hint=self.hint(r))
            if f.kind == "combobox":
                f.options = self._combobox_options(frame, f)
                if not f.options:
                    f.kind = "typeahead"
            fields.append(f)
        return fields

    def _combobox_options(self, frame, f: Field) -> list[str]:
        loc = frame.locator(f'[data-ja-id="{f.key}"]')
        try:
            loc.click(timeout=3000)
            frame.wait_for_timeout(200)
            opts = [t.strip() for t in frame.locator('[role="option"]:visible').all_inner_texts() if t.strip()]
            loc.press("Escape")
            frame.wait_for_timeout(50)
            return opts
        except Exception:
            return []

    # ---------------------------------------------------------------- filling
    def fill(self, frame, plan: list[tuple[Field, Decision]], files: dict, delay=(0.15, 0.6)) -> list[str]:
        """Apply decisions. Returns the questions that were filled (labels only)."""
        done = []
        for f, d in plan:
            if d.action in ("skip", "unknown", "needs_consent"):
                continue
            self._pause(delay)
            sel = f'[data-ja-id="{f.key}"]'
            loc = frame.locator(sel).first
            if d.action == "upload_resume":
                loc.set_input_files(files["resume"])
            elif d.action == "upload_cover":
                loc.set_input_files(files["cover"])
            elif d.action == "fill":
                loc.fill(str(d.value))
                if loc.input_value() != str(d.value):
                    raise DriverError(f"Field '{f.question}' did not accept the value.")
            elif d.action == "check":
                loc.check()
            elif d.action == "choose":
                self._choose(frame, f, d.value)
            elif d.action == "type_choose":
                self._type_choose(frame, f, str(d.value))
            done.append(f.question)
        return done

    def _choose(self, frame, f: Field, value) -> None:
        sel = f'[data-ja-id="{f.key}"]'
        if f.kind == "select":
            frame.locator(sel).select_option(label=value)
        elif f.kind in ("radio", "checkbox_group"):
            wanted = value if isinstance(value, list) else [value]
            members = frame.locator(f'input[name="{f.name}"]') if f.name else frame.locator(sel)
            n, hit = members.count(), 0
            for i in range(n):
                m = members.nth(i)
                lab = m.evaluate("e => ((e.labels && e.labels[0] && e.labels[0].innerText) || "
                                 "e.getAttribute('aria-label') || e.value || '').trim()")
                if lab in wanted:
                    m.check()
                    hit += 1
            if hit != len(wanted):
                raise DriverError(f"Option not found for '{f.question}'.")
        elif f.kind == "checkbox":
            frame.locator(sel).check()
        elif f.kind == "buttons":
            frame.locator(sel).locator("button", has_text=re.compile(r"^\s*" + re.escape(value) + r"\s*$")).first.click()
        elif f.kind == "combobox":
            loc = frame.locator(sel)
            loc.click()
            self._click_option(frame, f, value)
        else:
            raise DriverError(f"Cannot choose on field type {f.kind}.")

    def _click_option(self, frame, f: Field, value: str) -> None:
        opts = frame.locator('[role="option"]:visible')
        try:
            opts.first.wait_for(timeout=4000)
        except Exception:
            raise DriverError(f"No options appeared for '{f.question}'.") from None
        texts = [t.strip() for t in opts.all_inner_texts()]
        if value not in texts:
            raise DriverError(f"Option '{value}' not offered for '{f.question}'.")
        opts.nth(texts.index(value)).click()

    def _type_choose(self, frame, f: Field, value: str) -> None:
        loc = frame.locator(f'[data-ja-id="{f.key}"]')
        loc.click()
        loc.fill("")
        loc.press_sequentially(value, delay=40)
        opts = frame.locator('[role="option"]:visible')
        try:
            opts.first.wait_for(timeout=6000)
        except Exception:
            raise DriverError(f"No suggestions for '{f.question}'.") from None
        texts = [t.strip() for t in opts.all_inner_texts()]
        pick = choose_option(texts, value, prefer_first=True)
        if pick is None:
            raise DriverError(f"No suggestion matches the confirmed answer for '{f.question}'.")
        opts.nth(texts.index(pick)).click()

    @staticmethod
    def _pause(delay) -> None:
        if delay and delay[1] > 0:
            time.sleep(random.uniform(*delay))

    # ---------------------------------------------------------------- submit / confirm
    def find_submit(self, frame):
        for sel in self.submit_selectors:
            loc = frame.locator(sel).filter(visible=True)
            if loc.count():
                return loc.first
        loc = frame.get_by_role("button", name=self.submit_text)
        if loc.count():
            return loc.first
        return None

    def confirmation(self, page, before_text: str, before_url: str) -> str | None:
        """Employer confirmation text if the page now shows one that was NOT there before submitting."""
        frames = [page.main_frame] + [f for f in page.frames if f is not page.main_frame]
        for frame in frames:
            try:
                text = frame.evaluate("() => document.body ? document.body.innerText : ''") or ""
            except Exception:
                continue
            for sel in self.success_selectors:
                try:
                    loc = frame.locator(sel)
                    if loc.count() and loc.first.is_visible():
                        t = re.sub(r"\s+", " ", loc.first.inner_text()).strip()
                        if t:
                            return t[:500]
                except Exception:
                    pass
            for m in CONFIRM_TEXT.finditer(text):
                phrase = m.group(0).strip()
                if phrase.lower() not in before_text.lower():
                    line = next((ln for ln in text.splitlines() if phrase in ln), phrase)
                    return re.sub(r"\s+", " ", line).strip()[:500]
        url = page.url
        if url != before_url and CONFIRM_URL.search(urlsplit(url).path):
            # A confirmation-style URL counts only together with positive, non-error page content.
            try:
                body = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
            except Exception:
                body = ""
            if CONFIRM_LOOSE.search(body) and not CONFIRM_ERROR.search(body):
                first = next((ln.strip() for ln in body.splitlines() if CONFIRM_LOOSE.search(ln)), "")
                return (f"Confirmation page {urlsplit(url).path}: {first}")[:500]
        return None


class GreenhouseDriver(BaseDriver):
    name = "greenhouse"
    hosts = ("greenhouse.io",)
    form_selectors = ("form#application-form", "form#application_form", "#application-form", "#application_form",
                      "form[action*='applications']")
    submit_selectors = ("#application-form button[type=submit]", "#submit_app", "button[type=submit]",
                        "input[type=submit]")
    success_selectors = ("#application_confirmation", ".application-confirmation", "[data-testid='confirmation']")

    def prepare_form(self, frame, has_cover: bool) -> None:
        if not has_cover:
            return
        btn = frame.locator("[id*='cover_letter'] button, .cover-letter button, "
                            "[data-field='cover_letter'] button").filter(has_text=re.compile("enter manually", re.I))
        try:
            if btn.count() and btn.first.is_visible():
                btn.first.click()
                frame.wait_for_timeout(150)
        except Exception:
            pass

    def hint(self, raw: dict) -> str:
        rid = raw.get("id") or ""
        if rid in ("resume", "resume_text"):
            return "resume"
        if rid.startswith("cover_letter"):
            return "cover_letter"
        return ""


class LeverDriver(BaseDriver):
    name = "lever"
    hosts = ("lever.co",)
    form_selectors = ("form#application-form", "form.application-form", "#application-form")
    submit_selectors = ("#btn-submit", "button.postings-btn[type=submit]", "button[type=submit]")
    success_selectors = (".application-confirmation", "[data-qa='msg-submit-success']", ".thanks-page")

    def form_url(self, url: str) -> str:
        parts = urlsplit(url)
        path = parts.path.rstrip("/")
        if re.match(r"^/[^/]+/[0-9a-f-]{8,}$", path):
            return parts._replace(path=path + "/apply").geturl()
        return url

    def hint(self, raw: dict) -> str:
        n = raw.get("name") or ""
        if n == "resume":
            return "resume"
        if n == "comments":
            return "cover_letter"
        return ""


class AshbyDriver(BaseDriver):
    name = "ashby"
    hosts = ("ashbyhq.com",)
    form_selectors = (".ashby-application-form-container", "#_systemfield_name", "[class*='application-form']")
    submit_selectors = ("button.ashby-application-form-submit-button", "button[class*='submit-button']")
    success_selectors = (".ashby-application-form-success-container", "[class*='success-container']")

    def form_url(self, url: str) -> str:
        parts = urlsplit(url)
        path = parts.path.rstrip("/")
        if re.match(r"^/[^/]+/[0-9a-f-]{8,}$", path):
            return parts._replace(path=path + "/application").geturl()
        return url

    def hint(self, raw: dict) -> str:
        rid = raw.get("id") or ""
        if rid == "_systemfield_resume":
            return "resume"
        return ""


class GenericDriver(BaseDriver):
    name = "generic"


DRIVERS = (GreenhouseDriver, LeverDriver, AshbyDriver)


def driver_for(url: str) -> BaseDriver | None:
    host = (urlsplit(url or "").hostname or "").lower()
    for cls in DRIVERS:
        if cls.matches(host):
            return cls()
    return None


def host_in(host: str, family: tuple[str, ...]) -> bool:
    host = (host or "").lower()
    return any(host == h or host.endswith("." + h) for h in family)


def submitted_text(frame) -> str:
    try:
        return frame.evaluate("() => document.body ? document.body.innerText : ''") or ""
    except Exception:
        return ""


__all__ = ["BaseDriver", "GreenhouseDriver", "LeverDriver", "AshbyDriver", "GenericDriver", "DriverError",
           "driver_for", "host_in", "CONFIRM_TEXT", "CONFIRM_URL", "norm"]
