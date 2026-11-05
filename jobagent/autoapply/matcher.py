"""Map application-form questions to answers the user has CONFIRMED. Never invents an answer.

Sources, in order of precedence:
  1. the application's answers_json (prepared from Settings and reviewed/edited by the user),
  2. candidate facts in Settings (name, email, phone, location, LinkedIn, work authorisation, ...),
  3. the canonical resume's most recent role (current company / title only),
  4. safe defaults that disclose nothing: the "decline to self-identify" option of VOLUNTARY EEO /
     self-identification questions, and a neutral "how did you hear about us" option.
Anything else that is REQUIRED makes the form not automatable (the worker hands it back to the user)."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from ..settings import COUNTRY_NAMES

CHOICE_KINDS = {"select", "radio", "combobox", "buttons", "checkbox_group"}
TEXT_KINDS = {"text", "email", "tel", "url", "number", "textarea", "search", "date"}

EU = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU",
      "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"}
_COUNTRY_ALIASES: dict[str, set[str]] = {
    "united kingdom": {"GB"}, "uk": {"GB"}, "great britain": {"GB"}, "britain": {"GB"}, "england": {"GB"},
    "scotland": {"GB"}, "wales": {"GB"}, "united states": {"US"}, "usa": {"US"}, "us": {"US"}, "america": {"US"},
    "uae": {"AE"}, "dubai": {"AE"}, "european union": EU, "eu": EU, "eea": EU | {"NO", "IS", "LI"},
    "republic of ireland": {"IE"},
}
for _code, _name in COUNTRY_NAMES.items():
    _COUNTRY_ALIASES.setdefault(_name.lower(), {_code})

_DECLINE = re.compile(r"\bdecline\b|\bdont wish\b|\bdo not wish\b|\bprefer not\b|\bnot to (say|disclose|answer|"
                      r"self identify|identify)\b|\bchoose not\b|\brather not\b|\b(do not|dont) want to (answer|"
                      r"disclose|say|identify)\b|\bwish not\b|\bnot disclos|\bno answer\b|\bdont want to\b")
_EEO = re.compile(r"\b(gender|sex|pronouns?|race|racial|ethnicity|ethnic|hispanic|latin[oax]|veteran|disabilit\w*|"
                  r"disabled|sexual orientation|transgender|trans|lgbt\w*|religion|caste|neurodiverg\w*|"
                  r"self identif\w*|demographic|identity)\b")
_CONSENT = re.compile(r"\b(privacy|data (processing|protection|retention)|gdpr|consent|i agree|agree to|i accept|"
                      r"accept the|acknowledge|terms|retain my|store my|process my|personal (data|information)|"
                      r"candidate privacy|policy)\b")
_PLACEHOLDER = re.compile(r"^(select|choose|please select|please choose|select an option|select one|"
                          r"select\.\.\.|--|none selected)\b")
YES = {"yes", "y", "true"}
NO = {"no", "n", "false"}

RULES: list[tuple[str, re.Pattern]] = [(k, re.compile(p)) for k, p in [
    ("skip_if_different", r"\bif different\b|\bif applicable\b.*\bname\b|\bmaiden name\b|\bformer name\b"),
    ("preferred_name", r"\bpreferred (first )?name\b|\bname you( would|d)? prefer\b|\bnickname\b"),
    ("first_name", r"\b(first|given|fore) ?name\b"),
    ("middle_name", r"\bmiddle name\b"),
    ("last_name", r"\b(last|family|sur) ?name\b"),
    ("email", r"\be ?mail\b"),
    ("phone_cc", r"^(?!.*\bwithout\b).*\b(country|dialing|dial|calling) code\b"),
    ("phone", r"\b(phone|mobile|telephone|cell)\b"),
    ("linkedin", r"\blinked ?in\b"),
    ("github", r"\bgit ?hub\b"),
    ("twitter", r"^(x|twitter)( url| handle| profile)?$"),
    ("website", r"\b(website|portfolio|personal site|blog|personal url)\b"),
    ("sponsorship", r"\bsponsor\w*|\bvisa\b|\bimmigration\b"),
    ("work_auth", r"\b(authori[sz]ed|eligible|entitled|permitted|legally able|allowed|right|legal right) to "
                  r"(work|be employed)\b|\bwork (permit|authori[sz]ation)\b|\bright to work\b"),
    ("relocate", r"\brelocat\w*"),
    ("notice", r"\bnotice period\b|\bearliest (start|available|possible start)\b|\bstart date\b|"
               r"\bwhen (can|could|would) you (start|join)\b|\bavailab(le|ility) to (start|join)\b|\bjoining date\b"),
    ("salary", r"\bsalary\b|\bcompensation\b|\bpay (expectation|range|requirement)s?\b|\bexpected (pay|ctc)\b|"
               r"\bctc\b|\bdesired pay\b|\bremuneration\b"),
    ("how_heard", r"\bhow did you (hear|find|learn|come across)\b|\bwhere did you (hear|find|see|learn)\b|"
                  r"\breferral source\b|^source$|\bhow did you discover\b"),
    ("english", r"\benglish\b.*\b(proficien\w*|level|fluen\w*)|\b(proficien\w*|level|fluen\w*)\b.*\benglish\b"),
    ("current_title", r"\bcurrent (job )?(title|role|position)\b|^(job )?title$"),
    ("current_company", r"\bcurrent (company|employer|organi[sz]ation)\b|^(company|employer|organi[sz]ation)$"),
    ("country", r"\bcountry of residence\b|\bwhat country\b|\bwhich country\b|\bcountry (are you|do you)\b|"
                r"\bcountry\b.*\b(based|located|reside|live)\b|^country$|^country region$|^location country$"),
    ("location", r"^(current )?location\b|\bcurrent location\b|\bcity\b|\bwhere are you (currently )?(based|located)\b|"
                 r"\bwhere do you (currently )?live\b|^location city$"),
    ("full_name", r"^(your )?(full |legal |complete )?name$|\bfull name\b|\blegal name\b"),
]]

HOW_HEARD_PREFERENCE = [r"\bcompany (web ?site|careers?( page| site)?)\b", r"\bcareers? (page|site|website)\b",
                        r"\bcompany\b.*\bsite\b", r"\bweb ?site\b", r"\bjob board\b", r"\bonline\b",
                        r"\binternet\b", r"^other$"]


def norm(text: Any) -> str:
    """Lower-case, strip accents, required-markers, punctuation and redundant whitespace."""
    s = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    s = s.lower().replace("'", "").replace("’", "")
    s = re.sub(r"\(\s*(required|optional)\s*\)|\brequired\b$", " ", s)
    s = re.sub(r"[^a-z0-9+#.]+", " ", s)
    s = re.sub(r"(?<![a-z0-9])\.|\.(?![a-z0-9])", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def clean_label(text: str) -> str:
    """Human-readable label without required markers (for audit / reports)."""
    s = re.sub(r"[*✱]+", " ", str(text or ""))
    s = re.sub(r"\(\s*required\s*\)", " ", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip()[:300]


def format_phone(raw: str | None, national: bool = False) -> str | None:
    """'+91 90000-12345' -> '+91 9000012345' (or '9000012345' when national=True). Only reformats the user's own
    number; never adds a country code that was not given. Returns None when the split is ambiguous."""
    if not raw:
        return None
    s = re.sub(r"\(0\)", " ", str(raw).strip())          # '+44 (0)20 ...' trunk prefix
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    m = re.match(r"^\s*\(?\s*(?:\+|00)\s*(\d{1,3})\s*\)?[\s.-]+(.*)$", s)
    if m:
        cc, rest = m.group(1), re.sub(r"\D", "", m.group(2))
        if len(rest) >= 6:
            return rest if national else f"+{cc} {rest}"
        return None
    if re.match(r"^\s*(\+|00)", s):                        # international but no separator after the code
        return None if national else "+" + digits.removeprefix("00")
    return digits


def phone_country_code(raw: str | None) -> str | None:
    m = re.match(r"^\s*\(?\s*(?:\+|00)\s*(\d{1,3})(?=[\s).-])", str(raw or ""))
    return f"+{m.group(1)}" if m else None


def countries_in(label_norm: str) -> set[str]:
    """Country codes named in a (normalised) label."""
    found: set[str] = set()
    for alias, codes in _COUNTRY_ALIASES.items():
        if re.search(r"(?<![a-z])" + re.escape(alias) + r"(?![a-z])", label_norm):
            if alias in ("us", "eu") and not re.search(r"\b(in|the|within|for) (the )?" + alias + r"\b", label_norm):
                continue
            if alias == "ireland" and "northern ireland" in label_norm:
                found.add("GB")
                continue
            found |= codes
    return found


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def choose_option(options: list[str], value: Any, *, prefer_first: bool = False) -> str | None:
    """Pick the option that unambiguously corresponds to `value` (exact, Yes/No synonyms, or all value words
    present). Returns None when no option matches or several match equally (=> not automatable)."""
    v = norm(value)
    if not v:
        return None
    opts = [(o, norm(o)) for o in options if norm(o) and not _PLACEHOLDER.match(norm(o))]
    exact = [o for o, n in opts if n == v]
    if len(exact) == 1:
        return exact[0]
    if v in YES or v in NO:
        syn = YES if v in YES else NO
        hits = [o for o, n in opts if n.split()[0] in syn]
        return hits[0] if len(hits) == 1 else None
    words = v.split()
    hits = [(o, n) for o, n in opts if all(re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", n)
                                            for w in words)]
    if not hits:
        return None
    if len(hits) == 1 or prefer_first:
        return hits[0][0]
    shortest = min(len(n) for _, n in hits)
    best = [o for o, n in hits if len(n) == shortest]
    return best[0] if len(best) == 1 else None


def decline_option(options: list[str]) -> str | None:
    hits = [o for o in options if _DECLINE.search(norm(o))]
    return hits[0] if hits else None


@dataclass
class Field:
    key: str                      # stable handle in the page (data-ja-id)
    label: str
    kind: str                     # text|email|tel|url|number|textarea|select|radio|checkbox|checkbox_group|
                                  # combobox|typeahead|buttons|file
    required: bool = False
    options: list[str] = field(default_factory=list)
    name: str = ""
    hint: str = ""                # driver hint: resume | cover_letter | phone_country | ...

    @property
    def question(self) -> str:
        return clean_label(self.label) or f"(unlabelled {self.kind} field)"


@dataclass
class Decision:
    action: str                   # fill | choose | type_choose | check | upload_resume | upload_cover | skip |
                                  # unknown | needs_consent
    value: Any = None
    source: str = ""
    reason: str = ""
    rule: str = ""

    @property
    def blocking(self) -> bool:
        return self.action in ("unknown", "needs_consent")


class Matcher:
    def __init__(self, candidate: dict, job: dict, answers: list[dict] | None = None,
                 resume_structured: dict | None = None, policy: dict | None = None, cover_letter: str | None = None):
        self.c = candidate or {}
        self.job = job or {}
        self.answers = [a for a in (answers or []) if a.get("question") and a.get("answer")]
        self.resume = resume_structured or {}
        self.policy = policy or {}
        self.cover = (cover_letter or "").strip()

    # ------------------------------------------------------------------ facts
    def _names(self) -> tuple[str | None, str | None, str | None]:
        parts = (self.c.get("full_name") or "").split()
        if not parts:
            return None, None, None
        if len(parts) == 1:
            return parts[0], None, None
        return parts[0], " ".join(parts[1:-1]) or None, parts[-1]

    def residence_country(self) -> str | None:
        loc = self.c.get("current_location") or ""
        tail = loc.split(",")[-1].strip().lower() if loc else ""
        codes = _COUNTRY_ALIASES.get(tail)
        if codes and len(codes) == 1:
            return COUNTRY_NAMES.get(next(iter(codes)))
        return None

    def city(self) -> str | None:
        loc = self.c.get("current_location") or ""
        return loc.split(",")[0].strip() or None

    def _authorised(self, label_norm: str) -> tuple[bool, set[str]] | None:
        codes = countries_in(label_norm)
        if not codes:
            if not self.job.get("country"):
                return None
            codes = {self.job["country"]}
        wa = self.c.get("work_authorization") or {}
        return any(c in wa for c in codes), codes

    def _current_role(self) -> dict:
        exp = self.resume.get("experience") or []
        return exp[0] if exp and isinstance(exp[0], dict) else {}

    def value_for(self, rule: str, label_norm: str, f: Field) -> Any:
        c = self.c
        first, middle, last = self._names()
        if rule == "skip_if_different":
            return None
        if rule in ("first_name", "preferred_name"):
            return first
        if rule == "middle_name":
            return middle
        if rule == "last_name":
            return last
        if rule == "full_name":
            return c.get("full_name")
        if rule == "email":
            return c.get("email")
        if rule == "phone_cc":
            return phone_country_code(c.get("phone"))
        if rule == "phone":
            national = bool(re.search(r"\bwithout (the )?country code\b|\bnational\b|\blocal number\b", label_norm))
            return format_phone(c.get("phone"), national=national)
        if rule == "linkedin":
            return c.get("linkedin_url")
        if rule == "github":
            return c.get("github_url")
        if rule == "website":
            return c.get("website_url") or c.get("portfolio_url")
        if rule == "twitter":
            return None
        if rule == "work_auth":
            r = self._authorised(label_norm)
            return None if r is None else ("Yes" if r[0] else "No")
        if rule == "sponsorship":
            r = self._authorised(label_norm)
            if r is None:
                return None
            if r[0]:
                return "No"
            return "Yes" if c.get("requires_sponsorship_outside") else "No"
        if rule == "relocate":
            v = c.get("willing_to_relocate")
            return None if v is None else ("Yes" if v else "No")
        if rule == "notice":
            return c.get("notice_period") or None
        if rule == "salary":
            return c.get("salary_expectation") or None
        if rule == "how_heard":
            return "Company careers page"
        if rule == "english":
            eng = next((l for l in c.get("languages") or [] if str(l.get("language", "")).lower() == "english"), None)
            return eng["level"] if eng and eng.get("level") not in (None, "unverified") else None
        if rule == "current_title":
            return self._current_role().get("title")
        if rule == "current_company":
            return self._current_role().get("company")
        if rule == "country":
            return self.residence_country()
        if rule == "location":
            return self.city() if f.kind in ("typeahead", "combobox") else c.get("current_location")
        return None

    # ------------------------------------------------------------------ decisions
    def _answer_from_json(self, label_norm: str) -> str | None:
        best, score = None, 0.0
        for a in self.answers:
            q = norm(a["question"])
            r = 1.0 if q == label_norm else _ratio(q, label_norm)
            if r > score:
                best, score = a, r
        return str(best["answer"]) if best and score >= 0.88 else None

    @staticmethod
    def _missing(f: Field, reason: str, rule: str = "") -> Decision:
        if f.required:
            return Decision("unknown", reason=reason, rule=rule)
        return Decision("skip", reason=reason, rule=rule)

    def _render(self, f: Field, value: Any, source: str, rule: str) -> Decision:
        if value is None or str(value).strip() == "":
            return self._missing(f, "No confirmed answer in Settings.", rule)
        if f.kind in TEXT_KINDS:
            return Decision("fill", str(value), source, rule=rule)
        if f.kind == "typeahead" or (f.kind == "combobox" and not f.options):
            return Decision("type_choose", str(value), source, rule=rule)
        if f.kind in CHOICE_KINDS:
            opt = choose_option(f.options, value)
            if opt is None:
                return self._missing(f, f"No option clearly matches the confirmed answer.", rule)
            return Decision("choose", [opt] if f.kind == "checkbox_group" else opt, source, rule=rule)
        if f.kind == "checkbox":
            v = norm(value)
            if v in YES:
                return Decision("check", True, source, rule=rule)
            if v in NO and not f.required:
                return Decision("skip", reason="Confirmed answer is No; box left unticked.", rule=rule)
            return self._missing(f, "Checkbox meaning unclear.", rule)
        return self._missing(f, f"Unsupported field type '{f.kind}'.", rule)

    def decide(self, f: Field) -> Decision:
        label_norm = norm(f.label)
        # -- files
        if f.kind == "file":
            if f.hint == "resume" or re.search(r"\b(resume|cv|curriculum vitae)\b", label_norm):
                return Decision("upload_resume", source="resume_version")
            if f.hint == "cover_letter" or "cover letter" in label_norm:
                if self.cover:
                    return Decision("upload_cover", source="cover_letter")
                return self._missing(f, "No cover letter prepared.")
            return self._missing(f, "Unknown file upload.")
        if f.hint == "cover_letter" or (f.kind == "textarea" and "cover letter" in label_norm):
            return Decision("fill", self.cover, "cover_letter") if self.cover else self._missing(f, "No cover letter.")
        if not label_norm:
            return self._missing(f, "Unlabelled field.")
        # -- voluntary self-identification (never answered with personal data)
        if _EEO.search(label_norm) and not re.search(r"\bsponsor|\bauthori[sz]", label_norm):
            if f.kind in CHOICE_KINDS:
                opt = decline_option(f.options)
                if opt:
                    return Decision("choose", [opt] if f.kind == "checkbox_group" else opt, "default_decline",
                                    rule="eeo")
            if f.kind in ("combobox", "typeahead") and not f.options:
                return self._missing(f, "Voluntary question without a visible decline option.", "eeo")
            return self._missing(f, "Voluntary self-identification: left unanswered.", "eeo")
        # -- required legal acknowledgements / consents
        if f.kind in ("checkbox", "checkbox_group") and _CONSENT.search(label_norm):
            if not f.required:
                return Decision("skip", reason="Optional consent left unticked.", rule="consent")
            if self.policy.get("accept_privacy_notices"):
                if f.kind == "checkbox":
                    return Decision("check", True, "policy_accept_privacy_notices", rule="consent")
                if len(f.options) == 1:
                    return Decision("choose", list(f.options), "policy_accept_privacy_notices", rule="consent")
            return Decision("needs_consent", reason="Required privacy/consent acknowledgement needs you.",
                            rule="consent")
        # -- rule match (decides formatting) + user-confirmed answers_json (decides value)
        rule = next((k for k, rx in RULES if rx.search(label_norm)), "")
        from_json = self._answer_from_json(label_norm)
        if rule == "how_heard":
            if f.kind in CHOICE_KINDS:
                for pat in HOW_HEARD_PREFERENCE:
                    hits = [o for o in f.options if re.search(pat, norm(o))]
                    if len(hits) >= 1:
                        return Decision("choose", [hits[0]] if f.kind == "checkbox_group" else hits[0],
                                        "not_personal", rule=rule)
                return self._missing(f, "No neutral 'how did you hear' option.", rule)
            if f.kind in ("typeahead", "combobox"):
                return self._missing(f, "Free-form 'how did you hear' list.", rule)
        if from_json is not None:
            if rule == "phone":
                from_json = format_phone(from_json) or from_json
            return self._render(f, from_json, "answers_json", rule)
        if not rule:
            return self._missing(f, "No confirmed answer for this question.")
        return self._render(f, self.value_for(rule, label_norm, f), "settings" if rule not in (
            "current_title", "current_company") else "resume", rule)

    def plan(self, fields: list[Field]) -> tuple[list[tuple[Field, Decision]], list[str]]:
        """Decisions for every field + the list of blocking questions (text only)."""
        out, blocking = [], []
        for f in fields:
            d = self.decide(f)
            out.append((f, d))
            if d.blocking:
                blocking.append(f.question)
        return out, blocking
