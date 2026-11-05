"""Bot-protection detection. The worker NEVER tries to solve or bypass a challenge: when one is present it
either hands the application back to the user or (headed) fills the form and lets the user submit."""
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
