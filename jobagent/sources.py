"""Job source adapters. Only official/public job-board APIs intended for programmatic access are used:
Greenhouse Job Board API, Lever Postings API, Ashby Posting API and the Arbeitnow public API.
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


def build_sources(http: HttpClient, conf: dict) -> list[tuple[Source, str | None]]:
    """Expand settings['sources'] into (adapter, board) work items."""
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
    return items
