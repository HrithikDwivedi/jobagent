"""Job source adapters. Only official/public job-board APIs intended for programmatic access are used:
Greenhouse Job Board API, Lever Postings API, Ashby Posting API, the Arbeitnow and Jobicy public APIs and (with
a free key) the Adzuna API.
No HTML scraping, no logins, no CAPTCHA handling."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .http import FetchError, HttpClient
from .normalize import html_to_text


@dataclass
class RawJob:
    source: str
    source_job_id: str
    title: str
    company: str
    location_raw: str | None
    url: str | None
    apply_url: str | None
    description_text: str
    published_at: str | None = None
    employment_type: str | None = None
    remote_hint: str | None = None      # remote|hybrid|onsite from structured fields
    requisition_id: str | None = None
    country_hint: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_period: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class SourceResult:
    source: str
    board: str | None
    jobs: list[RawJob]
    complete: bool                       # True only if every page was fetched (safe to infer closures)
    error: str | None = None


def _ts(ms_or_s) -> str | None:
    try:
        v = float(ms_or_s)
    except (TypeError, ValueError):
        return None
    if v > 1e11:
        v /= 1000.0
    return datetime.fromtimestamp(v, tz=timezone.utc).isoformat(timespec="seconds")


def _iso(s: str | None) -> str | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return None


class Source:
    name = "base"

    def __init__(self, http: HttpClient):
        self.http = http

    def fetch(self, board: str | None = None) -> SourceResult:  # pragma: no cover
        raise NotImplementedError

    def verify(self, source_job_id: str, board: str | None) -> bool | None:
        """True = still open, False = gone, None = cannot tell."""
        return None


class GreenhouseSource(Source):
    name = "greenhouse"
    BASE = "https://boards-api.greenhouse.io/v1/boards"

    def fetch(self, board: str | None = None) -> SourceResult:
        try:
            meta = self.http.get_json(f"{self.BASE}/{board}")
            company = (meta or {}).get("name") or board
        except FetchError:
            company = board
        try:
            data = self.http.get_json(f"{self.BASE}/{board}/jobs", {"content": "true"})
        except FetchError as e:
            return SourceResult(self.name, board, [], False, str(e))
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            return SourceResult(self.name, board, [], False, "Malformed Greenhouse response")
        jobs = []
        for j in data["jobs"]:
            if not isinstance(j, dict) or not j.get("id") or not j.get("title"):
                continue
            loc = (j.get("location") or {}).get("name") if isinstance(j.get("location"), dict) else None
            offices = [o.get("location") or o.get("name") for o in j.get("offices") or [] if isinstance(o, dict)]
            jobs.append(RawJob(
                source=self.name, source_job_id=f"{board}:{j['id']}", title=str(j["title"]).strip(),
                company=company, location_raw=" / ".join(x for x in [loc] + offices if x) or loc,
                url=j.get("absolute_url"), apply_url=j.get("absolute_url"),
                description_text=html_to_text(j.get("content")),
                published_at=_iso(j.get("first_published") or j.get("updated_at")),
                requisition_id=str(j["requisition_id"]) if j.get("requisition_id") else None,
                extra={"board": board, "internal_job_id": j.get("internal_job_id")},
            ))
        return SourceResult(self.name, board, jobs, True)

    def verify(self, source_job_id: str, board: str | None) -> bool | None:
        board, _, jid = source_job_id.partition(":")
        try:
            self.http.get(f"{self.BASE}/{board}/jobs/{jid}")
            return True
        except FetchError as e:
            return False if e.status == 404 else None


class LeverSource(Source):
    name = "lever"
    BASE = "https://api.lever.co/v0/postings"

    def fetch(self, board: str | None = None) -> SourceResult:
        try:
            data = self.http.get_json(f"{self.BASE}/{board}", {"mode": "json"})
        except FetchError as e:
            return SourceResult(self.name, board, [], False, str(e))
        if not isinstance(data, list):
            return SourceResult(self.name, board, [], False, "Malformed Lever response")
        jobs = []
        for j in data:
            if not isinstance(j, dict) or not j.get("id") or not j.get("text"):
                continue
            cats = j.get("categories") or {}
            locs = cats.get("allLocations") or ([cats.get("location")] if cats.get("location") else [])
            lists = "\n".join(f"{l.get('text', '')}\n{html_to_text(l.get('content'))}" for l in j.get("lists") or []
                              if isinstance(l, dict))
            desc = "\n".join(x for x in [j.get("descriptionPlain"), lists, j.get("additionalPlain")] if x)
            sal = j.get("salaryRange") or {}
            wt = (j.get("workplaceType") or "").lower()
            jobs.append(RawJob(
                source=self.name, source_job_id=f"{board}:{j['id']}", title=str(j["text"]).strip(),
                company=board.replace("-", " ").title(), location_raw=" / ".join(locs) or None,
                url=j.get("hostedUrl"), apply_url=j.get("applyUrl") or j.get("hostedUrl"),
                description_text=desc, published_at=_ts(j.get("createdAt")),
                employment_type=cats.get("commitment"),
                remote_hint={"remote": "remote", "hybrid": "hybrid", "on-site": "onsite", "onsite": "onsite"}.get(wt),
                country_hint=(j.get("country") or None),
                salary_min=sal.get("min"), salary_max=sal.get("max"), salary_currency=sal.get("currency"),
                salary_period={"per-year-salary": "year", "per-month-salary": "month",
                               "per-hour-wage": "hour"}.get(sal.get("interval")),
                extra={"board": board, "team": cats.get("team")},
            ))
        return SourceResult(self.name, board, jobs, True)

    def verify(self, source_job_id: str, board: str | None) -> bool | None:
        board, _, jid = source_job_id.partition(":")
        try:
            self.http.get(f"{self.BASE}/{board}/{jid}")
            return True
        except FetchError as e:
            return False if e.status == 404 else None


class AshbySource(Source):
    name = "ashby"
    BASE = "https://api.ashbyhq.com/posting-api/job-board"

    def fetch(self, board: str | None = None) -> SourceResult:
        try:
            data = self.http.get_json(f"{self.BASE}/{board}", {"includeCompensation": "true"})
        except FetchError as e:
            return SourceResult(self.name, board, [], False, str(e))
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            return SourceResult(self.name, board, [], False, "Malformed Ashby response")
        jobs = []
        for j in data["jobs"]:
            if not isinstance(j, dict) or not j.get("id") or not j.get("title"):
                continue
            if j.get("isListed") is False:
                continue
            locs = [j.get("location")] + [s.get("location") for s in j.get("secondaryLocations") or []
                                          if isinstance(s, dict)]
            country = (((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry"))
            smin = smax = cur = period = None
            for comp in ((j.get("compensation") or {}).get("summaryComponents") or []):
                if isinstance(comp, dict) and comp.get("compensationType") == "Salary":
                    smin, smax = comp.get("minValue"), comp.get("maxValue")
                    cur = comp.get("currencyCode")
                    period = {"1 YEAR": "year", "1 MONTH": "month", "1 HOUR": "hour"}.get(comp.get("interval"))
                    break
            wt = (j.get("workplaceType") or "").lower()
            remote = "remote" if j.get("isRemote") or wt == "remote" else {"hybrid": "hybrid", "onsite": "onsite"}.get(wt)
            jobs.append(RawJob(
                source=self.name, source_job_id=f"{board}:{j['id']}", title=str(j["title"]).strip(),
                company=board.replace("-", " ").title(), location_raw=" / ".join(l for l in locs if l) or None,
                url=j.get("jobUrl"), apply_url=j.get("applyUrl") or j.get("jobUrl"),
                description_text=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
                published_at=_iso(j.get("publishedAt")), employment_type=j.get("employmentType"),
                remote_hint=remote, country_hint=country,
                salary_min=smin, salary_max=smax, salary_currency=cur, salary_period=period,
                extra={"board": board, "department": j.get("department")},
            ))
        return SourceResult(self.name, board, jobs, True)


class ArbeitnowSource(Source):
    name = "arbeitnow"
    URL = "https://www.arbeitnow.com/api/job-board-api"

    def __init__(self, http: HttpClient, max_pages: int = 3):
        super().__init__(http)
        self.max_pages = max_pages

    def fetch(self, board: str | None = None) -> SourceResult:
        jobs, complete, error = [], True, None
        url, page = self.URL, 0
        while url and page < self.max_pages:
            try:
                data = self.http.get_json(url)
            except FetchError as e:
                complete, error = False, str(e)
                break
            if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                complete, error = False, "Malformed Arbeitnow response"
                break
            for j in data["data"]:
                if not isinstance(j, dict) or not j.get("slug") or not j.get("title"):
                    continue
                jobs.append(RawJob(
                    source=self.name, source_job_id=str(j["slug"]), title=str(j["title"]).strip(),
                    company=str(j.get("company_name") or "Unknown"), location_raw=j.get("location"),
                    url=j.get("url"), apply_url=j.get("url"),
                    description_text=html_to_text(j.get("description")),
                    published_at=_ts(j.get("created_at")),
                    employment_type=", ".join(j.get("job_types") or []) or None,
                    remote_hint="remote" if j.get("remote") else None,
                    extra={"tags": j.get("tags") or []},
                ))
            page += 1
            url = ((data.get("links") or {}).get("next"))
        if url and page >= self.max_pages:
            complete = False  # more pages exist; don't infer closures from a partial view
        return SourceResult(self.name, None, jobs, complete, error)


class JobicySource(Source):
    """Jobicy public remote-jobs API (every industry). Terms: credit Jobicy and send applicants to the original job
    URL (we always link to it). Max 1 request per second per IP, so requests are spaced out."""
    name = "jobicy"
    URL = "https://jobicy.com/api/v2/remote-jobs"

    def fetch(self, board: str | None = None) -> SourceResult:
        params = {"count": "50"}
        if board:
            params["industry"] = board
        self.http.sleep(1.1)                       # Jobicy allows one request per second
        try:
            data = self.http.get_json(self.URL, params=params)
        except FetchError as e:
            return SourceResult(self.name, board, [], False, str(e))
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            return SourceResult(self.name, board, [], False, "Malformed Jobicy response")
        jobs = []
        for j in data["jobs"]:
            if not isinstance(j, dict) or not j.get("id") or not j.get("jobTitle"):
                continue
            inds = j.get("jobIndustry") or []
            if isinstance(inds, str):
                inds = [inds]
            geo = str(j.get("jobGeo") or "").strip()
            smin, smax = j.get("annualSalaryMin"), j.get("annualSalaryMax")
            types = j.get("jobType") or []
            cur = j.get("salaryCurrency")
            jobs.append(RawJob(
                source=self.name, source_job_id=str(j["id"]), title=html_to_text(str(j["jobTitle"])).strip()[:300],
                company=str(j.get("companyName") or "Unknown"),
                location_raw=("Remote" + (f" - {geo}" if geo and geo.lower() != "anywhere" else "")),
                url=j.get("url"), apply_url=j.get("url"),
                description_text=html_to_text(j.get("jobDescription") or j.get("jobExcerpt") or ""),
                published_at=_iso(str(j.get("pubDate") or "").replace(" ", "T") or None),
                employment_type=(", ".join(str(t) for t in types) if isinstance(types, list) else str(types)) or None,
                remote_hint="remote",
                salary_min=float(smin) if isinstance(smin, (int, float)) else None,
                salary_max=float(smax) if isinstance(smax, (int, float)) else None,
                salary_currency=str(cur)[:3] if cur else None,
                salary_period="year" if smax else None,
                extra={"industries": [str(x) for x in inds][:5], "level": j.get("jobLevel"), "via": "Jobicy"},
            ))
        # one page per industry (newest 50): never a complete view of the board, so closures are not inferred
        return SourceResult(self.name, board, jobs, False)


ADZUNA_COUNTRIES = {"GB": "gb", "US": "us", "AT": "at", "AU": "au", "BE": "be", "BR": "br", "CA": "ca", "CH": "ch",
                    "DE": "de", "ES": "es", "FR": "fr", "IN": "in", "IT": "it", "MX": "mx", "NL": "nl", "NZ": "nz",
                    "PL": "pl", "SG": "sg", "ZA": "za"}


class AdzunaSource(Source):
    """Adzuna official jobs API (every industry; needs a free app id/key). `board` = "<country>|<what>"."""
    name = "adzuna"
    URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"

    def __init__(self, http: HttpClient, app_id: str, app_key: str, max_pages: int = 1):
        super().__init__(http)
        self.app_id, self.app_key = app_id, app_key
        self.max_pages = max(1, min(int(max_pages or 1), 5))

    def fetch(self, board: str | None = None) -> SourceResult:
        country, _, what = (board or "").partition("|")
        if country not in ADZUNA_COUNTRIES.values() or not what:
            return SourceResult(self.name, board, [], False, "invalid Adzuna query")
        jobs, error = [], None
        for page in range(1, self.max_pages + 1):
            try:
                data = self.http.get_json(self.URL.format(country=country, page=page), params={
                    "app_id": self.app_id, "app_key": self.app_key, "results_per_page": "50", "what": what,
                    "max_days_old": "30", "content-type": "application/json"})
            except FetchError as e:
                error = str(e).replace(self.app_key, "***").replace(self.app_id, "***")
                break
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                error = "Malformed Adzuna response"
                break
            for j in data["results"]:
                if not isinstance(j, dict) or not j.get("id") or not j.get("title"):
                    continue
                loc = j.get("location") if isinstance(j.get("location"), dict) else {}
                area = loc.get("area") if isinstance(loc.get("area"), list) else []
                cat = (j.get("category") or {}).get("label") if isinstance(j.get("category"), dict) else None
                company = j.get("company") if isinstance(j.get("company"), dict) else {}
                ctime, ctype = j.get("contract_time"), j.get("contract_type")
                jobs.append(RawJob(
                    source=self.name, source_job_id=str(j["id"]), title=html_to_text(str(j["title"])).strip()[:300],
                    company=str(company.get("display_name") or "Unknown"),
                    location_raw=", ".join(reversed([str(a) for a in area[1:]])) or loc.get("display_name"),
                    url=j.get("redirect_url"), apply_url=j.get("redirect_url"),
                    description_text=html_to_text(j.get("description") or ""),
                    published_at=_iso(j.get("created")),
                    employment_type={"full_time": "full_time", "part_time": "part_time"}.get(ctime)
                    or ("contract" if ctype == "contract" else None),
                    country_hint=next((k for k, v in ADZUNA_COUNTRIES.items() if v == country), None),
                    salary_min=float(j["salary_min"]) if isinstance(j.get("salary_min"), (int, float)) else None,
                    salary_max=float(j["salary_max"]) if isinstance(j.get("salary_max"), (int, float)) else None,
                    salary_period="year" if j.get("salary_max") else None,
                    extra={"category": cat, "via": "Adzuna"},
                ))
            if len(data["results"]) < 50:
                break
        return SourceResult(self.name, board, jobs, False, error)


def build_sources(http: HttpClient, conf: dict, scope: dict | None = None, countries: list[str] | None = None,
                  adzuna_keys: tuple = (None, None), max_queries: int = 40) -> list[tuple[Source, str | None]]:
    """Expand settings['sources'] into (adapter, board) work items. `scope` (discovery_settings career_scope) adds
    every-industry sources for what users chose: one Jobicy feed per industry and one Adzuna search per
    (role, target country), capped at `max_queries`."""
    items: list[tuple[Source, str | None]] = []
    if conf.get("greenhouse", {}).get("enabled"):
        gh = GreenhouseSource(http)
        items += [(gh, b) for b in conf["greenhouse"].get("boards", [])]
    if conf.get("lever", {}).get("enabled"):
        lv = LeverSource(http)
        items += [(lv, b) for b in conf["lever"].get("sites", [])]
    if conf.get("ashby", {}).get("enabled"):
        ab = AshbySource(http)
        items += [(ab, b) for b in conf["ashby"].get("boards", [])]
    if conf.get("arbeitnow", {}).get("enabled"):
        items.append((ArbeitnowSource(http, conf["arbeitnow"].get("max_pages", 3)), None))
    items += targeted_sources(http, conf, scope, countries, adzuna_keys, max_queries)
    return items


def targeted_sources(http: HttpClient, conf: dict, scope: dict | None, countries: list[str] | None,
                     adzuna_keys: tuple = (None, None), max_queries: int = 40) -> list[tuple[Source, str | None]]:
    """Only the every-industry, query-based sources for a scope (used for a new user's first search too)."""
    if not scope or not (scope.get("industries") or scope.get("custom")):
        return []
    from . import taxonomy
    items: list[tuple[Source, str | None]] = []
    if conf.get("jobicy", {"enabled": True}).get("enabled", True):
        jb = JobicySource(http)
        slugs: list[str] = []
        for ind in scope.get("industries") or []:
            for slug in (taxonomy.BY_ID[ind].jobicy if ind in taxonomy.BY_ID else ()):
                if slug not in slugs:
                    slugs.append(slug)
        items += [(jb, s) for s in slugs[:12]]
    app_id, app_key = adzuna_keys
    if app_id and app_key and conf.get("adzuna", {"enabled": True}).get("enabled", True):
        az = AdzunaSource(http, app_id, app_key, conf.get("adzuna", {}).get("max_pages", 1))
        codes = [ADZUNA_COUNTRIES[c] for c in (countries or []) if c in ADZUNA_COUNTRIES]
        qs: list[str] = []
        for _ind, q in scope.get("queries") or []:
            for cc in codes:
                b = f"{cc}|{q}"
                if b not in qs:
                    qs.append(b)
        items += [(az, b) for b in qs[:max_queries]]
    return items
