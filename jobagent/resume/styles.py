"""Access to the resume style conventions in styles.json (company types, countries, role families, wording,
spelling). All country- and company-specific behaviour lives in that data file; this module only reads it."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

from ..settings import COUNTRY_NAMES

STYLES_PATH = Path(__file__).with_name("styles.json")
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTHS_LONG = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
               "November", "December"]


@lru_cache(maxsize=1)
def _load() -> dict:
    return json.loads(STYLES_PATH.read_text(encoding="utf-8"))


def styles() -> dict:
    """The parsed styles.json (shared, read-only: deep-copy before mutating)."""
    return _load()


def styles_hash() -> str:
    return hashlib.sha256(STYLES_PATH.read_bytes()).hexdigest()[:16]


def reload() -> None:
    _load.cache_clear()
    _spelling_rx.cache_clear()
    vocabulary.cache_clear()


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def country_profile(code: str | None) -> dict:
    """Resolved profile for a country code (inheritance applied). Unknown/None -> 'default'."""
    countries = styles()["countries"]
    key = code if code in countries else "default"
    chain, seen = [], set()
    while key and key not in seen:
        seen.add(key)
        prof = countries.get(key, {})
        chain.append(prof)
        key = prof.get("inherits")
    merged: dict = {}
    for prof in reversed(chain):
        merged = _deep_merge(merged, {k: v for k, v in prof.items() if k != "inherits"})
    merged["code"] = code if code in countries and not str(code).startswith("_") else None
    return merged


def role_families() -> dict:
    return styles()["role_families"]


def company_types() -> dict:
    return styles()["company_types"]


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def detect_company_type(employer: str | None, text: str | None) -> tuple[str, list[str]]:
    """Infer big_tech / enterprise / startup from the employer name (editable known list) and job-text signals.
    Returns (company_type, evidence). No evidence -> the configured default (MNC/big_tech)."""
    det = styles()["company_detection"]
    evidence: list[str] = []
    name = _norm_name(employer or "")
    if name:
        compact = name.replace(" ", "")
        for ctype, names in det["known_employers"].items():
            for n in names:
                nn = _norm_name(n)
                if name == nn or compact == nn.replace(" ", "") or name.startswith(nn + " "):
                    return ctype, [f"known employer '{n}'"]
    scores: dict[str, float] = {}
    body = text or ""
    for ctype, sigs in det["signals"].items():
        for sig in sigs:
            m = re.search(sig["pattern"], body, re.I)
            if m:
                scores[ctype] = scores.get(ctype, 0.0) + float(sig["weight"])
                evidence.append(f"{ctype}: '{m.group(0)[:40]}'")
    if not scores:
        return styles()["default_company_type"], []
    # Startup signals are distinctive; big_tech wins ties with enterprise (user prefers MNC-style).
    order = {"startup": 0, "big_tech": 1, "enterprise": 2}
    best = sorted(scores.items(), key=lambda kv: (-kv[1], order.get(kv[0], 9)))[0][0]
    return best, [e for e in evidence if e.startswith(best)]


def match_company_profile(employer: str | None) -> tuple[str, dict] | None:
    """Company profile whose name/aliases match the employer (exact, or alias followed by more words, e.g.
    'Amazon Web Services' -> amazon, 'Morgan Stanley Services Group' -> morgan_stanley)."""
    name = _norm_name(employer or "")
    if not name:
        return None
    best = None
    for key, prof in styles().get("company_profiles", {}).items():
        if key.startswith("_"):
            continue
        for alias in [prof.get("name", "")] + prof.get("aliases", []):
            a = _norm_name(alias)
            if a and (name == a or name.startswith(a + " ") or name.replace(" ", "") == a.replace(" ", "")):
                if best is None or len(a) > best[0]:
                    best = (len(a), key, prof)
    return (best[1], best[2]) if best else None


def sector(name: str | None) -> dict:
    return styles().get("sectors", {}).get(name or "", {})


# ------------------------------------------------------------------ spelling

@lru_cache(maxsize=2)
def _spelling_rx(target: str):
    pairs = styles()["spelling"]["pairs"]
    mapping = {(uk if target == "us" else us).lower(): (us if target == "us" else uk)
               for uk, us in pairs if uk != us}
    if not mapping:
        return None, {}
    alt = "|".join(sorted((re.escape(k) for k in mapping), key=len, reverse=True))
    return re.compile(r"(?<![A-Za-z])(" + alt + r")(?![A-Za-z])", re.I), mapping


def localise_spelling(text: str, target: str) -> str:
    """Convert UK<->US spelling ('uk' or 'us') using the word-pair list; preserves capitalisation."""
    if not text or target not in ("uk", "us"):
        return text
    rx, mapping = _spelling_rx(target)
    if rx is None:
        return text

    def repl(m):
        src = m.group(0)
        dst = mapping[src.lower()]
        if src.isupper():
            return dst.upper()
        if src[0].isupper():
            return dst[0].upper() + dst[1:]
        return dst
    return rx.sub(repl, text)


def to_uk(text: str) -> str:
    return localise_spelling(text, "uk")


# ------------------------------------------------------------------ dates

def format_date(raw: str | None, fmt: str) -> str:
    """'July 2024' -> 'Jul 2024' (mon_yyyy) | 'July 2024' (month_yyyy) | '07/2024' (mm_yyyy). Year-only and
    'Present' pass through unchanged."""
    if not raw:
        return raw or ""
    s = raw.strip()
    m = re.match(r"^([A-Za-z]{3,9})\.?\s+(\d{4})$", s)
    if not m:
        return s[:1].upper() + s[1:] if s.lower() in ("present", "current", "now") else s
    mon = m.group(1)[:3].title()
    if mon not in MONTHS:
        return s
    i = MONTHS.index(mon)
    if fmt == "mm_yyyy":
        return f"{i + 1:02d}/{m.group(2)}"
    if fmt == "month_yyyy":
        return f"{MONTHS_LONG[i]} {m.group(2)}"
    return f"{mon} {m.group(2)}"


# ------------------------------------------------------------------ vocabulary allowlist for the fact guard

def _strings(node, out: list[str]):
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, list):
        for x in node:
            _strings(x, out)
    elif isinstance(node, dict):
        for x in node.values():
            _strings(x, out)


@lru_cache(maxsize=1)
def vocabulary() -> frozenset[str]:
    """Words the tailoring itself may introduce (headings, header phrases, headlines, verbs, neutral wording
    replacements, spelling variants, month names, country names). Deliberately EXCLUDES the employer lists
    and detection patterns so an injected company name is still caught by the guard."""
    st = styles()
    texts: list[str] = []
    _strings(st["headings"], texts)
    _strings(st["header_phrases"], texts)
    for prof in st["countries"].values():
        _strings(prof.get("headings", {}), texts)
    for fam in st["role_families"].values():
        texts += [fam["label"], fam["headline"]]
    for ct in st["company_types"].values():
        texts += ct["verbs"] + [ct["label"]]
        _strings(ct.get("headings", {}), texts)
    w = st["wording"]
    texts += [r["replacement"] for r in w["replacements"]]
    for syns in w["verb_synonyms"].values():
        texts += syns
    texts += w["past_tense_irregular"]
    _strings(st["spelling"]["pairs"], texts)
    texts += MONTHS + MONTHS_LONG + list(COUNTRY_NAMES.values()) + ["Present"]
    words = set()
    for t in texts:
        for wd in re.findall(r"[A-Za-z][A-Za-z0-9+#]*", t):
            words.add(to_uk(wd.lower()))
    return frozenset(words)
