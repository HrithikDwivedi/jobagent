"""Currency conversion for salaries: the user saves their expected salary once, in their own currency, and every
application form gets it in the job's currency (and per month / per hour when the form asks that way).

Rates are the European Central Bank's official daily reference rates (free, no key), cached in the `settings`
table for a day. If the ECB cannot be reached, the last cached rates are used, then the built-in snapshot below.
The UAE dirham is pegged to the US dollar (3.6725)."""
from __future__ import annotations

import re
from datetime import timedelta

from .db import Database, dumps, iso, loads, parse_iso, utcnow

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
CACHE_KEY = "_fx_rates"
# EUR -> currency, ECB reference rates of 2026-11-18 (fallback only)
SNAPSHOT = {"date": "2026-11-18", "rates": {
    "EUR": 1.0, "USD": 1.0968, "JPY": 174.85, "CZK": 24.386, "DKK": 7.4744, "GBP": 0.8329, "HUF": 368.40,
    "PLN": 4.4118, "RON": 5.3467, "SEK": 11.143, "CHF": 0.9243, "NOK": 10.6045, "AUD": 1.6294, "CAD": 1.569,
    "INR": 102.4905, "NZD": 1.9951, "SGD": 1.4144, "ZAR": 18.8897, "AED": round(1.0968 * 3.6725, 4)}}
SYMBOLS = {"GBP": "£", "EUR": "€", "USD": "$", "INR": "₹", "JPY": "¥", "AUD": "A$", "CAD": "C$", "SGD": "S$",
           "NZD": "NZ$", "CHF": "CHF ", "AED": "AED ", "ZAR": "R ", "SEK": "SEK ", "NOK": "NOK ", "DKK": "DKK ",
           "PLN": "PLN ", "CZK": "CZK "}
COUNTRY_CURRENCY = {
    "GB": "GBP", "IE": "EUR", "DE": "EUR", "NL": "EUR", "FR": "EUR", "ES": "EUR", "IT": "EUR", "PT": "EUR",
    "BE": "EUR", "LU": "EUR", "AT": "EUR", "FI": "EUR", "EE": "EUR", "LT": "EUR", "LV": "EUR", "GR": "EUR",
    "MT": "EUR", "CY": "EUR", "SE": "SEK", "DK": "DKK", "NO": "NOK", "CH": "CHF", "PL": "PLN", "CZ": "CZK",
    "HU": "HUF", "RO": "RON", "US": "USD", "CA": "CAD", "AU": "AUD", "NZ": "NZD", "IN": "INR", "AE": "AED",
    "SG": "SGD", "JP": "JPY", "ZA": "ZAR"}
PERIODS = {"year": 1.0, "month": 1 / 12, "week": 1 / 52, "day": 1 / 260, "hour": 1 / 2080}


