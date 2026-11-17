"""Bot-protection detection. The worker NEVER tries to solve or bypass a challenge.

detect_captcha() lists every integration on the page (informational). interactive_challenge() lists only challenges
a person would have to complete (visible widget, puzzle frame, interstitial); those hand the application back to the
user, or (headed) leave the filled form for the user. Invisible, score-based checks do not block."""
from __future__ import annotations

import re

# Markers in raw HTML (script/iframe URLs, widget classes, response fields, interstitial texts).
_HTML_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("recaptcha", re.compile(r"recaptcha|g-recaptcha|grecaptcha", re.I)),
    ("hcaptcha", re.compile(r"hcaptcha|h-captcha", re.I)),
    ("turnstile", re.compile(r"challenges\.cloudflare\.com|cf-turnstile|turnstile", re.I)),
    ("challenge", re.compile(r"verify (that )?you are (a )?human|checking (if the site connection is secure|your "
                             r"browser)|are you a robot|press (&|and) hold|just a moment\.\.\.|cf-chl-|"
                             r"attention required! \| cloudflare|px-captcha|captcha-delivery\.com|arkoselabs|funcaptcha",
                             re.I)),
]


def detect_in_html(html: str) -> list[str]:
    """Pure-text detection (used by tests and as a fallback)."""
    return sorted({kind for kind, rx in _HTML_PATTERNS if rx.search(html or "")})


_JS = r"""() => {
  const kinds = new Set();
  const test = (s) => {
    if (!s) return;
    if (/recaptcha|grecaptcha/i.test(s)) kinds.add('recaptcha');
    if (/hcaptcha/i.test(s)) kinds.add('hcaptcha');
    if (/challenges\.cloudflare\.com|turnstile/i.test(s)) kinds.add('turnstile');
    if (/captcha-delivery\.com|arkoselabs|funcaptcha|px-captcha/i.test(s)) kinds.add('challenge');
  };
  document.querySelectorAll('script[src]').forEach(e => test(e.src));
  document.querySelectorAll('iframe').forEach(e => test(e.src || e.getAttribute('data-src') || ''));
  if (document.querySelector('.g-recaptcha, .grecaptcha-badge, textarea[name="g-recaptcha-response"], #g-recaptcha-response'))
    kinds.add('recaptcha');
  if (document.querySelector('.h-captcha, textarea[name="h-captcha-response"], iframe[data-hcaptcha-widget-id]'))
    kinds.add('hcaptcha');
  if (document.querySelector('.cf-turnstile, input[name="cf-turnstile-response"]')) kinds.add('turnstile');
  document.querySelectorAll('[data-sitekey]').forEach(e => {
    const c = (e.className || '') + ' ' + (e.getAttribute('data-callback') || '');
    if (/h-captcha/i.test(c)) kinds.add('hcaptcha'); else if (/turnstile/i.test(c)) kinds.add('turnstile');
    else kinds.add('recaptcha');
  });
  if (typeof window.grecaptcha !== 'undefined') kinds.add('recaptcha');
  if (typeof window.hcaptcha !== 'undefined') kinds.add('hcaptcha');
  if (typeof window.turnstile !== 'undefined') kinds.add('turnstile');
  const text = ((document.title || '') + ' ' + (document.body ? document.body.innerText.slice(0, 4000) : ''));
  if (/verify (that )?you are (a )?human|checking (if the site connection is secure|your browser)|are you a robot|press (&|and) hold|just a moment\.\.\./i.test(text))
    kinds.add('challenge');
  return Array.from(kinds);
}"""


# A challenge the PERSON must interact with: a rendered checkbox widget, an image/puzzle frame, an interstitial.
# Invisible / score-based integrations (reCAPTCHA v3 or invisible v2, invisible hCaptcha, an unrendered container)
# ask nothing of the person submitting, so they do not block automation. Nothing here ever solves a challenge.
_INTERACTIVE_JS = r"""() => {
  const kinds = new Set();
  const shown = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 30 || r.height < 30) return false;
    const s = getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden' && parseFloat(s.opacity || '1') > 0.05;
  };
  document.querySelectorAll('iframe').forEach((f) => {
    const src = f.src || f.getAttribute('data-src') || '';
    if (!src || !shown(f)) return;
    if (/recaptcha\/(api2|enterprise)\/bframe/i.test(src)) kinds.add('recaptcha');               // image challenge
    else if (/recaptcha\/(api2|enterprise)\/anchor/i.test(src) && !/[?&]size=invisible/i.test(src)) kinds.add('recaptcha');
    else if (/hcaptcha\.com/i.test(src) && !/[#?&]size=invisible/i.test(src)) kinds.add('hcaptcha');
    else if (/challenges\.cloudflare\.com/i.test(src)) kinds.add('turnstile');
    else if (/arkoselabs|funcaptcha|captcha-delivery\.com|px-captcha/i.test(src)) kinds.add('challenge');
  });
  document.querySelectorAll('.g-recaptcha, .h-captcha, .cf-turnstile').forEach((el) => {
    if ((el.getAttribute('data-size') || '').toLowerCase() === 'invisible' || !shown(el)) return;
    const frames = Array.from(el.querySelectorAll('iframe'));
    if (frames.length && frames.every((f) => /[?&#]size=invisible/i.test(f.src || ''))) return;  // invisible widget
    kinds.add(el.classList.contains('h-captcha') ? 'hcaptcha' : el.classList.contains('cf-turnstile') ? 'turnstile' : 'recaptcha');
  });
  if (document.querySelector('#px-captcha, [id^="px-captcha"]')) kinds.add('challenge');
  const text = ((document.title || '') + ' ' + (document.body ? document.body.innerText.slice(0, 4000) : ''));
  if (/verify (that )?you are (a )?human|checking (if the site connection is secure|your browser)|are you a robot|press (&|and) hold|just a moment\.\.\./i.test(text))
    kinds.add('challenge');
  return Array.from(kinds);
}"""


def interactive_challenge(page) -> list[str]:
    """Visible bot challenges that need a person (see _INTERACTIVE_JS). Evaluation errors count as 'unknown'."""
    kinds: set[str] = set()
    for frame in page.frames:
        try:
            kinds.update(frame.evaluate(_INTERACTIVE_JS))
        except Exception:
            if frame is page.main_frame:
                kinds.add("unknown")
    return sorted(kinds)


def detect_captcha(page) -> list[str]:
    """Inspect the page and every frame for CAPTCHA/challenge markers. Errors count as 'unknown' (= present)."""
    kinds: set[str] = set()
    for frame in page.frames:
        try:
            kinds.update(frame.evaluate(_JS))
        except Exception:
            if frame is page.main_frame:
                kinds.add("unknown")
        url = frame.url or ""
        if url and frame is not page.main_frame:
            kinds.update(k for k in detect_in_html(url) if k != "challenge")
    return sorted(kinds)
