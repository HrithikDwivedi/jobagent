"""Deterministic normalisation: URLs, employer names, job titles, locations and stable job identity."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = re.compile(
    r"^(utm_.*|gh_src|gh_jid_src|source|src|ref|referrer|refid|trk|trackingid|lever-source|lever-origin|"
    r"fbclid|gclid|mc_cid|mc_eid|_hsenc|_hsmi|campaign|medium|origin|share|cmp|jobboard|from)$", re.I)
# Query keys that identify the job itself and must be kept.
IDENTITY_PARAMS = {"gh_jid", "jobid", "job_id", "id", "jid", "reqid", "req_id", "requisitionid", "token"}


def canonical_url(url: str | None) -> str | None:
    """Lower-case scheme/host, drop fragments, default ports, trailing slashes and tracking parameters."""
    if not url:
        return None
    url = url.strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    port = f":{parts.port}" if parts.port and parts.port not in (80, 443) else ""
    path = re.sub(r"/+", "/", parts.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
             if k.lower() in IDENTITY_PARAMS or not TRACKING_PARAMS.match(k)]
    query.sort()
    return urlunsplit(("https", host + port, path, urlencode(query), ""))


def url_host(url: str | None) -> str | None:
    try:
        h = urlsplit(url or "").hostname
    except ValueError:
        return None
    return h.lower() if h else None


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")


COMPANY_SUFFIXES = re.compile(
    r"\b(inc|incorporated|llc|ltd|limited|plc|gmbh|ag|se|sa|sas|sarl|bv|b\.v|nv|n\.v|oy|ab|as|aps|a/s|"
    r"spa|s\.p\.a|srl|s\.r\.l|sp z o\.?o|kg|co|corp|corporation|company|group|holding|holdings|technologies|"
    r"technology|software|labs|international|europe|emea|ireland|uk|deutschland)\b\.?", re.I)


def normalize_employer(name: str | None) -> str:
    if not name:
        return "unknown"
    s = _ascii(name).lower()
    s = re.sub(r"\(.*?\)", " ", s)
    s = s.replace("&", " and ")
    prev = None
    while prev != s:
        prev = s
        s = COMPANY_SUFFIXES.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s or _ascii(name).lower().strip() or "unknown"


TITLE_SYNONYMS = [
    (r"\bsr\.?\b", "senior"), (r"\bjr\.?\b", "junior"), (r"\bsw\b", "software"), (r"\bswe\b", "software engineer"),
    (r"\bsde\b", "software development engineer"), (r"\bdev\b", "developer"), (r"\beng\b", "engineer"),
    (r"\bfull[- ]?stack\b", "fullstack"), (r"\bback[- ]?end\b", "backend"), (r"\bfront[- ]?end\b", "frontend"),
    (r"\bc sharp\b", "c#"), (r"\bdot ?net\b", ".net"), (r"\bml\b", "machine learning"), (r"\bai\b", "ai"),
    (r"\b1\b", "i"), (r"\b2\b", "ii"), (r"\b3\b", "iii"), (r"\blevel i\b", "i"),
]
TITLE_NOISE = re.compile(r"\((?:m|f|w|d|x|all genders?|m/w/d|m/f/d|f/m/d|w/m/d|h/f|f/h)[^)]*\)|\b(?:m|w|f|d)/(?:m|w|f|d)(?:/(?:m|w|f|d|x))?\b"
                         r"|\bremote\b|\bhybrid\b|\bon-?site\b|\bfull[- ]time\b|\bpermanent\b|\bcontract\b", re.I)


def normalize_title(title: str | None) -> str:
    if not title:
        return ""
    s = _ascii(title).lower()
    s = TITLE_NOISE.sub(" ", s)
    s = re.sub(r"[\-–—|/,:]+", " ", s)
    s = re.sub(r"\s+-\s+.*$", "", s)
    for pat, rep in TITLE_SYNONYMS:
        s = re.sub(pat, rep, s)
    s = re.sub(r"[^a-z0-9#+. ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


SENIORITY_PATTERNS = [
    ("principal", r"\b(principal|distinguished|fellow)\b"),
    ("staff", r"\bstaff\b"),
    ("lead", r"\b(lead|head of|tech lead|team lead|manager|director|vp|vice president|svp|evp|md)\b"),
    # Bank grades: AVP ~4-7 yrs; "Associate" at banks is mid-level and is left to the experience rules.
    ("senior", r"\b(senior|sr|iii|iv|level 3|l5|l6|avp|assistant vice president)\b"),
    ("mid", r"\b(ii|mid|intermediate|level 2|l4)\b"),
    ("junior", r"\b(junior|jr|graduate|grad|entry|associate|i|level 1|l3|new grad|early career)\b"),
    ("intern", r"\b(intern|internship|working student|werkstudent|trainee|apprentice)\b"),
]


def seniority(title: str | None) -> str:
    t = normalize_title(title)
    for level, pat in SENIORITY_PATTERNS:
        if re.search(pat, t):
            return level
    return "unspecified"


# ---------------------------------------------------------------- locations

COUNTRY_ALIASES = {
    "IE": ["ireland", "dublin", "cork", "galway", "limerick", "eire"],
    "GB": ["united kingdom", "uk", "u.k.", "england", "scotland", "wales", "northern ireland", "london",
           "manchester", "edinburgh", "glasgow", "bristol", "cambridge", "oxford", "birmingham", "leeds", "belfast",
           "great britain", "britain", "reading", "aberdeen", "dundee", "inverness", "stirling", "livingston"],
    "DE": ["germany", "deutschland", "berlin", "munich", "münchen", "muenchen", "hamburg", "frankfurt", "cologne",
           "köln", "koeln", "stuttgart", "düsseldorf", "dusseldorf", "leipzig", "dresden", "nuremberg", "nürnberg",
           "karlsruhe", "hannover", "bonn", "essen", "dortmund", "heidelberg", "mannheim", "aachen", "potsdam"],
    "NL": ["netherlands", "the netherlands", "holland", "amsterdam", "rotterdam", "utrecht", "the hague",
           "den haag", "eindhoven", "groningen", "delft", "leiden", "haarlem"],
    "SE": ["sweden", "stockholm", "gothenburg", "göteborg", "malmö", "malmo", "uppsala", "lund"],
    "DK": ["denmark", "copenhagen", "københavn", "aarhus", "odense"],
    "FI": ["finland", "helsinki", "espoo", "tampere", "oulu", "turku"],
    "AT": ["austria", "vienna", "wien", "graz", "linz", "salzburg", "innsbruck"],
    "CH": ["switzerland", "zurich", "zürich", "geneva", "genève", "basel", "lausanne", "bern", "zug", "lugano"],
    "BE": ["belgium", "brussels", "bruxelles", "antwerp", "antwerpen", "ghent", "gent", "leuven", "liège"],
    "LU": ["luxembourg"],
    "IT": ["italy", "italia", "milan", "milano", "rome", "roma", "turin", "torino", "bologna", "florence", "naples"],
    "PT": ["portugal", "lisbon", "lisboa", "porto", "braga", "coimbra"],
    "ES": ["spain", "españa", "espana", "madrid", "barcelona", "valencia", "seville", "sevilla", "malaga", "málaga",
           "bilbao"],
    "PL": ["poland", "polska", "warsaw", "warszawa", "krakow", "kraków", "wroclaw", "wrocław", "gdansk", "gdańsk",
           "poznan", "poznań", "lodz", "łódź", "katowice"],
    "CZ": ["czechia", "czech republic", "prague", "praha", "brno", "ostrava"],
    "EE": ["estonia", "tallinn", "tartu"],
    "FR": ["france", "paris", "lyon", "toulouse", "nantes", "bordeaux", "lille", "marseille", "nice", "grenoble"],
    "NO": ["norway", "oslo", "bergen", "trondheim"],
    "LT": ["lithuania", "vilnius", "kaunas"],
    "LV": ["latvia", "riga"],
    "RO": ["romania", "bucharest", "cluj", "iasi"],
    "GR": ["greece", "athens", "thessaloniki"],
    "HU": ["hungary", "budapest"],
    "MT": ["malta", "valletta"],
    "CY": ["cyprus", "limassol", "nicosia"],
    "IN": ["india", "bengaluru", "bangalore", "mumbai", "pune", "hyderabad", "chennai", "delhi", "new delhi",
           "gurgaon", "gurugram", "noida", "kolkata", "ahmedabad"],
    "US": ["united states", "usa", "u.s.", "us", "new york", "san francisco", "seattle", "austin", "boston",
           "chicago", "los angeles", "denver", "atlanta", "remote - us", "remote us", "nyc", "sf"],
    "CA": ["canada", "toronto", "vancouver", "montreal", "montréal", "ottawa", "calgary", "waterloo", "edmonton",
           "winnipeg", "halifax", "kitchener", "mississauga", "victoria, bc", "quebec"],
    "AU": ["australia", "sydney", "melbourne", "brisbane", "perth", "adelaide", "canberra", "gold coast", "hobart"],
    "SG": ["singapore"],
    "AE": ["united arab emirates", "uae", "u.a.e.", "dubai", "abu dhabi", "sharjah"],
    "JP": ["japan", "tokyo", "osaka", "kyoto", "fukuoka", "yokohama", "nagoya"],
    "IL": ["israel", "tel aviv"],
    "BR": ["brazil", "são paulo", "sao paulo"],
    "MX": ["mexico", "mexico city"],
}
_ALIAS_RE = [(code, re.compile(r"(?<![a-z])(" + "|".join(re.escape(a) for a in sorted(aliases, key=len, reverse=True))
                               + r")(?![a-z])", re.I)) for code, aliases in COUNTRY_ALIASES.items()]
REGION_WORDS = {
    "europe": "EU_REGION", "emea": "EMEA_REGION", "eu": "EU_REGION", "european union": "EU_REGION",
    "worldwide": "WORLDWIDE", "anywhere": "WORLDWIDE", "global": "WORLDWIDE",
}


def parse_location(raw: str | None) -> dict:
    """Return {countries: [ISO...], city, remote_type, region}. Conflicting signals are all reported."""
    out = {"countries": [], "city": None, "remote_type": "unknown", "region": None}
    if not raw:
        return out
    s = raw.strip()
    low = s.lower()
    if re.search(r"\bremote\b|work from home|wfh|anywhere", low):
        out["remote_type"] = "remote"
    if re.search(r"\bhybrid\b", low):
        out["remote_type"] = "hybrid"
    if re.search(r"\bon-?site\b|in[- ]office", low):
        out["remote_type"] = "onsite"
    for word, region in REGION_WORDS.items():
        if re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", low):
            out["region"] = region
    found = []
    for code, rx in _ALIAS_RE:
        m = rx.search(s)
        if m:
            found.append((m.start(), code, m.group(1)))
    found.sort()
    out["countries"] = list(dict.fromkeys(c for _, c, _ in found))
    if found:
        first_alias = found[0][2].lower()
        if first_alias not in ("ireland", "united kingdom", "germany", "netherlands") and \
                first_alias not in (a for a in [c.lower() for c in ["uk", "usa", "us"]]):
            out["city"] = found[0][2].title()
    return out


def job_identity(employer_norm: str, title_norm: str, requisition_id: str | None, country: str | None,
                 city: str | None) -> str:
    """Stable identity for 'the same vacancy'. Requisition IDs are strongest; otherwise employer + title +
    location. URL is deliberately NOT part of the identity (reposts/cross-posts change URLs)."""
    if requisition_id:
        basis = f"req|{employer_norm}|{re.sub(r'[^a-z0-9]', '', requisition_id.lower())}"
    else:
        basis = f"t|{employer_norm}|{title_norm}|{(country or '?').upper()}|{(city or '').lower()}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def description_hash(text: str) -> str:
    norm = re.sub(r"\s+", " ", (text or "").lower()).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:32]


def html_to_text(html: str | None) -> str:
    """Safe, dependency-free HTML -> text. Output is plain text; it is never rendered as HTML."""
    if not html:
        return ""
    import html as htmllib
    s = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h[1-6]>|</div>", "\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "\n• ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = htmllib.unescape(htmllib.unescape(s))  # some APIs double-encode
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip()