def parse_amount(text) -> float | None:
    """'15,00,000' / '35.5k' / '£42,000' / '1.2 lakh' / '12 LPA' -> number (None if not a number)."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text) if text >= 0 else None
    t = str(text).strip().lower().replace(" ", " ")
    if not t:
        return None
    m = re.search(r"(\d[\d,. ]*)\s*(k|thousand|lakh|lakhs|lac|lpa|l\b|crore|cr|m|mn|million)?", t)
    if not m:
        return None
    num = m.group(1).replace(",", "").replace(" ", "")
    if num.count(".") > 1:
        num = num.replace(".", "")
    try:
        v = float(num)
    except ValueError:
        return None
    mult = {"k": 1e3, "thousand": 1e3, "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lpa": 1e5, "l": 1e5,
            "crore": 1e7, "cr": 1e7, "m": 1e6, "mn": 1e6, "million": 1e6}.get(m.group(2) or "", 1)
    return v * mult


def rates(db: Database | None = None, http=None) -> dict:
    """{'date': 'YYYY-MM-DD', 'rates': {CUR: per-EUR}}. Refreshes from the ECB at most once a day (when `http`
    is given); never raises."""
    cached = None
    if db is not None:
        row = db.one("SELECT value_json, updated_at FROM settings WHERE key=?", (CACHE_KEY,))
        if row:
            cached = loads(row["value_json"], None)
            fresh = (parse_iso(row["updated_at"]) or utcnow()) > utcnow() - timedelta(hours=20)
            if cached and fresh:
                return cached
    if http is not None:
        try:
            xml = http.get(ECB_URL).text
            found = dict(re.findall(r"currency='([A-Z]{3})'\s+rate='([0-9.]+)'", xml))
            day = (re.search(r"time='(\d{4}-\d{2}-\d{2})'", xml) or [None, None])[1]
            if found and "USD" in found and "GBP" in found:
                r = {k: float(v) for k, v in found.items()}
                r["EUR"] = 1.0
                r["AED"] = round(r["USD"] * 3.6725, 4)
                cached = {"date": day, "rates": r}
                if db is not None:
                    with db.transaction():
                        db.run("INSERT INTO settings(key, value_json, updated_at) VALUES (?,?,?) ON CONFLICT(key) "
                               "DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                               (CACHE_KEY, dumps(cached), iso()))
                return cached
        except Exception:
            pass
    return cached or SNAPSHOT


def convert(amount: float, frm: str, to: str, fx: dict | None = None) -> float | None:
    r = (fx or SNAPSHOT)["rates"]
    if frm == to:
        return float(amount)
    if frm not in r or to not in r:
        return None
    return float(amount) / r[frm] * r[to]


def nice_round(v: float) -> int:
    """Round like a person would type it: 41,873 -> 42,000; 3,489 -> 3,500; 18.7 -> 19."""
    if v >= 100_000:
        step = 5_000
    elif v >= 10_000:
        step = 1_000
    elif v >= 1_000:
        step = 100
    else:
        step = 1
    return int(round(v / step) * step)


def job_currency(job: dict | None, fallback: str | None = None) -> str | None:
    job = job or {}
    cur = (job.get("salary_currency") or "").upper()
    if cur in SNAPSHOT["rates"]:
        return cur
    return COUNTRY_CURRENCY.get((job.get("country") or "").upper()) or fallback


def expected_for_job(expected: dict | None, job: dict | None, fx: dict | None = None,
                     period: str = "year") -> dict | None:
    """The user's expected salary converted into the job's currency (and pay period).
    -> {'amount', 'currency', 'period', 'from_amount', 'from_currency', 'rate_date'} or None."""
    if not expected or not expected.get("amount"):
        return None
    frm = (expected.get("currency") or "EUR").upper()
    per = expected.get("period") or "year"
    yearly = float(expected["amount"]) / PERIODS.get(per, 1.0)
    to = job_currency(job, frm) or frm
    conv = convert(yearly, frm, to, fx)
    if conv is None:
        to, conv = frm, yearly
    conv *= PERIODS.get(period, 1.0)
    return {"amount": nice_round(conv), "currency": to, "period": period, "from_amount": expected["amount"],
            "from_currency": frm, "rate_date": (fx or SNAPSHOT).get("date")}


def fmt(amount: float, currency: str) -> str:
    sym = SYMBOLS.get(currency, currency + " ")
    return f"{sym}{int(round(amount)):,}"


def period_in(label: str) -> str:
    l = (label or "").lower()
    if re.search(r"\b(per|a|/)\s*(hour|hr)\b|\bhourly\b", l):
        return "hour"
    if re.search(r"\b(per|a|/)\s*(month|mo)\b|\bmonthly\b|\bper mensem\b", l):
        return "month"
    if re.search(r"\b(per|a|/)\s*day\b|\bdaily rate\b|\bday rate\b", l):
        return "day"
    return "year"


def currency_in(label: str) -> str | None:
    """A currency the form names in the question ('Expected salary (GBP)', '... in €')."""
    l = label or ""
    for cur in SNAPSHOT["rates"]:
        if re.search(rf"\b{cur}\b", l, re.I):
            return cur
    for cur, sym in (("GBP", "£"), ("EUR", "€"), ("INR", "₹"), ("JPY", "¥")):
        if sym in l:
            return cur
    if re.search(r"\blakh|\blpa\b|\bctc\b", l, re.I):
        return "INR"
    return None
