"""Small safety layer for Yahoo Finance/yfinance calls.

Yahoo Finance is not a stable public API and can return 429 responses when
scripts repeatedly touch heavy endpoints such as quoteSummary/info. This module
keeps those calls centralized, cached, and retryable.
"""

from __future__ import annotations

import hashlib
import json
import pickle
import re
import time
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar
from zoneinfo import ZoneInfo


T = TypeVar("T")
CACHE_DIR = Path(tempfile.gettempdir()) / "aime_business_charts_yahoo_cache"
DEFAULT_TTL_SECONDS = 6 * 60 * 60
INFO_RATE_LIMIT_MARKER = CACHE_DIR / "info_rate_limited.marker"
NASDAQ_INDEX_FALLBACKS = {
    "^GSPC": ("SPY", "etf", "SPY proxy for S&P 500 index"),
    "^IXIC": ("COMP", "index", "Nasdaq Composite index"),
    "^DJI": ("DIA", "etf", "DIA proxy for Dow Jones Industrial Average"),
}


def import_yfinance() -> Any:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("yfinance is required for this component") from exc
    return yf


def ticker(code: str) -> Any:
    return import_yfinance().Ticker(code)


def is_rate_limited(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return "ratelimit" in text or "rate limit" in text or "too many requests" in text or "429" in text


def yahoo_call(label: str, fn: Callable[[], T], retries: int = 3) -> T:
    delays = [2, 8, 20]
    errors: list[str] = []
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            if attempt >= retries or not is_rate_limited(exc):
                break
            time.sleep(delays[min(attempt, len(delays) - 1)])
    raise RuntimeError(f"Yahoo Finance request failed for {label}: {'; '.join(errors)}")


def _cache_path(key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{digest}.pickle"


def _store_cached_value(key: str, value: Any) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(key)
    tmp_path = path.with_suffix(".tmp")
    with tmp_path.open("wb") as handle:
        pickle.dump(value, handle)
    tmp_path.replace(path)


def cached_yahoo_call(
    label: str,
    key: str,
    fn: Callable[[], T],
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    retries: int = 3,
) -> T:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(key)
    now = time.time()
    if path.exists() and now - path.stat().st_mtime < ttl_seconds:
        with path.open("rb") as handle:
            return pickle.load(handle)
    try:
        result = yahoo_call(label, fn, retries=retries)
    except RuntimeError:
        if path.exists():
            with path.open("rb") as handle:
                return pickle.load(handle)
        raise
    _store_cached_value(key, result)
    return result


def ticker_info(code: str, ttl_seconds: int = DEFAULT_TTL_SECONDS, prefer_fallback: bool = False) -> dict[str, Any]:
    symbol = code.strip().upper()
    if prefer_fallback or _marker_active(INFO_RATE_LIMIT_MARKER, ttl_seconds=30 * 60):
        return cached_yahoo_call(
            f"{symbol} fallback info",
            f"fallback_info:{symbol}",
            lambda: _fallback_info(symbol),
            ttl_seconds,
        )
    try:
        return cached_yahoo_call(
            f"{symbol} info",
            f"info:{symbol}",
            lambda: dict(ticker(symbol).get_info()),
            ttl_seconds,
        )
    except RuntimeError as exc:
        if not is_rate_limited(exc):
            raise
        _touch_marker(INFO_RATE_LIMIT_MARKER)
        return cached_yahoo_call(
            f"{symbol} fallback info",
            f"fallback_info:{symbol}",
            lambda: _fallback_info(symbol),
            ttl_seconds,
        )


def ticker_fast_info(code: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> dict[str, Any]:
    symbol = code.strip().upper()
    return cached_yahoo_call(
        f"{symbol} fast_info",
        f"fast_info:{symbol}",
        lambda: dict(ticker(symbol).fast_info),
        ttl_seconds,
    )


def ticker_financials(code: str, quarterly: bool = False, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> Any:
    symbol = code.strip().upper()
    attr = "quarterly_financials" if quarterly else "financials"
    return cached_yahoo_call(
        f"{symbol} {attr}",
        f"{attr}:{symbol}",
        lambda: getattr(ticker(symbol), attr),
        ttl_seconds,
    )


def ticker_history(
    code: str,
    start: str,
    end: str,
    interval: str = "1d",
    auto_adjust: bool = False,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> Any:
    symbol = code.strip().upper()
    return cached_yahoo_call(
        f"{symbol} history",
        f"history:{symbol}:{start}:{end}:{interval}:{auto_adjust}",
        lambda: ticker(symbol).history(start=start, end=end, interval=interval, auto_adjust=auto_adjust),
        ttl_seconds,
    )


def _number(value: Any) -> float | None:
    text = str(value or "").strip().replace(",", "").replace("$", "")
    if not text or text in {"--", "N/A"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _nasdaq_chart_fallback(url: str, timeout: float) -> dict[str, Any] | None:
    """Return Yahoo-shaped daily data for supported US securities after Yahoo 429s."""
    parsed = urllib.parse.urlparse(url)
    marker = "/finance/chart/"
    if marker not in parsed.path:
        return None
    symbol = urllib.parse.unquote(parsed.path.split(marker, 1)[1]).strip().upper()
    query = urllib.parse.parse_qs(parsed.query)
    interval = str((query.get("interval") or ["1d"])[0])
    if interval != "1d":
        return None

    fallback = NASDAQ_INDEX_FALLBACKS.get(symbol)
    if fallback:
        provider_candidates = [fallback]
    elif re.fullmatch(r"[A-Z][A-Z0-9-]{0,9}", symbol):
        provider_candidates = [
            (symbol, "stocks", "US-listed stock daily history fallback"),
            (symbol, "etf", "US-listed ETF daily history fallback"),
        ]
    else:
        return None

    now = datetime.now(timezone.utc)
    max_rows: int | None = None
    if query.get("period1") and query.get("period2"):
        start = datetime.fromtimestamp(int(query["period1"][0]), tz=timezone.utc).date()
        end = datetime.fromtimestamp(int(query["period2"][0]), tz=timezone.utc).date()
    else:
        range_value = str((query.get("range") or ["1mo"])[0]).lower()
        range_days = {
            "1d": 5,
            "5d": 14,
            "1mo": 40,
            "3mo": 110,
            "6mo": 210,
            "ytd": 370,
            "1y": 370,
            "2y": 760,
            "5y": 1900,
            "10y": 3800,
        }.get(range_value)
        max_rows = {"1d": 2, "5d": 5, "1mo": 22, "3mo": 66, "6mo": 132}.get(range_value)
        if range_days is None:
            return None
        end = now.date()
        start = end - timedelta(days=range_days)

    rows: list[dict[str, Any]] = []
    provider_symbol = symbol
    asset_class = ""
    description = ""
    for candidate_symbol, candidate_asset_class, candidate_description in provider_candidates:
        params = urllib.parse.urlencode(
            {
                "assetclass": candidate_asset_class,
                "fromdate": start.isoformat(),
                "todate": end.isoformat(),
                "limit": "5000",
            }
        )
        target = f"https://api.nasdaq.com/api/quote/{candidate_symbol}/historical?{params}"
        request = urllib.request.Request(
            target,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://www.nasdaq.com",
                "Referer": "https://www.nasdaq.com/",
                "Connection": "close",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
            continue
        rows = ((((payload.get("data") or {}).get("tradesTable") or {}).get("rows")) or [])
        if rows:
            provider_symbol = candidate_symbol
            asset_class = candidate_asset_class
            description = candidate_description
            break
    if not rows:
        return None
    normalized = []
    for row in rows:
        try:
            trade_date = datetime.strptime(str(row.get("date") or ""), "%m/%d/%Y").replace(
                tzinfo=ZoneInfo("America/New_York")
            )
        except ValueError:
            continue
        close = _number(row.get("close"))
        if close is None:
            continue
        normalized.append(
            {
                "timestamp": int(trade_date.timestamp()),
                "open": _number(row.get("open")),
                "high": _number(row.get("high")),
                "low": _number(row.get("low")),
                "close": close,
                "volume": _number(row.get("volume")),
            }
        )
    normalized.sort(key=lambda item: item["timestamp"])
    if max_rows:
        normalized = normalized[-max_rows:]
    if not normalized:
        return None
    return {
        "chart": {
            "result": [
                {
                    "meta": {
                        "currency": "USD",
                        "symbol": symbol,
                        "exchangeName": "Nasdaq",
                        "exchangeTimezoneName": "America/New_York",
                        "regularMarketPrice": normalized[-1]["close"],
                        "_aimeSource": "Nasdaq historical API",
                        "_aimeMethod": description,
                        "_aimeProviderSymbol": provider_symbol,
                        "_aimeAdjustedCloseAvailable": False,
                    },
                    "timestamp": [item["timestamp"] for item in normalized],
                    "indicators": {
                        "quote": [{key: [item[key] for item in normalized] for key in ("open", "high", "low", "close", "volume")}],
                        "adjclose": [{"adjclose": [None for _item in normalized]}],
                    },
                }
            ],
            "error": None,
        }
    }


def yahoo_json(
    url: str,
    label: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    timeout: float = 20,
) -> dict[str, Any]:
    def fetch() -> dict[str, Any]:
        errors = []
        for attempt in range(3):
            rate_limited = False
            for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
                target = url.replace("query1.finance.yahoo.com", host).replace("query2.finance.yahoo.com", host)
                request = urllib.request.Request(
                    target,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36",
                        "Accept": "application/json,text/plain,*/*",
                        "Accept-Language": "en-US,en;q=0.9",
                        "Connection": "close",
                    },
                )
                try:
                    with urllib.request.urlopen(request, timeout=timeout) as response:
                        return json.loads(response.read().decode("utf-8"))
                except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
                    errors.append(f"attempt {attempt + 1} {host}: {exc}")
                    if is_rate_limited(exc):
                        # A 429 from one Yahoo edge must not prevent trying the
                        # alternate host.  The previous early raise retried
                        # query1 four times and never reached query2.
                        rate_limited = True
                    continue
            # Let cached_yahoo_call apply the longer 2/8/20 second backoff
            # after both Yahoo hosts reject the request.  Retaining the short
            # loop here is useful for non-rate-limit transport failures.
            if rate_limited:
                break
            if attempt < 2:
                time.sleep((0.6, 1.5)[attempt])
        raise RuntimeError("Yahoo Finance data request failed: " + "; ".join(errors))

    # Once both Yahoo hosts have returned 429, a long retry train rarely helps
    # and delays the agent's permitted provider fallback.  Try one backoff round
    # here; successful responses remain cached for six hours and stale cache is
    # still used by cached_yahoo_call when the upstream stays unavailable.
    cache_key = f"json:v3:{url}"
    try:
        return cached_yahoo_call(label, cache_key, fetch, ttl_seconds, retries=1)
    except RuntimeError as exc:
        if not is_rate_limited(exc):
            raise
        try:
            fallback = _nasdaq_chart_fallback(url, timeout)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, ValueError):
            raise exc
        if fallback is None:
            raise
        _store_cached_value(cache_key, fallback)
        return fallback


def _marker_active(path: Path, ttl_seconds: int) -> bool:
    return path.exists() and time.time() - path.stat().st_mtime < ttl_seconds


def _touch_marker(path: Path) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(str(int(time.time())), encoding="utf-8")


def _fetch_json_url(url: str, user_agent: str = "aime-chart yfinance-safe contact@example.com") -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.loads(response.read().decode("utf-8"))


def _raw_reported(item: dict[str, Any] | None) -> float | None:
    try:
        return float((item or {}).get("reportedValue", {}).get("raw"))
    except (TypeError, ValueError):
        return None


def _latest_series_value(payload: dict[str, Any], key: str) -> float | None:
    for series in payload.get("timeseries", {}).get("result", []):
        types = series.get("meta", {}).get("type", [])
        if types and types[0] == key:
            values = series.get(key, [])
            if values:
                return _raw_reported(values[-1])
    return None


def _ticker_to_cik(symbol: str) -> tuple[str | None, str | None]:
    try:
        tickers = _fetch_json_url("https://www.sec.gov/files/company_tickers.json")
    except Exception:
        return None, None
    match = next(
        (item for item in tickers.values() if str(item.get("ticker", "")).upper() == symbol.upper()),
        None,
    )
    if not match:
        return None, None
    return str(match["cik_str"]).zfill(10), str(match.get("title") or symbol)


def _sec_units(company_facts: dict[str, Any], concept: str) -> list[dict[str, Any]]:
    units = company_facts.get("facts", {}).get("us-gaap", {}).get(concept, {}).get("units", {})
    return units.get("USD") or units.get("shares") or next(iter(units.values()), [])


def _latest_instant_fact(company_facts: dict[str, Any], concepts: list[str]) -> float | None:
    candidates = []
    for concept in concepts:
        for fact in _sec_units(company_facts, concept):
            if fact.get("val") is None:
                continue
            if fact.get("start"):
                continue
            if fact.get("form") not in {"10-K", "10-Q"}:
                continue
            candidates.append(fact)
    if not candidates:
        return None
    candidates.sort(key=lambda item: (str(item.get("end") or ""), str(item.get("filed") or "")))
    try:
        return float(candidates[-1]["val"])
    except (TypeError, ValueError, KeyError):
        return None


def _latest_duration_fact(company_facts: dict[str, Any], concepts: list[str], min_days: int = 300, max_days: int = 400) -> tuple[float, str] | None:
    candidates = []
    for concept in concepts:
        for fact in _sec_units(company_facts, concept):
            try:
                duration = (date.fromisoformat(fact["end"]) - date.fromisoformat(fact["start"])).days
            except (KeyError, TypeError, ValueError):
                continue
            if fact.get("val") is None:
                continue
            if fact.get("form") != "10-K" or fact.get("fp") != "FY":
                continue
            if min_days <= duration <= max_days:
                candidates.append(fact)
    if not candidates:
        return None
    candidates.sort(key=lambda item: (str(item.get("end") or ""), str(item.get("filed") or "")))
    latest = candidates[-1]
    try:
        return float(latest["val"]), str(latest.get("end") or "")
    except (TypeError, ValueError, KeyError):
        return None


def sec_annual_revenue_income(symbol: str) -> dict[str, Any]:
    cik, company = _ticker_to_cik(symbol)
    if not cik:
        return {}
    try:
        facts = _fetch_json_url(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
    except Exception:
        return {}
    revenue = _latest_duration_fact(
        facts,
        [
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
        ],
    )
    income = _latest_duration_fact(facts, ["NetIncomeLoss", "ProfitLoss"])
    if not revenue or not income:
        return {}
    return {
        "totalRevenue": revenue[0],
        "netIncome": income[0],
        "fiscalDate": revenue[1] or income[1],
        "company": company or symbol,
        "source": "SEC Company Facts annual 10-K fallback",
    }


def _sec_balance_fields(symbol: str) -> dict[str, Any]:
    cik, company = _ticker_to_cik(symbol)
    if not cik:
        return {}
    try:
        facts = _fetch_json_url(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
    except Exception:
        return {"longName": company, "shortName": company}
    cash = _latest_instant_fact(
        facts,
        [
            "CashAndCashEquivalentsAtCarryingValue",
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        ],
    )
    debt_current = _latest_instant_fact(
        facts,
        [
            "ShortTermBorrowings",
            "LongTermDebtCurrent",
            "LongTermDebtAndFinanceLeaseObligationsCurrent",
        ],
    )
    debt_long = _latest_instant_fact(
        facts,
        [
            "LongTermDebtNoncurrent",
            "LongTermDebtAndFinanceLeaseObligationsNoncurrent",
        ],
    )
    shares = _latest_instant_fact(facts, ["EntityCommonStockSharesOutstanding"])
    output: dict[str, Any] = {"longName": company, "shortName": company}
    if cash is not None:
        output["totalCash"] = cash
    if debt_current is not None or debt_long is not None:
        output["totalDebt"] = float(debt_current or 0) + float(debt_long or 0)
    if shares is not None:
        output["sharesOutstanding"] = shares
    return output


def _fallback_info(symbol: str) -> dict[str, Any]:
    encoded = urllib.parse.quote(symbol)
    info: dict[str, Any] = {}
    chart = yahoo_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?range=5d&interval=1d", f"{symbol} chart")
    meta = chart.get("chart", {}).get("result", [{}])[0].get("meta", {})
    info.update(
        {
            "currency": meta.get("currency") or "USD",
            "shortName": meta.get("shortName") or symbol,
            "longName": meta.get("longName") or meta.get("shortName") or symbol,
            "regularMarketPrice": meta.get("regularMarketPrice"),
            "exchangeName": meta.get("exchangeName"),
        }
    )
    query = urllib.parse.urlencode(
        {
            "symbol": symbol,
            "type": "trailingMarketCap,trailingPeRatio,annualTotalRevenue",
            "period1": 1577836800,
            "period2": int(time.time()) + 86400,
        }
    )
    fundamentals = yahoo_json(
        f"https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/{encoded}?{query}",
        f"{symbol} fallback fundamentals",
    )
    market_cap = _latest_series_value(fundamentals, "trailingMarketCap")
    trailing_pe = _latest_series_value(fundamentals, "trailingPeRatio")
    annual_revenue = _latest_series_value(fundamentals, "annualTotalRevenue")
    if market_cap is not None:
        info["marketCap"] = market_cap
    if trailing_pe is not None:
        info["trailingPE"] = trailing_pe
    sec_fields = _sec_balance_fields(symbol)
    info.update({key: value for key, value in sec_fields.items() if value is not None})
    if "marketCap" not in info and info.get("regularMarketPrice") and info.get("sharesOutstanding"):
        info["marketCap"] = float(info["regularMarketPrice"]) * float(info["sharesOutstanding"])
    if "priceToSalesTrailing12Months" not in info and info.get("marketCap") and annual_revenue:
        info["priceToSalesTrailing12Months"] = float(info["marketCap"]) / annual_revenue
    if "enterpriseValue" not in info and info.get("marketCap") and "totalDebt" in info and "totalCash" in info:
        info["enterpriseValue"] = float(info["marketCap"]) + float(info["totalDebt"]) - float(info["totalCash"])
    info["source"] = "Yahoo chart/fundamentals + SEC fallback after yfinance info rate limit"
    return info
