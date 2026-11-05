"""Polite HTTP client: per-host pacing, bounded retries with exponential backoff + jitter, Retry-After,
response size caps, and clear error classes. Only GET is used against job sources."""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

import httpx

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class FetchError(Exception):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False, url: str = ""):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.url = url


class RateLimited(FetchError):
    pass


@dataclass
class HttpClient:
    user_agent: str = "JobAgent/0.1 (personal job search tool)"
    timeout: float = 20.0
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    min_interval: float = 0.5           # seconds between requests to the same host
    max_bytes: int = 15 * 1024 * 1024
    transport: httpx.BaseTransport | None = None
    sleep: callable = time.sleep
    deadline: float | None = None       # absolute time.monotonic() budget for serverless runs
    _last: dict = field(default_factory=dict)
    stats: dict = field(default_factory=lambda: {"requests": 0, "retries": 0, "rate_limited": 0})

    def __post_init__(self):
        self._client = httpx.Client(timeout=self.timeout, follow_redirects=True, transport=self.transport,
                                    headers={"User-Agent": self.user_agent, "Accept": "application/json, */*"})

    def close(self):
        self._client.close()

    def _pace(self, host: str):
        last = self._last.get(host)
        if last is not None:
            wait = self.min_interval - (time.monotonic() - last)
            if wait > 0:
                self.sleep(wait)
        self._last[host] = time.monotonic()

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, self.max_delay)
        return min(self.max_delay, self.base_delay * (2 ** attempt)) * (0.5 + random.random() / 2)

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float | None:
        h = resp.headers.get("Retry-After")
        if not h:
            return None
        try:
            return max(0.0, float(h))
        except ValueError:
            try:
                return max(0.0, parsedate_to_datetime(h).timestamp() - time.time())
            except (TypeError, ValueError):
                return None

    def _out_of_time(self, extra: float = 0.0) -> bool:
        return self.deadline is not None and time.monotonic() + extra > self.deadline

    def get(self, url: str, params: dict | None = None) -> httpx.Response:
        return self._send("GET", url, params=params)

    def post_json(self, url: str, body: dict, params: dict | None = None):
        resp = self._send("POST", url, params=params, json_body=body)
        try:
            return resp.json()
        except ValueError:
            raise FetchError(f"Malformed JSON from {httpx.URL(url).host}", status=resp.status_code, url=url) from None

    def _send(self, method: str, url: str, params: dict | None = None, json_body: dict | None = None) -> httpx.Response:
        host = httpx.URL(url).host
        last_exc: FetchError | None = None
        for attempt in range(self.max_retries + 1):
            if self._out_of_time():
                raise FetchError("Run time budget exhausted", retryable=True, url=url)
            self._pace(host)
            self.stats["requests"] += 1
            try:
                resp = self._client.request(method, url, params=params, json=json_body)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
                last_exc = FetchError(f"{type(e).__name__} contacting {host}", retryable=True, url=url)
                retry_after = None
            else:
                if len(resp.content) > self.max_bytes:
                    raise FetchError(f"Response from {host} exceeds size limit", status=resp.status_code, url=url)
                if resp.status_code < 400:
                    return resp
                retry_after = self._retry_after(resp)
                if resp.status_code == 429:
                    self.stats["rate_limited"] += 1
                    last_exc = RateLimited(f"Rate limited by {host}", status=429, retryable=True, url=url)
                elif resp.status_code in RETRYABLE_STATUS:
                    last_exc = FetchError(f"HTTP {resp.status_code} from {host}", status=resp.status_code,
                                          retryable=True, url=url)
                else:
                    raise FetchError(f"HTTP {resp.status_code} from {host}", status=resp.status_code, url=url)
            if attempt == self.max_retries:
                break
            delay = self._backoff(attempt, retry_after)
            if retry_after is not None and retry_after > self.max_delay:
                break  # provider asked us to wait longer than we're willing to; give up this run
            if self._out_of_time(delay):
                break
            self.stats["retries"] += 1
            self.sleep(delay)
        raise last_exc or FetchError("Request failed", url=url)

    def get_json(self, url: str, params: dict | None = None):
        resp = self.get(url, params)
        try:
            return resp.json()
        except ValueError:
            raise FetchError(f"Malformed JSON from {httpx.URL(url).host}", status=resp.status_code, url=url) from None
