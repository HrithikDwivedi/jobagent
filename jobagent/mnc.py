"""Top-MNC catalogue and job-feed adapters for large employers' own career sites.

Every feed is a public JSON endpoint the company's own careers page uses. Before fetching, robots.txt is
checked at runtime (RFC 9309: a 4xx robots.txt means no restrictions). Companies without a permitted feed
are shown in the dashboard with a link to their official careers search instead."""
from __future__ import annotations

import json
import re
import urllib.robotparser
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from .http import FetchError, HttpClient
from .matching import ENGINEERING, NON_SOFTWARE_ENGINEERING
from .normalize import html_to_text, normalize_employer
from .sources import RawJob, Source, SourceResult

CATALOG_PATH = Path(__file__).parent / "data" / "mnc_companies.json"
ISO3 = {"IND": "IN", "USA": "US", "GBR": "GB", "IRL": "IE", "DEU": "DE", "NLD": "NL", "SWE": "SE", "DNK": "DK",
        "FIN": "FI", "AUT": "AT", "CHE": "CH", "BEL": "BE", "LUX": "LU", "ITA": "IT", "PRT": "PT", "ESP": "ES",
        "POL": "PL", "CZE": "CZ", "EST": "EE", "FRA": "FR", "NOR": "NO", "LTU": "LT", "CAN": "CA", "AUS": "AU",
        "ARE": "AE", "JPN": "JP", "SGP": "SG", "ROU": "RO", "HUN": "HU", "GRC": "GR", "ISR": "IL", "BRA": "BR",
        "MEX": "MX", "CHN": "CN", "PHL": "PH", "MYS": "MY", "ZAF": "ZA", "CRI": "CR"}


@lru_cache(maxsize=1)
def catalog() -> list[dict]:
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    return data["companies"]


def by_key(key: str) -> dict | None:
    return next((c for c in catalog() if c["key"] == key), None)


@lru_cache(maxsize=1)
def _alias_index() -> dict[str, dict]:
    idx = {}
    for c in catalog():
        for a in c["aliases"] + [c["name"]]:
            idx[normalize_employer(a)] = c
    return idx


def match_employer(name: str | None) -> dict | None:
    """Catalogue entry for an employer name (exact normalised alias, or alias as the leading words)."""
    if not name:
        return None
    norm = normalize_employer(name)
    idx = _alias_index()
    if norm in idx:
        return idx[norm]
    for alias, c in idx.items():
        if len(alias) >= 3 and (norm.startswith(alias + " ") or norm == alias):
            return c
    return None


NON_IC_ROLE = re.compile(r"\b(consultant|manager|director|sales|analyst|recruiter|partner|principal consultant|"
                         r"architect|head of|vice president|\bvp\b|executive|specialist)\b", re.I)
IC_ENGINEER = re.compile(r"\b(engineer|developer|programmer|sde|swe)\b", re.I)


def is_software_title(title: str) -> bool:
    """Individual-contributor software roles only (MNC feeds return many consulting/management titles)."""
    t = title or ""
    if not ENGINEERING.search(t) or NON_SOFTWARE_ENGINEERING.search(t):
        return False
    return bool(IC_ENGINEER.search(t)) or not NON_IC_ROLE.search(t)


class RobotsCache:
    def __init__(self, http: HttpClient, agent: str = "JobAgent"):
        self.http, self.agent, self._cache = http, agent, {}

    def allowed(self, url: str) -> bool:
        m = re.match(r"^(https?://[^/]+)", url)
        if not m:
            return False
        base = m.group(1)
        if base not in self._cache:
            rp = urllib.robotparser.RobotFileParser()
            try:
                resp = self.http._client.get(base + "/robots.txt", timeout=15)
                if resp.status_code >= 500:
                    rp.disallow_all = True        # server error: be conservative this run
                elif resp.status_code >= 400:
                    rp.allow_all = True           # RFC 9309: unavailable robots.txt -> no restrictions
                else:
                    rp.parse(resp.text.splitlines())
            except Exception:
                rp.disallow_all = True
            self._cache[base] = rp
        return self._cache[base].can_fetch(self.agent, url)


def _iso_date(s: str | None) -> str | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%B %d, %Y"):
        try:
            return datetime.strptime(re.sub(r"\s+", " ", s.strip()), fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return None


class MNCSource(Source):
    """Base for company feeds; `company` is the catalogue entry."""
    name = "mnc"

    def __init__(self, http: HttpClient, company: dict, robots: RobotsCache, max_pages: int = 2,
                 fetch_details: bool = True, max_details: int = 40):
        super().__init__(http)
        self.company, self.robots = company, robots
        self.max_pages, self.fetch_details, self.max_details = max_pages, fetch_details, max_details

    def _raw(self, **kw) -> RawJob:
        kw.setdefault("extra", {})["mnc_key"] = self.company["key"]
        # Prefix ids with the company key so closures can be inferred per company (see ingest.mark_closures).
        kw["source_job_id"] = f"{self.company['key']}:{kw['source_job_id']}"
        return RawJob(source=self.name, company=self.company["name"], **kw)

    def _guard(self, url: str) -> None:
        if not self.robots.allowed(url):
            raise FetchError(f"robots.txt disallows {url.split('?')[0]}", url=url)


class WorkdaySource(MNCSource):
    name = "workday"

    def fetch(self, board: str | None = None) -> SourceResult:
        f = self.company["feed"]
        base = f"https://{f['tenant']}.{f['wd']}.myworkdayjobs.com"
        api = f"{base}/wday/cxs/{f['tenant']}/{f['site']}"
        jobs, complete, error, details = [], True, None, 0
        try:
            self._guard(api + "/jobs")
            for page in range(self.max_pages):
                data = self.http.post_json(api + "/jobs", {"appliedFacets": {}, "limit": 20, "offset": page * 20,
                                                           "searchText": f.get("query", "software engineer")})
                posts = data.get("jobPostings") or []
                for p in posts:
                    title = (p.get("title") or "").strip()
                    if not title or not is_software_title(title) or not p.get("externalPath"):
                        continue
                    req = next((b for b in p.get("bulletFields") or [] if re.match(r"^[A-Z0-9_-]{4,}$", str(b))), None)
                    desc, loc, country, url, posted, ttype, remote = "", p.get("locationsText"), None, None, None, None, None
                    if self.fetch_details and details < self.max_details:
                        try:
                            info = (self.http.get_json(api + p["externalPath"]) or {}).get("jobPostingInfo") or {}
                            details += 1
                            desc = html_to_text(info.get("jobDescription"))
                            loc = info.get("location") or loc
                            country = (info.get("country") or {}).get("descriptor")
                            url = info.get("externalUrl")
                            posted = _iso_date(info.get("startDate"))
                            ttype = info.get("timeType")
                            remote = (info.get("remoteType") or "").lower() or None
                            req = info.get("jobReqId") or req
                        except FetchError:
                            pass
                    url = url or f"{base}/en-US/{f['site']}{p['externalPath']}"
                    jobs.append(self._raw(
                        source_job_id=str(req or p["externalPath"]), title=title,
                        location_raw=", ".join(x for x in [loc, country] if x) or None, url=url, apply_url=url,
                        description_text=desc, published_at=posted, employment_type=ttype,
                        remote_hint={"remote": "remote", "hybrid": "hybrid", "on-site": "onsite"}.get(remote or ""),
                        requisition_id=req, country_hint=country))
                if len(posts) < 20:
                    break
            else:
                complete = False
        except FetchError as e:
            complete, error = False, str(e)
        return SourceResult(self.name, self.company["key"], jobs, complete, error)


class AmazonSource(MNCSource):
    name = "amazon"
    URL = "https://www.amazon.jobs/en/search.json"

    def fetch(self, board: str | None = None, countries: list[str] | None = None) -> SourceResult:
        q = self.company["feed"].get("query", "software development engineer")
        iso3 = {v: k for k, v in ISO3.items()}
        jobs, complete, error = [], True, None
        try:
            self._guard(self.URL)
            params = [("base_query", q), ("result_limit", "50"), ("sort", "recent")]
            for c in (countries or []):
                if c in iso3:
                    params.append(("normalized_country_code[]", iso3[c]))
            for page in range(self.max_pages):
                data = self.http.get_json(self.URL, params=params + [("offset", str(page * 50))])
                items = data.get("jobs") or []
                for j in items:
                    title = (j.get("title") or "").strip()
                    if not title or not is_software_title(title):
                        continue
                    url = "https://www.amazon.jobs" + (j.get("job_path") or "")
                    desc = "\n".join(x for x in [
                        html_to_text(j.get("description")),
                        "Basic qualifications:\n" + html_to_text(j.get("basic_qualifications")) if j.get("basic_qualifications") else "",
                        "Preferred qualifications:\n" + html_to_text(j.get("preferred_qualifications")) if j.get("preferred_qualifications") else ""] if x)
                    jobs.append(self._raw(
                        source_job_id=str(j.get("id_icims") or j.get("id")), title=title,
                        location_raw=j.get("normalized_location") or j.get("location"), url=url, apply_url=url,
                        description_text=desc, published_at=_iso_date(j.get("posted_date")),
                        employment_type=j.get("job_schedule_type"), requisition_id=str(j.get("id_icims") or "") or None,
                        country_hint=ISO3.get(j.get("country_code") or "")))
                if len(items) < 50:
                    break
            else:
                complete = False
        except FetchError as e:
            complete, error = False, str(e)
        return SourceResult(self.name, self.company["key"], jobs, complete, error)


class AtlassianSource(MNCSource):
    name = "atlassian"
    URL = "https://www.atlassian.com/endpoint/careers/listings"

    def fetch(self, board: str | None = None) -> SourceResult:
        try:
            self._guard(self.URL)
            data = self.http.get_json(self.URL)
        except FetchError as e:
            return SourceResult(self.name, self.company["key"], [], False, str(e))
        if not isinstance(data, list):
            return SourceResult(self.name, self.company["key"], [], False, "Malformed Atlassian response")
        jobs = []
        for j in data:
            title = (j.get("title") or "").strip()
            if not title or not is_software_title(title):
                continue
            url = f"https://www.atlassian.com/company/careers/details/{j.get('id')}"
            desc = "\n".join(html_to_text(j.get(k)) for k in ("overview", "responsibilities", "qualifications") if j.get(k))
            locs = [l if isinstance(l, str) else (l or {}).get("name", "") for l in j.get("locations") or []]
            jobs.append(self._raw(source_job_id=str(j.get("id")), title=title, location_raw=" / ".join(x for x in locs if x) or None,
                                  url=url, apply_url=url, description_text=desc, requisition_id=str(j.get("id"))))
        return SourceResult(self.name, self.company["key"], jobs, True)


class CapgeminiSource(MNCSource):
    name = "capgemini"
    URL = "https://cg-jobstream-api.azurewebsites.net/api/job-search"

    def fetch(self, board: str | None = None) -> SourceResult:
        q = self.company["feed"].get("query", "software engineer")
        jobs, complete, error = [], True, None
        try:
            self._guard(self.URL)
            for page in range(1, self.max_pages + 1):
                data = self.http.get_json(self.URL, params={"page": page, "size": 50, "search": q})
                items = data.get("data") or []
                for j in items:
                    title = (j.get("title") or "").strip()
                    if not title or not is_software_title(title):
                        continue
                    cc = (j.get("country_code") or "").split("-")[-1].upper() or None
                    url = j.get("apply_job_url")
                    jobs.append(self._raw(source_job_id=str(j.get("id")), title=title,
                                          location_raw=", ".join(x for x in [j.get("location"), cc] if x) or None,
                                          url=url, apply_url=url, description_text=j.get("description_stripped") or
                                          html_to_text(j.get("description")), employment_type=j.get("contract_type"),
                                          published_at=j.get("updated_at"), requisition_id=j.get("ref"),
                                          country_hint=cc if cc and len(cc) == 2 else None))
                if len(items) < 50:
                    break
            else:
                complete = False
        except FetchError as e:
            complete, error = False, str(e)
        return SourceResult(self.name, self.company["key"], jobs, complete, error)


class IBMSource(MNCSource):
    name = "ibm"
    URL = "https://www-api.ibm.com/search/api/v2"

    def fetch(self, board: str | None = None) -> SourceResult:
        q = self.company["feed"].get("query", "software engineer")
        jobs, complete, error = [], True, None
        try:
            self._guard(self.URL)
            for page in range(self.max_pages):
                data = self.http.post_json(self.URL, {
                    "appId": "careers", "scopes": ["careers2"], "size": 50, "from": page * 50, "lang": "zz",
                    "localeSelector": {}, "sm": {"query": q, "lang": "zz"},
                    "query": {"bool": {"must": [{"simple_query_string": {"query": q, "fields": ["title^3", "description^2", "body"]}}]}},
                    "_source": ["_id", "title", "url", "description", "field_keyword_08", "field_keyword_17",
                                "field_keyword_18", "field_keyword_19"]})
                hits = ((data.get("hits") or {}).get("hits")) or []
                for h in hits:
                    src = h.get("_source") or {}
                    title = (src.get("title") or "").strip()
                    if not title or not is_software_title(title):
                        continue
                    url = src.get("url")
                    jid = re.search(r"jobId=(\d+)", url or "")
                    jobs.append(self._raw(source_job_id=jid.group(1) if jid else h.get("_id"), title=title,
                                          location_raw=src.get("field_keyword_19") or None, url=url, apply_url=url,
                                          description_text=src.get("description") or "",
                                          requisition_id=jid.group(1) if jid else None))
                if len(hits) < 50:
                    break
            else:
                complete = False
        except FetchError as e:
            complete, error = False, str(e)
        return SourceResult(self.name, self.company["key"], jobs, complete, error)


class OracleHCMSource(MNCSource):
    name = "oracle_hcm"

    def fetch(self, board: str | None = None) -> SourceResult:
        f = self.company["feed"]
        api = f"https://{f['host']}/hcmRestApi/resources/latest"
        jobs, complete, error, details = [], True, None, 0
        try:
            self._guard(api + "/recruitingCEJobRequisitions")
            for page in range(self.max_pages):
                data = self.http.get_json(api + "/recruitingCEJobRequisitions", params={
                    "onlyData": "true", "expand": "requisitionList.secondaryLocations",
                    "finder": f"findReqs;siteNumber={f['site']},keyword={f.get('query', 'software engineer')},"
                              f"limit=25,offset={page * 25}"})
                items = ((data.get("items") or [{}])[0].get("requisitionList")) or []
                for j in items:
                    title = (j.get("Title") or "").strip()
                    if not title or not is_software_title(title):
                        continue
                    desc = ""
                    if self.fetch_details and details < self.max_details:
                        try:
                            d = self.http.get_json(api + "/recruitingCEJobRequisitionDetails", params={
                                "expand": "all", "onlyData": "true",
                                "finder": f'ById;Id="{j["Id"]}",siteNumber={f["site"]}'})
                            it = (d.get("items") or [{}])[0]
                            desc = "\n".join(html_to_text(it.get(k)) for k in ("ExternalDescriptionStr",
                                             "ExternalResponsibilitiesStr", "ExternalQualificationsStr") if it.get(k))
                            details += 1
                        except FetchError:
                            pass
                    url = f"https://{f['host']}/hcmUI/CandidateExperience/en/sites/{f['site']}/job/{j['Id']}"
                    jobs.append(self._raw(source_job_id=str(j["Id"]), title=title,
                                          location_raw=j.get("PrimaryLocation") or j.get("PrimaryLocationCountry"),
                                          url=url, apply_url=url, description_text=desc,
                                          published_at=_iso_date(j.get("PostedDate")), requisition_id=str(j["Id"]),
                                          country_hint=j.get("PrimaryLocationCountry"),
                                          remote_hint={"ORA_REMOTE": "remote", "ORA_HYBRID": "hybrid",
                                                       "ORA_ON_SITE": "onsite"}.get(j.get("WorkplaceTypeCode") or "")))
                if len(items) < 25:
                    break
            else:
                complete = False
        except FetchError as e:
            complete, error = False, str(e)
        return SourceResult(self.name, self.company["key"], jobs, complete, error)


ADAPTERS = {"workday": WorkdaySource, "amazon": AmazonSource, "atlassian": AtlassianSource,
            "capgemini": CapgeminiSource, "ibm": IBMSource, "oracle_hcm": OracleHCMSource}


class _CountryBound:
    """Wraps AmazonSource so build_sources' fetch(board) signature still works with country filters."""

    def __init__(self, src: AmazonSource, countries: list[str]):
        self.src, self.countries, self.name = src, countries, src.name

    def fetch(self, board=None):
        return self.src.fetch(board, countries=self.countries)


def build_mnc_sources(http: HttpClient, conf: dict, countries: list[str]) -> list[tuple]:
    if not conf.get("enabled", True):
        return []
    robots = RobotsCache(http)
    wanted = set(conf.get("companies") or [c["key"] for c in catalog()])
    items = []
    for c in catalog():
        feed = c.get("feed") or {}
        if c["key"] not in wanted or feed.get("type") not in ADAPTERS:
            continue
        src = ADAPTERS[feed["type"]](http, c, robots, max_pages=conf.get("max_pages", 2),
                                     fetch_details=conf.get("fetch_details", True))
        items.append((_CountryBound(src, countries) if isinstance(src, AmazonSource) else src, c["key"]))
    return items
