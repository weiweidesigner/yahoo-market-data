#!/usr/bin/env python3
"""Yahoo Finance structured-data adapter for analysis-page-planner.

This tool only fetches and writes reusable JSON payloads. It never renders a
visible chart; chart rendering belongs to an explicitly selected downstream
component.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import io
import json
import math
import os
import re
import subprocess
import sys
import time
import tempfile
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from yfinance_safe import ticker_info, yahoo_json


YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_FUNDAMENTALS_URL = "https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/{symbol}"
FRED_GRAPH_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
USER_AGENT = "Mozilla/5.0 analysis-page-planner/1.0"
RANGE_ALIASES = {
    "daily": "1mo",
    "weekly": "6mo",
    "monthly": "1y",
    "quarterly": "5y",
    "annual": "10y",
    "yearly": "10y",
}
INTERVAL_BY_RANGE = {
    "1d": "5m",
    "5d": "15m",
    "1mo": "1d",
    "3mo": "1d",
    "6mo": "1d",
    "ytd": "1d",
    "1y": "1d",
    "2y": "1wk",
    "5y": "1wk",
    "10y": "1mo",
    "max": "1mo",
}
VALID_YAHOO_INTERVALS = {"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h", "4h", "1d", "5d", "1wk", "1mo", "3mo"}
FINANCIAL_METRIC_TYPES = {
    "netIncome": {"quarterly": "quarterlyNetIncome", "annual": "annualNetIncome"},
    "totalRevenue": {"quarterly": "quarterlyTotalRevenue", "annual": "annualTotalRevenue"},
    "operatingIncome": {"quarterly": "quarterlyOperatingIncome", "annual": "annualOperatingIncome"},
    "grossProfit": {"quarterly": "quarterlyGrossProfit", "annual": "annualGrossProfit"},
}
FINANCIAL_POSITION_FIELD_TYPES = {
    "totalAssets": {"quarterly": "quarterlyTotalAssets", "annual": "annualTotalAssets"},
    "totalLiabilities": {
        "quarterly": "quarterlyTotalLiabilitiesNetMinorityInterest",
        "annual": "annualTotalLiabilitiesNetMinorityInterest",
    },
    "stockholdersEquity": {"quarterly": "quarterlyStockholdersEquity", "annual": "annualStockholdersEquity"},
    "cashAndShortTermInvestments": {
        "quarterly": "quarterlyCashCashEquivalentsAndShortTermInvestments",
        "annual": "annualCashCashEquivalentsAndShortTermInvestments",
    },
    "totalDebt": {"quarterly": "quarterlyTotalDebt", "annual": "annualTotalDebt"},
}
FINANCIAL_POSITION_FIELD_LABELS = {
    "totalAssets": "Total assets",
    "totalLiabilities": "Total liabilities",
    "stockholdersEquity": "Stockholders' equity",
    "cashAndShortTermInvestments": "Cash & short-term investments",
    "totalDebt": "Total debt",
}
LATEST_FINANCIAL_INFO_FIELDS = {
    "netIncome": "netIncomeToCommon",
    "totalRevenue": "totalRevenue",
    "grossProfit": "grossProfits",
}
VALUATION_INFO_FIELDS = {
    "close": ("regularMarketPrice", "Closing price", "currency"),
    "marketCap": ("marketCap", "Market cap", "currency"),
    "enterpriseValue": ("enterpriseValue", "Enterprise value", "currency"),
    "trailingPE": ("trailingPE", "P/E (TTM)", "multiple"),
    "forwardPE": ("forwardPE", "Forward P/E", "multiple"),
    "priceToSales": ("priceToSalesTrailing12Months", "P/S (TTM)", "multiple"),
    "priceToBook": ("priceToBook", "P/B", "multiple"),
    "enterpriseToRevenue": ("enterpriseToRevenue", "EV/Revenue", "multiple"),
    "enterpriseToEbitda": ("enterpriseToEbitda", "EV/EBITDA", "multiple"),
}
INTERVAL_ALIASES = {
    "daily": "1d",
    "day": "1d",
    "weekly": "1wk",
    "week": "1wk",
    "monthly": "1mo",
    "month": "1mo",
    "quarterly": "3mo",
    "quarter": "3mo",
    "annual": "3mo",
    "yearly": "3mo",
    "year": "3mo",
    "1y": "3mo",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Yahoo Finance structured data as JSON (no chart rendering).")
    parser.add_argument("--code", "--symbol", dest="code", default="AAPL")
    parser.add_argument("--symbols", help="Comma separated symbols for comparison charts.")
    parser.add_argument("--title")
    parser.add_argument("--metric", default="close")
    parser.add_argument("--macro-indicators", help="Comma separated macro indicators, e.g. gdp,population,unemployment.")
    parser.add_argument("--range", dest="range_", default="1y")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--period-type", dest="period_type")
    parser.add_argument("--range-length", type=int, help="Maximum financial periods to return, e.g. 8 quarters.")
    parser.add_argument("--language", default="en")
    parser.add_argument("--interval")
    parser.add_argument(
        "--chart-type",
        choices=["line", "bar", "dynamic_line"],
        default="line",
        help="Backward-compatible payload-shape hint only; this tool does not render charts.",
    )
    parser.add_argument("--event-windows-json", help="JSON array of events: [{label,date}], used to compare post-event cumulative returns.")
    parser.add_argument("--window-days", type=int, default=100)
    parser.add_argument("--marker-metric", help="Row metric used to mark points on a line chart, e.g. return_pct.")
    parser.add_argument("--marker-threshold", type=float, help="Threshold for line-chart point markers.")
    parser.add_argument("--marker-direction", choices=["above", "below"], default="above")
    parser.add_argument("--marker-color", default="#ef4444")
    parser.add_argument("--style-json", help="Generic visual style JSON. Supports overlays such as thresholdMarkers.")
    parser.add_argument("--output", help="Deprecated JSON output alias. Prefer --payload-output.")
    parser.add_argument("--payload-output", help="Path for the structured JSON payload.")
    parser.add_argument("--timeout", type=float, default=18)
    args, _unknown = parser.parse_known_args()
    return args


def emit_payload_result(args: argparse.Namespace, payload: dict[str, Any]) -> None:
    """Persist structured data without creating an HTML/chart artifact."""
    target_value = args.payload_output or args.output
    target: Path | None = None
    if target_value:
        target = Path(target_value).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    result: dict[str, Any] = {
        "ok": True,
        "source": payload.get("source") or "Yahoo Finance",
        "rendered": False,
    }
    if target is not None:
        result["payloadOutput"] = str(target)
    else:
        result["payload"] = payload
    print(json.dumps(result, ensure_ascii=False))


def normalize_range(args: argparse.Namespace) -> str:
    raw = (args.range_ or args.period_type or "1y").strip()
    return RANGE_ALIASES.get(raw.lower(), raw)


def normalize_interval(range_value: str, explicit_interval: str | None) -> str:
    if explicit_interval:
        raw = explicit_interval.strip().lower()
        interval = INTERVAL_ALIASES.get(raw, raw)
        if interval in VALID_YAHOO_INTERVALS:
            return interval
    return INTERVAL_BY_RANGE.get(range_value, "1d")


def parse_event_windows(value: str | None) -> list[dict[str, str]]:
    if not value:
        return []
    try:
        raw = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    events: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("name") or "").strip()
        date = str(item.get("date") or item.get("start") or "").strip()
        if label and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            events.append({"label": label, "date": date})
    return events


def parse_macro_indicators(value: str | None) -> list[str]:
    if not value:
        return []
    aliases = {
        "gross domestic product": "gdp",
        "domestic product": "gdp",
        "国内生产总值": "gdp",
        "國內生產總值": "gdp",
        "population": "population",
        "人口": "population",
        "unemployment": "unemployment",
        "unemployment rate": "unemployment",
        "失业率": "unemployment",
        "失業率": "unemployment",
    }
    indicators: list[str] = []
    for raw in re.split(r"[,，;；/、\s]+", value):
        item = raw.strip().lower()
        if not item:
            continue
        canonical = aliases.get(item, item)
        if canonical and canonical not in indicators:
            indicators.append(canonical)
    return indicators


def fetch_fred_series(series_id: str, timeout: float) -> list[tuple[dt.date, float]]:
    url = FRED_GRAPH_URL.format(series_id=quote(series_id))
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/csv"})
    with urlopen(request, timeout=timeout) as response:
        text = response.read().decode("utf-8")
    rows: list[tuple[dt.date, float]] = []
    for item in csv.DictReader(io.StringIO(text)):
        date_text = str(item.get("observation_date") or "").strip()
        value_text = str(item.get(series_id) or "").strip()
        if not date_text or value_text in {"", "."}:
            continue
        try:
            rows.append((dt.date.fromisoformat(date_text), float(value_text)))
        except ValueError:
            continue
    return rows


def annual_average(series: list[tuple[dt.date, float]]) -> dict[int, float]:
    grouped: dict[int, list[float]] = {}
    for date_value, value in series:
        grouped.setdefault(date_value.year, []).append(value)
    return {year: sum(values) / len(values) for year, values in grouped.items() if values}


def build_fred_us_macro_rows(args: argparse.Namespace) -> tuple[list[dict[str, Any]], str, str]:
    gdp = annual_average(fetch_fred_series("GDP", args.timeout))
    population = annual_average(fetch_fred_series("POPTHM", args.timeout))
    unemployment = annual_average(fetch_fred_series("UNRATE", args.timeout))
    current_year = dt.date.today().year
    common_years = sorted(year for year in set(gdp) & set(population) & set(unemployment) if year < current_year)
    if not common_years:
        raise RuntimeError("FRED macro series returned no overlapping annual years.")
    try:
        requested_years = int(str(args.range_ or "5y").lower().rstrip("y"))
    except ValueError:
        requested_years = 5
    year_count = max(1, min(requested_years, 10))
    selected_years = common_years[-year_count:]
    rows = [
        {
            "year": str(year),
            "gdp_trillion_usd": round(gdp[year] / 1000, 2),
            "population_million": round(population[year] / 1000, 1),
            "unemployment_rate": round(unemployment[year], 1),
        }
        for year in selected_years
    ]
    return rows, "FRED public economic data CSV", "FRED GDP, POPTHM and UNRATE annual averages"


def fallback_us_macro_rows() -> list[dict[str, Any]]:
    return [
        {"year": "2020", "gdp_trillion_usd": 21.35, "population_million": 331.5, "unemployment_rate": 8.1},
        {"year": "2021", "gdp_trillion_usd": 23.68, "population_million": 332.0, "unemployment_rate": 5.4},
        {"year": "2022", "gdp_trillion_usd": 26.01, "population_million": 333.3, "unemployment_rate": 3.6},
        {"year": "2023", "gdp_trillion_usd": 27.72, "population_million": 334.9, "unemployment_rate": 3.6},
        {"year": "2024", "gdp_trillion_usd": 29.18, "population_million": 336.8, "unemployment_rate": 4.0},
    ]


def build_us_macro_payload(args: argparse.Namespace, indicators: list[str]) -> dict[str, Any]:
    try:
        rows, source, method = build_fred_us_macro_rows(args)
    except Exception as exc:
        rows = fallback_us_macro_rows()
        source = "Built-in US macro fallback dataset"
        method = f"FRED request failed; used bundled fallback rows ({exc})"
    base_gdp = rows[0]["gdp_trillion_usd"]
    base_population = rows[0]["population_million"]
    for row in rows:
        row["gdp_index"] = round(float(row["gdp_trillion_usd"]) / float(base_gdp) * 100, 1)
        row["population_index"] = round(float(row["population_million"]) / float(base_population) * 100, 1)
    series = []
    if not indicators or "gdp" in indicators:
        series.append({"field": "gdp_index", "label": "GDP指数（2020=100）", "type": "bar", "axis": "right", "color": "#2863f4"})
    if not indicators or "population" in indicators:
        series.append({"field": "population_index", "label": "人口指数（2020=100）", "type": "bar", "axis": "right", "color": "#4bc3d5"})
    if not indicators or "unemployment" in indicators:
        series.append({"field": "unemployment_rate", "label": "失业率（%）", "type": "line", "axis": "left", "color": "#ff6845"})
    title = args.title or "美国GDP、人口与失业率"
    chart_payload = {
        "chartType": "bar-line",
        "title": title,
        "xField": "year",
        "series": series,
        "data": rows,
        "rightAxis": {"digits": 0},
        "leftAxis": {"suffix": "%", "digits": 0},
    }
    return {
        "type": "yahoo-macro-data",
        "source": source,
        "symbol": "US_MACRO",
        "title": title,
        "subtitle": "United States annual macro indicators",
        "metric": "macroIndicators",
        "metricLabel": "Macro indicators",
        "method": method,
        "range": args.range_ or "5y",
        "interval": "annual",
        "chartType": "macro_data",
        "macroIndicators": indicators,
        "rows": rows,
        "chartPayload": chart_payload,
        "lineChartData": {
            "xField": "year",
            "series": series,
            "data": rows,
        },
    }


def fetch_chart(symbol: str, range_value: str, interval: str, timeout: float) -> dict[str, Any]:
    url = (
        YAHOO_CHART_URL.format(symbol=quote(symbol))
        + f"?range={quote(range_value)}&interval={quote(interval)}"
        + "&includePrePost=false&events=div%2Csplits"
    )
    try:
        return yahoo_json(url, f"{symbol} chart {range_value} {interval}", timeout=timeout)
    except RuntimeError as exc:
        raise SystemExit(f"Yahoo Finance request failed: {exc}") from exc


def fetch_chart_period(symbol: str, start: dt.date, end: dt.date, interval: str, timeout: float) -> dict[str, Any]:
    period1 = int(dt.datetime.combine(start, dt.time.min, tzinfo=dt.timezone.utc).timestamp())
    period2 = int(dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min, tzinfo=dt.timezone.utc).timestamp())
    url = (
        YAHOO_CHART_URL.format(symbol=quote(symbol))
        + f"?period1={period1}&period2={period2}&interval={quote(interval)}"
        + "&includePrePost=false&events=div%2Csplits"
    )
    try:
        return yahoo_json(url, f"{symbol} chart {period1} {period2} {interval}", timeout=timeout)
    except RuntimeError as exc:
        raise SystemExit(f"Yahoo Finance request failed: {exc}") from exc


def fetch_fundamentals(symbol: str, types: str, range_value: str, timeout: float) -> dict[str, Any]:
    period2 = int(time.time()) + 86400
    period1 = period2 - range_days(range_value) * 86400
    query = urlencode({"symbol": symbol, "type": types, "period1": period1, "period2": period2})
    url = YAHOO_FUNDAMENTALS_URL.format(symbol=quote(symbol)) + f"?{query}"
    try:
        return yahoo_json(url, f"{symbol} fundamentals {types} {range_value}")
    except RuntimeError:
        return {}


def range_days(range_value: str) -> int:
    value = (range_value or "1y").lower()
    mapping = {
        "1d": 2,
        "5d": 7,
        "1mo": 40,
        "3mo": 120,
        "6mo": 220,
        "ytd": 370,
        "1y": 370,
        "2y": 760,
        "5y": 1900,
        "10y": 3800,
        "max": 12000,
    }
    return mapping.get(value, 370)


def rows_from_response(payload: dict[str, Any], symbol: str, metric: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not isinstance(result, dict):
        error = (payload.get("chart") or {}).get("error")
        raise SystemExit(f"Yahoo Finance returned no chart data for {symbol}: {error or 'empty result'}")

    timestamps = result.get("timestamp") or []
    quote_payload = (((result.get("indicators") or {}).get("quote") or [None])[0]) or {}
    adj_payload = (((result.get("indicators") or {}).get("adjclose") or [None])[0]) or {}
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    timezone = meta.get("exchangeTimezoneName") or "UTC"

    rows: list[dict[str, Any]] = []
    previous_close: float | None = None
    for index, timestamp in enumerate(timestamps):
        row = {
            "date": format_timestamp(timestamp, timezone),
            "open": numeric_at(quote_payload, "open", index),
            "high": numeric_at(quote_payload, "high", index),
            "low": numeric_at(quote_payload, "low", index),
            "close": numeric_at(quote_payload, "close", index),
            "adjClose": numeric_at(adj_payload, "adjclose", index),
            "volume": numeric_at(quote_payload, "volume", index),
        }
        close = row["close"]
        row["returnPct"] = None
        if close is not None and previous_close not in (None, 0):
            row["returnPct"] = (close / previous_close - 1) * 100
        if close is not None:
            previous_close = close
        if row.get("date"):
            rows.append(row)

    first_close = next((row.get("close") for row in rows if isinstance(row.get("close"), (int, float)) and row.get("close")), None)
    for row in rows:
        close = row.get("close")
        row["cumulativeReturnPct"] = None
        if isinstance(close, (int, float)) and first_close not in (None, 0):
            row["cumulativeReturnPct"] = (float(close) / float(first_close) - 1) * 100

    metric_key = normalize_metric(metric)
    if metric_key == "adjClose" and meta.get("_aimeAdjustedCloseAvailable") is False:
        raise SystemExit(
            f"Fallback source returned unadjusted daily prices for {symbol}; adjusted close is unavailable."
        )
    if metric_key == "marketCap":
        chart_rows = [{"date": row["date"], "value": row.get("close"), **row} for row in rows]
        chart_rows = [row for row in chart_rows if isinstance(row.get("close"), (int, float)) and math.isfinite(row["close"])]
        if not chart_rows:
            raise SystemExit(f"Yahoo Finance returned no usable close data for {symbol}.")
        return chart_rows, meta
    chart_rows = [{"date": row["date"], "value": row.get(metric_key), **row} for row in rows]
    chart_rows = [row for row in chart_rows if isinstance(row.get("value"), (int, float)) and math.isfinite(row["value"])]
    if not chart_rows:
        raise SystemExit(f"Yahoo Finance returned no usable {metric_key} data for {symbol}.")
    return chart_rows, meta


def numeric_at(payload: dict[str, Any], key: str, index: int) -> float | None:
    values = payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(values, list) or index >= len(values):
        return None
    value = values[index]
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def format_timestamp(timestamp: Any, timezone: str) -> str:
    try:
        zone = ZoneInfo(timezone)
    except Exception:
        zone = dt.timezone.utc
    try:
        return dt.datetime.fromtimestamp(int(timestamp), tz=zone).strftime("%Y-%m-%d")
    except Exception:
        return ""


def normalize_metric(metric: str, *, allow_row_field: bool = False) -> str:
    raw = (metric or "close").strip()
    canonical = {
        "adjClose",
        "returnPct",
        "eventDayReturnPct",
        "cumulativeReturnPct",
        "marketCap",
        "financialPosition",
        "valuationMeasures",
        *FINANCIAL_METRIC_TYPES,
    }
    if raw in canonical:
        return raw
    key = raw.lower().replace("-", "_")
    aliases = {
        "price": "close",
        "last": "close",
        "adj_close": "adjClose",
        "adjclose": "adjClose",
        "adjusted_close": "adjClose",
        "return": "returnPct",
        "returns": "returnPct",
        "return_pct": "returnPct",
        "returnpct": "returnPct",
        "change_pct": "returnPct",
        "event_day_return": "eventDayReturnPct",
        "event_day_return_pct": "eventDayReturnPct",
        "eventdayreturnpct": "eventDayReturnPct",
        "same_day_return": "eventDayReturnPct",
        "same_day_return_pct": "eventDayReturnPct",
        "当日涨幅": "eventDayReturnPct",
        "当天涨幅": "eventDayReturnPct",
        "cumulative_return": "cumulativeReturnPct",
        "cumulative_returns": "cumulativeReturnPct",
        "cumulative_return_pct": "cumulativeReturnPct",
        "cumulativereturnpct": "cumulativeReturnPct",
        "total_return": "cumulativeReturnPct",
        "total_returns": "cumulativeReturnPct",
        "累计收益": "cumulativeReturnPct",
        "累计收益率": "cumulativeReturnPct",
        "volume": "volume",
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "market_cap": "marketCap",
        "marketcap": "marketCap",
        "market_capitalization": "marketCap",
        "capitalization": "marketCap",
        "mkt_cap": "marketCap",
        "市值": "marketCap",
        "valuation": "valuationMeasures",
        "valuations": "valuationMeasures",
        "valuation_measures": "valuationMeasures",
        "valuation_metrics": "valuationMeasures",
        "key_statistics": "valuationMeasures",
        "估值": "valuationMeasures",
        "估值指标": "valuationMeasures",
        "估值指標": "valuationMeasures",
        "net_income": "netIncome",
        "netincome": "netIncome",
        "net_profit": "netIncome",
        "netprofit": "netIncome",
        "quarterly_net_income": "netIncome",
        "净利润": "netIncome",
        "淨利潤": "netIncome",
        "total_revenue": "totalRevenue",
        "revenue": "totalRevenue",
        "quarterly_revenue": "totalRevenue",
        "营收": "totalRevenue",
        "營收": "totalRevenue",
        "operating_income": "operatingIncome",
        "quarterly_operating_income": "operatingIncome",
        "营业利润": "operatingIncome",
        "營業利潤": "operatingIncome",
        "gross_profit": "grossProfit",
        "quarterly_gross_profit": "grossProfit",
        "毛利润": "grossProfit",
        "毛利潤": "grossProfit",
        "financials": "financialPosition",
        "financial_position": "financialPosition",
        "financialposition": "financialPosition",
        "balance_sheet": "financialPosition",
        "balancesheet": "financialPosition",
        "财务状况": "financialPosition",
        "財務狀況": "financialPosition",
        "资产负债": "financialPosition",
        "資產負債": "financialPosition",
    }
    normalized = aliases.get(key)
    if normalized:
        return normalized
    if allow_row_field:
        return raw
    supported = "close, adj_close, volume, return_pct, cumulative_return, market_cap, valuation_measures, financial_position, net_income, total_revenue, operating_income, gross_profit"
    raise SystemExit(f"Unsupported Yahoo metric '{raw}'. Supported metrics: {supported}.")


def metric_label(metric_key: str) -> str:
    return {
        "close": "Close",
        "adjClose": "Adjusted close",
        "returnPct": "Daily return %",
        "eventDayReturnPct": "Event-day return %",
        "cumulativeReturnPct": "Cumulative return %",
        "volume": "Volume",
        "open": "Open",
        "high": "High",
        "low": "Low",
        "marketCap": "Market cap",
        "valuationMeasures": "Valuation measures",
        "financialPosition": "Financial position",
        "netIncome": "Net income",
        "totalRevenue": "Total revenue",
        "operatingIncome": "Operating income",
        "grossProfit": "Gross profit",
    }.get(metric_key, metric_key)


def detect_output_language(text: str) -> str:
    value = text or ""
    if re.search(r"[\u3040-\u30ff]", value):
        return "ja"
    if re.search(r"[\u0e00-\u0e7f]", value):
        return "th"
    if re.search(r"[總統後內累計收益率]", value):
        return "zh-Hant"
    if re.search(r"[\u3400-\u9fff]", value):
        return "zh-CN"
    return "en"


def localized_event_window_metric_label(window_days: int, language: str) -> str:
    if language == "ja":
        return f"就任後{window_days}日間の累積収益率"
    if language == "zh-Hant":
        return f"就任後{window_days}日累計收益率"
    if language == "zh-CN":
        return f"就任后{window_days}日累计收益率"
    if language == "th":
        return f"ผลตอบแทนสะสมใน {window_days} วันแรกหลังเข้ารับตำแหน่ง"
    return f"{window_days}-day cumulative return"


def localized_event_window_title(symbol: str, window_days: int, language: str) -> str:
    if language == "ja":
        return f"各イベント後{window_days}日間の{symbol}累積収益率"
    if language == "zh-Hant":
        return f"各事件後{window_days}日內{symbol}累計收益率"
    if language == "zh-CN":
        return f"各事件后{window_days}日内{symbol}累计收益率"
    if language == "th":
        return f"ผลตอบแทนสะสมของ {symbol} ใน {window_days} วันแรกหลังเหตุการณ์"
    return f"{symbol} {window_days}-day post-event cumulative return"


def localized_event_day_title(symbol: str, language: str) -> str:
    if language == "ja":
        return f"{symbol} イベント当日騰落率"
    if language == "zh-Hant":
        return f"{symbol}事件當日漲跌幅"
    if language == "zh-CN":
        return f"{symbol}事件当日涨跌幅"
    if language == "th":
        return f"ผลตอบแทนวันเกิดเหตุของ {symbol}"
    return f"{symbol} event-day return"


def localized_event_day_metric_label(language: str) -> str:
    if language == "ja":
        return "イベント当日騰落率"
    if language == "zh-Hant":
        return "事件當日漲跌幅"
    if language == "zh-CN":
        return "事件当日涨跌幅"
    if language == "th":
        return "ผลตอบแทนวันเกิดเหตุ"
    return "Event-day return"


def localize_event_labels(events: list[dict[str, str]], language: str) -> list[dict[str, str]]:
    translations = {
        "ja": {
            "George W. Bush 2001": "ジョージ・W・ブッシュ 2001",
            "Barack Obama 2009": "バラク・オバマ 2009",
            "Donald Trump 2017": "ドナルド・トランプ 2017",
            "Joe Biden 2021": "ジョー・バイデン 2021",
            "Donald Trump 2025": "ドナルド・トランプ 2025",
        },
        "zh-Hant": {
            "George W. Bush 2001": "喬治·W·布什 2001",
            "Barack Obama 2009": "巴拉克·奧巴馬 2009",
            "Donald Trump 2017": "唐納德·特朗普 2017",
            "Joe Biden 2021": "喬·拜登 2021",
            "Donald Trump 2025": "唐納德·特朗普 2025",
        },
        "zh-CN": {
            "George W. Bush 2001": "乔治·W·布什 2001",
            "Barack Obama 2009": "巴拉克·奥巴马 2009",
            "Donald Trump 2017": "唐纳德·特朗普 2017",
            "Joe Biden 2021": "乔·拜登 2021",
            "Donald Trump 2025": "唐纳德·特朗普 2025",
        },
        "th": {
            "George W. Bush 2001": "จอร์จ ดับเบิลยู. บุช 2001",
            "Barack Obama 2009": "บารัก โอบามา 2009",
            "Donald Trump 2017": "โดนัลด์ ทรัมป์ 2017",
            "Joe Biden 2021": "โจ ไบเดน 2021",
            "Donald Trump 2025": "โดนัลด์ ทรัมป์ 2025",
        },
    }
    mapping = translations.get(language, {})
    localized: list[dict[str, str]] = []
    for event in events:
        item = dict(event)
        label = str(item.get("label") or "")
        item["label"] = mapping.get(label, label)
        localized.append(item)
    return localized


def value_format(metric_key: str, value: float) -> str:
    if metric_key in {"returnPct", "eventDayReturnPct", "cumulativeReturnPct"}:
        return f"{value:.2f}%"
    if metric_key in {"volume", "marketCap"}:
        return compact_number(value)
    return f"{value:,.2f}"


def compact_number(value: float) -> str:
    abs_value = abs(value)
    for suffix, divisor in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs_value >= divisor:
            return f"{value / divisor:.2f}{suffix}"
    return f"{value:,.0f}"


def render_html(payload: dict[str, Any], output: Path) -> None:
    rows = payload["rows"]
    metric_key = payload["metric"]
    chart_type = payload["chartType"]
    is_bar_chart = chart_type in {"bar", "grouped-bar"}
    title = html.escape(payload["title"])
    subtitle = html.escape(display_subtitle(payload))
    values = [float(row["value"]) for row in rows]
    min_value = min(values)
    max_value = max(values)
    if math.isclose(min_value, max_value):
        min_value -= 1
        max_value += 1
    padded_min, padded_max = bar_extent(min_value, max_value) if is_bar_chart else padded_extent(metric_key, min_value, max_value)
    points = svg_points(rows, padded_min, padded_max)
    bars = svg_bars(rows, padded_min, padded_max) if is_bar_chart else ""
    line = (
        f'<polyline class="line" points="{html.escape(points)}" fill="none" />'
        if chart_type == "line"
        else ""
    )
    marker_markup = svg_line_markers(payload.get("markers") or [], rows, padded_min, padded_max) if chart_type == "line" else ""
    marker_legend_markup = marker_legend_html(payload.get("markers") or [])
    latest = rows[-1]
    first = rows[0]
    latest_text = "" if is_bar_chart else value_format(metric_key, latest["value"])
    change_text = ""
    if not is_bar_chart and metric_key not in {"returnPct", "eventDayReturnPct", "cumulativeReturnPct"} and first["value"]:
        total_change = (latest["value"] / first["value"] - 1) * 100
        change_text = f"{total_change:+.2f}%"
    y_ticks = build_y_ticks(metric_key, padded_min, padded_max, chart_type)
    x_ticks = build_x_ticks(rows, chart_type)
    zero_y = svg_y_for_value(0, padded_min, padded_max)
    zero_line = (
        f'<line class="zero-line" x1="0" y1="{zero_y:.2f}" x2="1008" y2="{zero_y:.2f}" />'
        if is_bar_chart and padded_min < 0 < padded_max
        else ""
    )
    grid_markup = "\n      ".join(
        f'<line class="grid-line" x1="0" y1="{tick["y"]:.2f}" x2="1008" y2="{tick["y"]:.2f}" />'
        f'<text class="axis-label y-label" x="1024" y="{tick["y"] + 4:.2f}">{html.escape(tick["label"])}</text>'
        for tick in y_ticks
    )
    x_markup = "\n      ".join(
        f'<text class="axis-label x-label" x="{tick["x"]:.2f}" y="260" text-anchor="{tick["anchor"]}">{html.escape(tick["label"])}</text>'
        for tick in x_ticks
    )
    data_json = json.dumps(
        [
            {
                "date": row.get("date"),
                "value": row.get("value"),
                "label": value_format(metric_key, row.get("value")),
                "positive": float(row.get("value") or 0) >= 0,
            }
            for row in rows
        ],
        ensure_ascii=False,
    )
    data_json_script = data_json.replace("</", "<\\/")
    change_class = "positive" if not change_text.startswith("-") else "negative"
    metric_name = html.escape(metric_label(metric_key))
    stat_attr = ' style="display:none"' if chart_type == "bar" else ""

    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
*{{box-sizing:border-box}}
html,body{{margin:0;background:#fff;color:#151922;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}}
.yahoo-card{{width:100%;padding:24px;background:#fff;border:0;border-radius:0;overflow:visible}}
.head{{display:flex;justify-content:space-between;gap:18px;align-items:flex-start;margin-bottom:8px}}
.title{{display:grid;gap:3px}}
h2{{margin:0;font-size:18px;line-height:1.28;font-weight:800;letter-spacing:0}}
.subtitle{{color:#687386;font-size:13px;font-weight:650}}
.stat{{text-align:right;display:grid;gap:2px;min-width:86px}}
.latest{{font-size:24px;font-weight:800;line-height:1.05}}
.change{{font-size:13px;font-weight:700}}
.change.positive{{color:#0f8b55}}
.change.negative{{color:#d64045}}
.chart-wrap{{position:relative;margin-top:16px}}
svg{{display:block;width:100%;height:auto;overflow:visible}}
.grid-line{{stroke:#edf1f6;stroke-width:1}}
.axis-line{{stroke:#d8dee8;stroke-width:1}}
.zero-line{{stroke:#9aa7b8;stroke-width:1.25}}
.hover-line{{stroke:#9aa4b2;stroke-width:1;stroke-dasharray:3 3;opacity:0}}
.hover-dot{{fill:#fff;stroke:#2863f4;stroke-width:2.4;opacity:0}}
.line{{stroke:#2863f4;stroke-width:2.7;stroke-linejoin:round;stroke-linecap:round}}
.threshold-marker{{stroke:#fff;stroke-width:2;filter:drop-shadow(0 3px 6px rgba(239,68,68,.28))}}
.event-label{{fill:#394150;font-size:11px;font-weight:750;paint-order:stroke;stroke:#fff;stroke-width:4px;stroke-linejoin:round;pointer-events:none}}
.bar{{rx:2;ry:2;opacity:.9}}
.bar.positive{{fill:#2563eb}}
.bar.negative{{fill:#f04452}}
.axis-label{{fill:#687386;font-size:12px;font-weight:650}}
.y-label{{text-anchor:start}}
.x-label{{fill:#687386}}
.foot{{display:flex;justify-content:space-between;align-items:center;gap:16px;color:#687386;font-size:12px;font-weight:650;margin-top:8px}}
.marker-legend{{display:flex;align-items:center;justify-content:flex-end;gap:14px;flex-wrap:wrap}}
.marker-legend-item{{display:inline-flex;align-items:center;gap:7px;white-space:nowrap}}
.marker-legend-symbol{{display:inline-block;width:10px;height:10px;border-radius:999px;box-shadow:0 0 0 2px #fff,0 3px 6px rgba(15,23,42,.12)}}
.marker-legend-symbol.rect{{width:12px;height:9px;border-radius:2px}}
.marker-legend-symbol.star{{width:13px;height:13px;border-radius:0;clip-path:polygon(50% 0%,61% 35%,98% 35%,68% 56%,79% 91%,50% 69%,21% 91%,32% 56%,2% 35%,39% 35%)}}
.tooltip{{position:absolute;z-index:20;min-width:190px;background:#363636;color:#f8fafc;border-radius:5px;padding:11px 12px;font-size:12px;line-height:1.75;box-shadow:none;pointer-events:none;opacity:0;visibility:hidden;transform:translateY(-4px);transition:opacity 120ms ease,transform 120ms ease,visibility 120ms ease}}
.tooltip::after{{content:"";position:absolute;left:var(--arrow-left,50%);bottom:-8px;transform:translateX(-50%);border-left:9px solid transparent;border-right:9px solid transparent;border-top:9px solid #363636}}
.tooltip.visible{{opacity:1;visibility:visible;transform:translateY(0)}}
.tooltip-title{{font-weight:800;margin-bottom:4px}}
.tooltip-row{{display:flex;align-items:center;justify-content:space-between;gap:18px;white-space:nowrap}}
.tooltip-dot{{display:inline-block;width:8px;height:8px;margin-right:7px;border-radius:50%;background:#2863f4;vertical-align:1px}}
</style>
</head>
<body>
<main class="yahoo-card">
  <header class="head">
    <div class="title">
      <h2>{title}</h2>
      <div class="subtitle">{subtitle}</div>
    </div>
      <div class="stat"{stat_attr}>
      <div class="latest">{html.escape(latest_text)}</div>
      <div class="change {change_class}">{html.escape(change_text)}</div>
    </div>
  </header>
  <section class="chart-wrap">
    <div class="tooltip" id="tooltip"></div>
    <svg id="chartSvg" viewBox="0 0 1080 270" role="img" aria-label="{title}">
      {grid_markup}
      <line class="axis-line" x1="0" y1="230" x2="1008" y2="230" />
      {zero_line}
      {bars}
      {line}
      {marker_markup}
      {x_markup}
      <line id="hoverLine" class="hover-line" x1="0" y1="20" x2="0" y2="230" />
      <circle id="hoverDot" class="hover-dot" cx="0" cy="230" r="4.5" />
    </svg>
  </section>
  <footer class="foot">
    <span>Source: Yahoo Finance</span>
    {marker_legend_markup}
  </footer>
  <script id="payload" type="application/json">{data_json_script}</script>
</main>
<script>
const rows = JSON.parse(document.getElementById('payload').textContent || '[]');
const svg = document.getElementById('chartSvg');
const tooltip = document.getElementById('tooltip');
const hoverLine = document.getElementById('hoverLine');
const hoverDot = document.getElementById('hoverDot');
const dims = {{left:0, right:1008, top:20, bottom:230}};
const yMin = {padded_min:.12f};
const yMax = {padded_max:.12f};
const isBarChart = {str(is_bar_chart).lower()};
function xFor(i) {{
  if (isBarChart) {{
    const slot = (dims.right - dims.left) / Math.max(rows.length, 1);
    return dims.left + slot * i + slot / 2;
  }}
  return dims.left + (dims.right - dims.left) * i / Math.max(rows.length - 1, 1);
}}
function yFor(v) {{
  return dims.top + (dims.bottom - dims.top) * (yMax - Number(v)) / Math.max(yMax - yMin, 1e-9);
}}
function showTooltip(event) {{
  if (!rows.length) return;
  const rect = svg.getBoundingClientRect();
  const ratioX = Math.min(Math.max((event.clientX - rect.left) / rect.width, 0), 1);
  const viewX = ratioX * 1080;
  const index = isBarChart
    ? Math.min(rows.length - 1, Math.max(0, Math.floor((viewX - dims.left) / ((dims.right - dims.left) / Math.max(rows.length, 1)))))
    : Math.min(rows.length - 1, Math.max(0, Math.round((viewX - dims.left) / (dims.right - dims.left) * (rows.length - 1))));
  const row = rows[index];
  const x = xFor(index);
  const y = yFor(row.value);
  hoverLine.setAttribute('x1', x);
  hoverLine.setAttribute('x2', x);
  hoverLine.style.opacity = '1';
  hoverDot.setAttribute('cx', x);
  hoverDot.setAttribute('cy', y);
  hoverDot.style.opacity = '1';
  const dotColor = row.positive ? '#2563eb' : '#f04452';
  tooltip.innerHTML = `<div class="tooltip-title">${{row.date || ''}}</div><div class="tooltip-row"><span><span class="tooltip-dot" style="background:${{dotColor}}"></span>{metric_name}</span><strong>${{row.label || ''}}</strong></div>`;
  tooltip.classList.add('visible');
  const sx = x / 1080 * rect.width;
  const firstGridY = dims.top / 270 * rect.height;
  const tw = tooltip.offsetWidth || 180;
  const th = tooltip.offsetHeight || 70;
  const left = Math.min(Math.max(sx - tw / 2, 8), rect.width - tw - 8);
  tooltip.style.left = `${{left}}px`;
  tooltip.style.top = `${{firstGridY - th - 8}}px`;
  tooltip.style.setProperty('--arrow-left', `${{Math.min(tw - 14, Math.max(14, sx - left))}}px`);
}}
function hideTooltip() {{
  tooltip.classList.remove('visible');
  hoverLine.style.opacity = '0';
  hoverDot.style.opacity = '0';
}}
svg.addEventListener('mousemove', showTooltip);
svg.addEventListener('mouseleave', hideTooltip);
function sendHeight(){{
  try {{
    const height = Math.ceil(Math.max(document.body.scrollHeight, document.documentElement.scrollHeight, 120));
    parent.postMessage({{type:'analysis-planner-chart-height', height}}, '*');
  }} catch (e) {{}}
}}
window.addEventListener('load', sendHeight);
window.addEventListener('resize', sendHeight);
setTimeout(sendHeight, 80);
setTimeout(sendHeight, 300);
</script>
</body>
</html>"""
    output.write_text(document, encoding="utf-8")


def render_dynamic_line(payload: dict[str, Any], output: Path) -> None:
    title = html.escape(str(payload.get("title") or "Yahoo data prepared"))
    rows = payload.get("chartPayload", {}).get("data") if isinstance(payload.get("chartPayload"), dict) else []
    row_count = len(rows) if isinstance(rows, list) else 0
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
html,body{{margin:0;background:#fff;color:#334155;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}}
.source-card{{padding:16px;border:1px solid #e5e7eb;border-radius:10px;background:#fff;font-size:13px;line-height:1.6}}
.source-card strong{{display:block;margin-bottom:4px;color:#111827;font-size:14px}}
</style>
</head>
<body>
<main class="source-card">
  <strong>{title}</strong>
  <div>Yahoo Finance 数据已准备，{row_count} 行。最终图表由下游 dynamic_line.py 渲染。</div>
</main>
<script>
function sendHeight(){{
  try {{ parent.postMessage({{type:'analysis-planner-chart-height', height:Math.ceil(document.body.scrollHeight || 100)}}, '*'); }} catch (e) {{}}
}}
window.addEventListener('load', sendHeight); setTimeout(sendHeight, 80);
</script>
</body>
</html>"""
    output.write_text(document, encoding="utf-8")


def render_unavailable_html(payload: dict[str, Any], output: Path) -> None:
    language = str(payload.get("language") or "en").lower()
    is_chinese = language.startswith("zh")
    title = html.escape(str(payload.get("title") or "Yahoo data unavailable"))
    symbol = html.escape(str(payload.get("symbol") or ""))
    raw_metric = str(payload.get("metric") or "")
    raw_period = str(payload.get("requestedPeriodType") or "requested")
    if is_chinese:
        metric_name = html.escape(
            {
                "totalRevenue": "营收",
                "netIncome": "净利润",
                "operatingIncome": "营业利润",
                "grossProfit": "毛利润",
            }.get(raw_metric, str(payload.get("metricLabel") or raw_metric or "财务指标"))
        )
        period_type = html.escape({"quarterly": "季度", "annual": "年度"}.get(raw_period, raw_period))
    else:
        metric_name = html.escape(str(payload.get("metricLabel") or raw_metric or "financial metric"))
        period_type = html.escape(raw_period)
    reason = html.escape(str(payload.get("unavailableReason") or "Yahoo Finance did not return the requested series."))
    heading = "Yahoo 未提供所请求的季度字段" if is_chinese else "Requested Yahoo field is unavailable"
    summary = (
        f"{symbol} 的 {period_type} {metric_name} 序列为空。这不是执行失败，也不会改用其他数据源。"
        if is_chinese
        else f"Yahoo returned an empty {period_type} {metric_name} series for {symbol}. This is not an execution failure, and no other source was used."
    )
    document = f"""<!doctype html>
<html lang="{html.escape(language)}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
*{{box-sizing:border-box}}
html,body{{margin:0;background:#fff;color:#172033;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}}
.card{{padding:22px 24px;border:1px solid #e5e7eb;border-radius:12px;background:#fff}}
.eyebrow{{color:#64748b;font-size:12px;font-weight:750;margin-bottom:8px}}
h2{{margin:0 0 10px;font-size:18px;line-height:1.35}}
p{{margin:0;color:#475569;font-size:14px;line-height:1.7}}
.alternative{{margin-top:12px;padding:10px 12px;border-radius:8px;background:#f8fafc;color:#334155}}
.detail{{margin-top:10px;color:#94a3b8;font-size:11px;word-break:break-word}}
</style>
</head>
<body>
<main class="card">
  <div class="eyebrow">Yahoo Finance · {symbol}</div>
  <h2>{heading}</h2>
  <p>{summary}</p>
  <div class="detail">{reason}</div>
</main>
<script>
function sendHeight(){{
  try {{ parent.postMessage({{type:'analysis-planner-chart-height', height:Math.ceil(document.body.scrollHeight || 140)}}, '*'); }} catch (e) {{}}
}}
window.addEventListener('load', sendHeight); setTimeout(sendHeight, 80);
</script>
</body>
</html>"""
    output.write_text(document, encoding="utf-8")


def build_unavailable_financial_payload(
    args: argparse.Namespace,
    symbol: str,
    metric_key: str,
    error_message: str,
) -> dict[str, Any]:
    requested_period = normalize_financial_period_type(args.period_type)
    return {
        "type": "yahoo-data-unavailable",
        "source": "Yahoo Finance",
        "symbol": symbol,
        "title": args.title or f"{symbol} {metric_label(metric_key)}",
        "metric": metric_key,
        "metricLabel": metric_label(metric_key),
        "requestedPeriodType": requested_period,
        "requestedDataAvailable": False,
        "unavailable": True,
        "unavailableReason": error_message,
        "answerStatus": "requested_field_unavailable",
        "sourceScope": "yahoo_only",
        "otherSourcesUsed": False,
        "fallbackApplied": False,
        "display": False,
        "rows": [],
        "language": args.language,
    }


def optimize_dynamic_line_document(payload: dict[str, Any], output: Path) -> None:
    chart_payload = payload.get("chartPayload") if isinstance(payload.get("chartPayload"), dict) else {}
    data = chart_payload.get("data") if isinstance(chart_payload.get("data"), list) else []
    encode = chart_payload.get("encode") if isinstance(chart_payload.get("encode"), dict) else {}
    x_key = str(encode.get("x") or "date")
    y_key = str(encode.get("y") or "value")
    series_key = str(encode.get("series") or "symbol")

    series_list: list[str] = []
    x_list: list[str] = []
    value_by_series_x: dict[tuple[str, str], float | None] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        series = str(item.get(series_key) or "")
        x_value = str(item.get(x_key) or "")
        if not series or not x_value:
            continue
        if series not in series_list:
            series_list.append(series)
        if x_value not in x_list:
            x_list.append(x_value)
        raw_value = item.get(y_key)
        value = raw_value if isinstance(raw_value, (int, float)) and math.isfinite(raw_value) else None
        value_by_series_x[(series, x_value)] = value

    if not series_list or not x_list:
        return

    full_series_data_map = {
        series: [value_by_series_x.get((series, x_value)) for x_value in x_list]
        for series in series_list
    }
    series_list_json = json.dumps(series_list, ensure_ascii=False, separators=(",", ":"))
    x_list_json = json.dumps(x_list, ensure_ascii=False, separators=(",", ":"))
    full_series_json = json.dumps(full_series_data_map, ensure_ascii=False, separators=(",", ":"))

    document = output.read_text(encoding="utf-8")
    document = re.sub(
        r"const rows = .*?;\n\s*const seriesList = .*?;\n\s*const xList = .*?;\n\s*const bgmDataUri = .*?;\n\s*const bgmFilename = .*?;",
        (
            "const rows = [];\n"
            f"    const seriesList = {series_list_json};\n"
            f"    const xList = {x_list_json};\n"
            "    const bgmDataUri = \"\";\n"
            "    const bgmFilename = \"\";"
        ),
        document,
        flags=re.S,
        count=1,
    )
    document = re.sub(
        r"const fullSeriesDataMap = \{\};\n\s*seriesList\.forEach\(\(seriesName\) => \{\n\s*fullSeriesDataMap\[seriesName\] = xList\.map\(\(xValue\) => \{\n\s*const found = rows\.find\(row => row\[seriesKey\] === seriesName && row\[xKey\] === xValue\);\n\s*return found \? rowValueToNumber\(found\[yKey\]\) : null;\n\s*\}\);\n\s*\}\);",
        f"const fullSeriesDataMap = {full_series_json};",
        document,
        flags=re.S,
        count=1,
    )
    output.write_text(document, encoding="utf-8")


def apply_dynamic_line_light_mode(output: Path) -> None:
    document = output.read_text(encoding="utf-8")
    replacements = {
        '<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>': '<script src="/static/vendor/echarts.min.js"></script>',
        "background: #0b0e14;": "background: #ffffff;",
        "radial-gradient(circle at top, rgba(30, 64, 175, 0.16), transparent 36%),\n        #0b0e14": "#ffffff",
        "border-radius: 18px;": "border-radius: 0;",
        "border: 1px solid rgba(51, 65, 85, 0.42);": "border: 0;",
        "background: rgba(15, 23, 42, 0.84);": "background: rgba(255, 255, 255, 0.92);",
        "border: 1px solid rgba(59, 80, 120, 0.42);": "border: 1px solid #dfe5ee;",
        "box-shadow: 0 10px 28px rgba(0, 0, 0, 0.24);": "box-shadow: none;",
        "background: rgba(17, 24, 39, 0.92);": "background: #f5f7fb;",
        "color: #dbe7ff;": "color: #273244;",
        "color: #9fb3d9;": "color: #687386;",
        "color: #94a3b8;": "color: #687386;",
        "color: #91a4c6;": "color: #687386;",
        "color: #dbe5f3;": "color: #273244;",
        "color: #8192ad;": "color: #687386;",
        "background: rgba(148, 163, 184, 0.18);": "background: #dfe5ee;",
        "background: rgba(30, 64, 175, 0.88);": "background: #2863f4;",
        "color: '#f8fafc'": "color: '#151922'",
        "fill: '#f8fafc'": "fill: '#151922'",
        "color: '#a7b5c9'": "color: '#687386'",
        "backgroundColor: 'rgba(15, 23, 42, 0.94)'": "backgroundColor: 'rgba(255, 255, 255, 0.98)'",
        "borderColor: '#334155'": "borderColor: '#dfe5ee'",
        "textStyle: { color: '#f1f5f9' }": "textStyle: { color: '#151922' }",
        "color: 'rgba(148, 163, 184, 0.48)'": "color: 'rgba(104, 115, 134, 0.42)'",
        "color:#f8fafc;": "color:#151922;",
        "color:#cbd5e1;": "color:#687386;",
        "color: '#8ea1bc'": "color: '#687386'",
        "color: 'rgba(56, 104, 190, 0.16)'": "color: '#edf1f7'",
        "fill: compact ? 'rgba(148, 163, 184, 0.14)' : 'rgba(148, 163, 184, 0.16)'": "fill: compact ? 'rgba(104, 115, 134, 0.11)' : 'rgba(104, 115, 134, 0.13)'",
        "backgroundColor: 'rgba(15, 23, 42, 0.68)'": "backgroundColor: 'rgba(255, 255, 255, 0.92)'",
        "shadowColor: 'rgba(0,0,0,0.18)'": "shadowColor: 'rgba(17,24,39,0)'",
        "lineStyle: {\n            width: isCompactLayout() ? 3.5 : 4,": "lineStyle: {\n            width: isCompactLayout() ? 2 : 2.4,",
        "symbolSize: 8,": "symbolSize: 6,",
        "shadowBlur: 8,": "shadowBlur: 0,",
        "const animDuration = isPlaying ? currentInterval : 260;": "const animDuration = isPlaying ? currentInterval : 0;",
        "animation: true,": "animation: isPlaying,",
        "animationDuration: animDuration,": "animationDuration: animDuration,",
        "connectNulls: false,": "connectNulls: true,",
    }
    for old, new in replacements.items():
        document = document.replace(old, new)
    document = document.replace("setTimeLabel(startLabel, xList[0] || '');", "setTimeLabel(startLabel, xList.length ? xList[0] : '');")
    document = document.replace("setTimeLabel(endLabel, xList[xList.length - 1] || '');", "setTimeLabel(endLabel, xList.length ? xList[xList.length - 1] : '');")
    document = document.replace(
        "const topFixedY = compact ? 70 : 76;\n"
        "            const pointerX = Array.isArray(point) ? point[0] : viewWidth / 2;\n"
        "            if (pointerX <= viewWidth / 2) return [rightFixedX, topFixedY];\n"
        "            return [leftFixedX, topFixedY];",
        "const topFixedY = compact ? 58 : 64;\n"
        "            const pointerX = Array.isArray(point) ? point[0] : viewWidth / 2;\n"
        "            const shouldUseLeft = pointerX > contentWidth + leftFixedX * 2;\n"
        "            return [shouldUseLeft ? leftFixedX : rightFixedX, topFixedY];",
    )
    document = document.replace(
        "if (!params || !params.length) return '';\n"
        "            const axisValue = params[0].axisValue;",
        "if (!params || !params.length) return '';\n"
        "            params = params.filter(item => {\n"
        "              const value = Array.isArray(item.value) ? item.value[item.value.length - 1] : item.value;\n"
        "              return value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));\n"
        "            });\n"
        "            if (!params.length) return '';\n"
        "            const axisValue = params[0].axisValue;",
    )
    document = document.replace(
        "function formatValue(value) {\n"
        "      if (value === null || value === undefined || value === '') return '-';\n"
        "      return `${value}${unit}`;\n"
        "    }",
        "function formatValue(value) {\n"
        "      if (value === null || value === undefined || value === '') return '-';\n"
        "      const num = Number(value);\n"
        "      if (Number.isFinite(num)) {\n"
        "        const abs = Math.abs(num);\n"
        "        let scaled = num;\n"
        "        let compactUnit = '';\n"
        "        if (!unit) {\n"
        "          if (abs >= 1e12) { scaled = num / 1e12; compactUnit = 'T'; }\n"
        "          else if (abs >= 1e9) { scaled = num / 1e9; compactUnit = 'B'; }\n"
        "          else if (abs >= 1e6) { scaled = num / 1e6; compactUnit = 'M'; }\n"
        "        }\n"
        "        return `${scaled.toFixed(2)}${compactUnit}${unit}`;\n"
        "      }\n"
        "      return `${value}${unit}`;\n"
        "    }",
    )
    document = document.replace(
        "let timer = null;\n"
        "    let isPlaying = false;",
        "let timer = null;\n"
        "    let isPlaying = false;\n"
        "    let lastFrameAt = 0;\n"
        "    let frameAccumulator = 0;",
    )
    document = document.replace(
        "animationDuration: animDuration,\n"
        "          animationEasing: animEasing,\n"
        "          animationDurationUpdate: animDuration,\n"
        "          animationEasingUpdate: animEasing,\n"
        "          itemStyle: { color: theme.line, borderWidth: 0 },",
        "animationDuration: animDuration,\n"
        "          animationEasing: animEasing,\n"
        "          animationDurationUpdate: animDuration,\n"
        "          animationEasingUpdate: animEasing,\n"
        "          progressive: 0,\n"
        "          hoverAnimation: false,\n"
        "          emphasis: { disabled: true },\n"
        "          itemStyle: { color: theme.line, borderWidth: 0 },",
    )
    document = document.replace("markPoint: lastPoint ? {", "markPoint: (!isPlaying && lastPoint) ? {")
    document = document.replace(
        "      }, true);\n"
        "\n"
        "      chart.setOption({\n"
        "        graphic: [\n"
        "          buildCenterTimeGraphic(),\n"
        "          ...buildRightLabelsGraphics(safeIndex)\n"
        "        ]\n"
        "      });",
        "      }, { notMerge: false, lazyUpdate: true, silent: true });\n"
        "\n"
        "      requestAnimationFrame(() => {\n"
        "        chart.setOption({\n"
        "          graphic: [\n"
        "            buildCenterTimeGraphic(),\n"
        "            ...buildRightLabelsGraphics(safeIndex)\n"
        "          ]\n"
        "        }, { notMerge: false, lazyUpdate: true, silent: true });\n"
        "      });",
    )
    document = document.replace(
        "    function stopTimer() {\n"
        "      if (timer) {\n"
        "        clearInterval(timer);\n"
        "        timer = null;\n"
        "      }\n"
        "    }",
        "    function stopTimer() {\n"
        "      if (timer) {\n"
        "        cancelAnimationFrame(timer);\n"
        "        timer = null;\n"
        "      }\n"
        "      lastFrameAt = 0;\n"
        "      frameAccumulator = 0;\n"
        "    }",
    )
    document = document.replace(
        "    function startTimer() {\n"
        "      stopTimer();\n"
        "      timer = setInterval(() => {\n"
        "        if (currentIndex >= xList.length - 1) {\n"
        "          currentIndex = xList.length - 1;\n"
        "          isPlaying = false;\n"
        "          setPlayButtonState();\n"
        "          stopTimer();\n"
        "          stopBackgroundMusic(true);\n"
        "          renderChart();\n"
        "          return;\n"
        "        }\n"
        "        currentIndex += 1;\n"
        "        renderChart();\n"
        "      }, currentInterval);\n"
        "    }",
        "    function startTimer() {\n"
        "      stopTimer();\n"
        "      const tick = (timestamp) => {\n"
        "        if (!isPlaying) return;\n"
        "        if (!lastFrameAt) lastFrameAt = timestamp;\n"
        "        const delta = Math.min(timestamp - lastFrameAt, currentInterval * 1.5);\n"
        "        lastFrameAt = timestamp;\n"
        "        frameAccumulator += delta;\n"
        "\n"
        "        if (frameAccumulator >= currentInterval) {\n"
        "          const steps = Math.max(1, Math.floor(frameAccumulator / currentInterval));\n"
        "          frameAccumulator = frameAccumulator % currentInterval;\n"
        "          currentIndex = Math.min(currentIndex + steps, xList.length - 1);\n"
        "          renderChart();\n"
        "\n"
        "          if (currentIndex >= xList.length - 1) {\n"
        "            currentIndex = xList.length - 1;\n"
        "            isPlaying = false;\n"
        "            setPlayButtonState();\n"
        "            stopBackgroundMusic(true);\n"
        "            renderChart();\n"
        "            stopTimer();\n"
        "            return;\n"
        "          }\n"
        "        }\n"
        "\n"
        "        timer = requestAnimationFrame(tick);\n"
        "      };\n"
        "      timer = requestAnimationFrame(tick);\n"
        "    }",
    )
    output.write_text(document, encoding="utf-8")


def apply_dynamic_line_compact_style(output: Path) -> None:
    document = output.read_text(encoding="utf-8")
    document = document.replace(
        "html, body {\n"
        "      margin: 0;\n"
        "      width: 100%;\n"
        "      height: 100%;",
        "html, body {\n"
        "      margin: 0;\n"
        "      width: 100%;\n"
        "      height: auto;\n"
        "      min-height: 0;",
    )
    document = document.replace(
        "#wrap {\n"
        "      position: relative;\n"
        "      width: 100%;\n"
        "      height: 100vh;",
        "#wrap {\n"
        "      position: relative;\n"
        "      width: 100%;\n"
        "      height: min(56.25vw, 520px);\n"
        "      min-height: 280px;",
    )
    document = document.replace(
        "#chart {\n"
        "      width: 100%;\n"
        "      height: 100vh;\n"
        "    }",
        "#chart {\n"
        "      width: 100%;\n"
        "      height: 100%;\n"
        "    }",
    )
    document = document.replace(
        "left: 14px;\n"
        "      right: 14px;\n"
        "      bottom: 12px;",
        "left: 12px;\n"
        "      right: 12px;\n"
        "      bottom: 12px;",
    )
    document = re.sub(
        r"@media \(max-width: 768px\) \{\n\s*#wrap \{\n\s*border-radius: 16px;\n\s*\}",
        "@media (max-width: 768px) {\n      #wrap {\n        border-radius: 16px;\n        height: calc(100vw * 0.5625);\n        min-height: 220px;\n      }",
        document,
        count=1,
    )
    document = re.sub(
        r"#timelineBar \{\n\s*left: 10px;\n\s*right: 10px;\n\s*bottom: 10px;",
        "#timelineBar {\n        left: 12px;\n        right: 12px;\n        bottom: 12px;",
        document,
        count=1,
    )
    document = document.replace(
        "const speedPresets = [\n"
        "      { label: '0.5x', interval: 1200 },\n"
        "      { label: '1x', interval: 700 },\n"
        "      { label: '2x', interval: 350 }\n"
        "    ];",
        "const speedPresets = [\n"
        "      { label: '1x', interval: 700 },\n"
        "      { label: '2x', interval: 350 },\n"
        "      { label: '3x', interval: 230 }\n"
        "    ];",
    )
    document = re.sub(
        r"legend: \{.*?\n\s*\},\n\s*tooltip: \{.*?\n\s*\},\n\s*grid:",
        "legend: { show: false },\n        tooltip: { show: false },\n        grid:",
        document,
        flags=re.S,
        count=1,
    )
    document = document.replace(
        "left: compact ? 16 : 20,\n"
        "          top: compact ? 14 : 18,",
        "left: 12,\n"
        "          top: 12,",
    )
    document = document.replace(
        "grid: {\n"
        "          left: compact ? 16 : 20,\n"
        "          right: compact ? 12 : 16,\n"
        "          top: compact ? 74 : 80,\n"
        "          bottom: compact ? 78 : 82,\n"
        "          containLabel: true\n"
        "        },",
        "grid: {\n"
        "          left: 12,\n"
        "          right: 12,\n"
        "          top: compact ? 46 : 50,\n"
        "          bottom: compact ? 66 : 70,\n"
        "          containLabel: true\n"
        "        },",
    )
    document = document.replace("splitNumber: 5,", "splitNumber: 4,")
    document = document.replace("type: 'dashed',", "type: 'solid',")
    document = document.replace(
        "markPoint: (!isPlaying && lastPoint) ? {",
        "markPoint: lastPoint ? {",
    )
    document = document.replace(
        "markPoint: lastPoint ? {\n"
        "            symbol: 'circle',\n"
        "            symbolSize: 6,\n"
        "            itemStyle: { color: theme.line, borderWidth: 0 },\n"
        "            label: { show: false },",
        "markPoint: lastPoint ? {\n"
        "            symbol: 'circle',\n"
        "            symbolSize: isCompactLayout() ? 7 : 8,\n"
        "            itemStyle: { color: theme.line, borderWidth: 2, borderColor: '#ffffff' },\n"
        "            label: { show: false },",
    )
    document = document.replace(
        "const leftLimit = compact ? 16 : 20;",
        "const leftLimit = 12;",
    )
    document = document.replace(
        "const anchorX = Math.max(item.x - labelGap, leftLimit);",
        "const anchorX = Math.max(Math.min(item.x + labelGap, chart.getWidth() - 12), leftLimit);",
    )
    document = document.replace(
        "const height = Math.ceil(Math.max(document.body.scrollHeight, document.documentElement.scrollHeight, 120));",
        "const wrap = document.getElementById('wrap');\n"
        "    const rect = wrap ? wrap.getBoundingClientRect() : null;\n"
        "    const height = Math.ceil(Math.max(rect ? rect.height : 0, document.body.scrollHeight, document.documentElement.scrollHeight, 120));",
    )
    output.write_text(document, encoding="utf-8")


def display_subtitle(payload: dict[str, Any]) -> str:
    range_value = str(payload.get("range") or "").strip()
    interval = str(payload.get("interval") or "").strip()
    if range_value and interval:
        return f"{range_value} / {interval}"
    return range_value or interval


def padded_extent(metric_key: str, min_value: float, max_value: float) -> tuple[float, float]:
    span = max(max_value - min_value, abs(max_value) * 0.08, 1e-9)
    lower = min_value - span * 0.08
    if metric_key in {"marketCap", "volume"} or min_value >= 0:
        lower = max(0, lower)
    return lower, max_value + span * 0.12


def nice_step(span: float, count: int = 4) -> float:
    rough = max(span / max(count, 1), 0.0001)
    power = 10 ** math.floor(math.log10(rough))
    normalized = rough / power
    if normalized <= 1:
        factor = 1
    elif normalized <= 2:
        factor = 2
    elif normalized <= 5:
        factor = 5
    else:
        factor = 10
    return factor * power


def bar_extent(min_value: float, max_value: float) -> tuple[float, float]:
    lower = min(0.0, min_value)
    upper = max(0.0, max_value)
    if math.isclose(lower, upper):
        lower -= 1
        upper += 1
    step = nice_step(upper - lower, 4)
    lower = math.floor(lower / step) * step
    upper = math.ceil(upper / step) * step
    if math.isclose(lower, upper):
        upper = lower + step
    return lower, upper


def build_y_ticks(metric_key: str, min_value: float, max_value: float, chart_type: str = "line") -> list[dict[str, Any]]:
    if chart_type == "bar":
        step = nice_step(max_value - min_value, 4)
        top = math.ceil(max_value / step) * step
        bottom = math.floor(min_value / step) * step
        tick_values = []
        value = top
        while value >= bottom - step / 2:
            tick_values.append(round(value, 10))
            value -= step
        if not any(math.isclose(value, 0, abs_tol=step / 1000) for value in tick_values):
            tick_values.append(0)
            tick_values = sorted(set(tick_values), reverse=True)
        return [
            {
                "value": value,
                "label": value_format(metric_key, value),
                "y": svg_y_for_value(value, min_value, max_value),
            }
            for value in tick_values
        ]
    ticks = []
    for index in range(5):
        ratio = index / 4
        value = max_value - (max_value - min_value) * ratio
        y = 20 + (230 - 20) * ratio
        ticks.append({"value": value, "label": value_format(metric_key, value), "y": y})
    return ticks


def build_x_ticks(rows: list[dict[str, Any]], chart_type: str = "line") -> list[dict[str, Any]]:
    count = len(rows)
    if count <= 1:
        return []
    if chart_type == "bar" and count <= 12:
        slot = 1008 / count
        seen_dates: dict[str, int] = {}
        date_counts: dict[str, int] = {}
        for row in rows:
            date = str(row.get("date") or "")
            date_counts[date] = date_counts.get(date, 0) + 1
        ticks = []
        for index, row in enumerate(rows):
            date = str(row.get("date") or "")
            seen_dates[date] = seen_dates.get(date, 0) + 1
            x = slot * index + slot / 2
            label = event_bar_tick_label(date, index)
            if date_counts.get(date, 0) > 1:
                label = f"{label} #{seen_dates[date]}"
            ticks.append({"x": x, "label": label, "anchor": "middle"})
        return ticks
    desired = 6 if count >= 60 else min(5, count)
    indexes = sorted({round(i * (count - 1) / max(desired - 1, 1)) for i in range(desired)})
    ticks = []
    for index in indexes:
        x = 1008 * index / max(count - 1, 1)
        anchor = "middle"
        if index == 0:
            anchor = "start"
        elif index == count - 1:
            anchor = "end"
        ticks.append({"x": x, "label": date_tick_label(str(rows[index].get("date") or "")), "anchor": anchor})
    return ticks


def date_tick_label(value: str) -> str:
    parts = value.split("-")
    if len(parts) >= 3:
        return parts[0] if parts[1:] == ["01", "01"] else f"{parts[0]}-{parts[1]}"
    return value


def event_bar_tick_label(value: str, index: int) -> str:
    parts = value.split("-")
    if len(parts) >= 3:
        return f"{parts[1]}-{parts[2]}"
    return value or str(index + 1)


def svg_y_for_value(value: float, min_value: float, max_value: float) -> float:
    top, height = 20, 210
    denom = max(max_value - min_value, 1e-9)
    return top + height * (max_value - value) / denom


def svg_points(rows: list[dict[str, Any]], min_value: float, max_value: float) -> str:
    left, top, width, height = 0, 20, 1008, 210
    denom = max(max_value - min_value, 1e-9)
    count = max(len(rows) - 1, 1)
    points = []
    for index, row in enumerate(rows):
        x = left + width * index / count
        y = top + height * (max_value - float(row["value"])) / denom
        points.append(f"{x:.2f},{y:.2f}")
    return " ".join(points)


def parse_style_config(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def build_threshold_markers_from_overlay(overlay: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if str(overlay.get("type") or "") not in {"thresholdMarkers", "threshold-markers", "pointMarkers", "point-markers"}:
        return []
    marker_metric = normalize_metric(str(overlay.get("sourceMetric") or overlay.get("metric") or ""), allow_row_field=True)
    if not marker_metric:
        return []
    target_metric = normalize_metric(str(overlay.get("targetField") or overlay.get("targetMetric") or "value"), allow_row_field=True)
    try:
        threshold = float(overlay.get("threshold"))
    except (TypeError, ValueError):
        return []
    direction = str(overlay.get("direction") or "above").strip().lower()
    color = str(overlay.get("color") or "#ef4444")
    radius = overlay.get("radius", 4.2)
    shape = str(overlay.get("shape") or overlay.get("symbol") or "circle").strip().lower()
    legend_label = str(overlay.get("legendLabel") or overlay.get("legend") or "")
    show_in_legend = bool(overlay.get("showInLegend", False))
    event_label = str(overlay.get("eventLabel") or overlay.get("pointLabel") or "")
    label_template = str(overlay.get("labelTemplate") or "")
    show_event_label = bool(overlay.get("showEventLabel", bool(event_label or label_template)))
    try:
        max_labels = max(0, int(overlay.get("maxLabels", 8)))
    except (TypeError, ValueError):
        max_labels = 8
    width = overlay.get("width", 10)
    height = overlay.get("height", 8)
    markers: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        marker_value = row.get(marker_metric)
        line_value = row.get(target_metric) if target_metric != "close" else row.get("value")
        if not isinstance(line_value, (int, float)):
            line_value = row.get("value")
        if not isinstance(marker_value, (int, float)) or not isinstance(line_value, (int, float)):
            continue
        matched = marker_value > threshold if direction == "above" else marker_value < threshold
        if not matched:
            continue
        markers.append(
            {
                "index": index,
                "date": row.get("date"),
                "value": line_value,
                "markerMetric": marker_metric,
                "targetMetric": target_metric,
                "markerValue": marker_value,
                "threshold": threshold,
                "direction": direction,
                "color": color,
                "radius": radius,
                "shape": shape,
                "width": width,
                "height": height,
                "legendLabel": legend_label,
                "showInLegend": show_in_legend,
                "eventLabel": event_label,
                "labelTemplate": label_template,
                "showEventLabel": show_event_label,
                "maxLabels": max_labels,
                "label": value_format(marker_metric, marker_value),
            }
        )
    return markers


def build_threshold_markers(args: argparse.Namespace, rows: list[dict[str, Any]], style_config: dict[str, Any]) -> list[dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    overlays = style_config.get("overlays")
    if isinstance(overlays, list):
        for overlay in overlays:
            if isinstance(overlay, dict):
                markers.extend(build_threshold_markers_from_overlay(overlay, rows))
    if markers:
        return markers
    if args.marker_threshold is None or not args.marker_metric:
        return []
    legacy_overlay = {
        "type": "thresholdMarkers",
        "sourceMetric": args.marker_metric,
        "threshold": args.marker_threshold,
        "direction": args.marker_direction,
        "color": args.marker_color,
    }
    return build_threshold_markers_from_overlay(legacy_overlay, rows)


def svg_line_markers(markers: list[dict[str, Any]], rows: list[dict[str, Any]], min_value: float, max_value: float) -> str:
    if not markers or not rows:
        return ""
    width = 1008
    count = max(len(rows) - 1, 1)
    nodes = []
    label_counts: dict[tuple[str, str, str], int] = {}
    for marker in markers:
        try:
            index = int(marker.get("index"))
            value = float(marker.get("value"))
        except Exception:
            continue
        x = width * index / count
        y = svg_y_for_value(value, min_value, max_value)
        color = html.escape(str(marker.get("color") or "#ef4444"))
        shape = str(marker.get("shape") or "circle").strip().lower()
        try:
            radius = max(1.0, min(12.0, float(marker.get("radius") or 4.2)))
        except (TypeError, ValueError):
            radius = 4.2
        try:
            marker_w = max(2.0, min(24.0, float(marker.get("width") or radius * 2.4)))
            marker_h = max(2.0, min(24.0, float(marker.get("height") or radius * 2.0)))
        except (TypeError, ValueError):
            marker_w, marker_h = radius * 2.4, radius * 2.0
        label = html.escape(str(marker.get("label") or ""))
        date = html.escape(str(marker.get("date") or ""))
        event_label = marker_event_label(marker)
        label_key = (
            str(marker.get("eventLabel") or marker.get("labelTemplate") or ""),
            str(marker.get("color") or ""),
            str(marker.get("shape") or ""),
        )
        label_counts[label_key] = label_counts.get(label_key, 0)
        show_label = bool(marker.get("showEventLabel") and event_label and label_counts[label_key] < int(marker.get("maxLabels") or 0))
        if shape in {"rect", "rectangle", "square"}:
            nodes.append(
                f'<rect class="threshold-marker" x="{x - marker_w / 2:.2f}" y="{y - marker_h / 2:.2f}" '
                f'width="{marker_w:.2f}" height="{marker_h:.2f}" rx="1.8" ry="1.8" fill="{color}">'
                f'<title>{date} {label}</title></rect>'
            )
        elif shape in {"star", "pentagram", "五角星"}:
            points = star_points(x, y, radius * 1.55, radius * 0.68)
            nodes.append(
                f'<polygon class="threshold-marker" points="{points}" fill="{color}">'
                f'<title>{date} {label}</title></polygon>'
            )
        else:
            nodes.append(
                f'<circle class="threshold-marker" cx="{x:.2f}" cy="{y:.2f}" r="{radius:.2f}" fill="{color}">'
                f'<title>{date} {label}</title></circle>'
            )
        if show_label:
            label_counts[label_key] += 1
            nodes.append(
                f'<text class="event-label" x="{x + 8:.2f}" y="{max(14, y - 9):.2f}">{html.escape(event_label)}</text>'
            )
    return "\n      ".join(nodes)


def star_points(cx: float, cy: float, outer: float, inner: float) -> str:
    points = []
    for index in range(10):
        angle = -math.pi / 2 + index * math.pi / 5
        radius = outer if index % 2 == 0 else inner
        points.append(f"{cx + math.cos(angle) * radius:.2f},{cy + math.sin(angle) * radius:.2f}")
    return " ".join(points)


def marker_event_label(marker: dict[str, Any]) -> str:
    template = str(marker.get("labelTemplate") or "").strip()
    if template:
        return (
            template.replace("{date}", str(marker.get("date") or ""))
            .replace("{value}", str(marker.get("label") or ""))
            .replace("{markerValue}", str(marker.get("label") or ""))
        )
    return str(marker.get("eventLabel") or marker.get("pointLabel") or "").strip()


def marker_legend_html(markers: list[dict[str, Any]]) -> str:
    seen: set[tuple[str, str, str]] = set()
    items: list[str] = []
    for marker in markers:
        if not marker.get("showInLegend"):
            continue
        label = str(marker.get("legendLabel") or marker.get("label") or "").strip()
        if not label:
            continue
        color = str(marker.get("color") or "#ef4444")
        shape = str(marker.get("shape") or "circle").strip().lower()
        key = (label, color, shape)
        if key in seen:
            continue
        seen.add(key)
        if shape in {"rect", "rectangle", "square"}:
            symbol_class = "marker-legend-symbol rect"
        elif shape in {"star", "pentagram", "五角星"}:
            symbol_class = "marker-legend-symbol star"
        else:
            symbol_class = "marker-legend-symbol"
        items.append(
            f'<span class="marker-legend-item"><i class="{symbol_class}" style="background:{html.escape(color)}"></i>'
            f'{html.escape(label)}</span>'
        )
    if not items:
        return ""
    return '<span class="marker-legend">' + "".join(items) + "</span>"


def svg_bars(rows: list[dict[str, Any]], min_value: float, max_value: float) -> str:
    left, top, width, height = 0, 20, 1008, 210
    denom = max(max_value - min_value, 1e-9)
    count = len(rows)
    slot = width / max(count, 1)
    bar_width = max(8, min(56, slot * 0.48))
    baseline = top + height if min_value >= 0 else top + height * (max_value - 0) / denom
    nodes = []
    stride = max(1, math.ceil(count / 180))
    visible_rows = rows[::stride]
    visible_count = len(visible_rows)
    slot = width / max(visible_count, 1)
    bar_width = max(8, min(56, slot * 0.48))
    for index, row in enumerate(visible_rows):
        value = float(row["value"])
        x = left + slot * index + (slot - bar_width) / 2
        y = top + height * (max_value - value) / denom
        rect_y = min(y, baseline)
        rect_h = max(1, abs(baseline - y))
        tone = "positive" if value >= 0 else "negative"
        nodes.append(
            f'<rect class="bar {tone}" x="{x:.2f}" y="{rect_y:.2f}" width="{bar_width:.2f}" height="{rect_h:.2f}" />'
        )
    return "\n      ".join(nodes)


def fundamentals_series_rows(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    results = (payload.get("timeseries") or {}).get("result") or []
    rows: list[dict[str, Any]] = []
    for series in results:
        if not isinstance(series, dict):
            continue
        values = series.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            raw = ((item.get("reportedValue") or {}).get("raw")) if isinstance(item.get("reportedValue"), dict) else None
            if not isinstance(raw, (int, float)) or not math.isfinite(raw):
                continue
            date = str(item.get("asOfDate") or "").strip()
            if date:
                rows.append({"date": date, key: float(raw), "value": float(raw), "sourceMetric": key})
    return sorted(rows, key=lambda row: row["date"])


def normalize_financial_period_type(period_type: str | None) -> str:
    value = str(period_type or "quarterly").strip().lower()
    aliases = {
        "quarter": "quarterly",
        "q": "quarterly",
        "季度": "quarterly",
        "季报": "quarterly",
        "annual": "annual",
        "yearly": "annual",
        "year": "annual",
        "年度": "annual",
        "年报": "annual",
    }
    normalized = aliases.get(value, value)
    if normalized not in {"quarterly", "annual"}:
        raise SystemExit("Yahoo financial metrics support --period-type quarterly or annual.")
    return normalized


def requested_financial_periods(range_value: str, range_length: int | None) -> int:
    if isinstance(range_length, int):
        if range_length <= 0:
            raise SystemExit("--range-length must be a positive integer.")
        return min(range_length, 40)
    raw = str(range_value or "").strip()
    if raw.isdigit():
        return min(max(int(raw), 1), 40)
    return 8


def latest_financial_snapshot(
    symbol: str,
    metric_key: str,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]] | None:
    info_field = LATEST_FINANCIAL_INFO_FIELDS.get(metric_key)
    if not info_field:
        return None
    try:
        info = ticker_info(symbol)
    except Exception:
        return None
    value = info.get(info_field)
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None
    period_end = info.get("mostRecentQuarter") or info.get("lastFiscalYearEnd")
    try:
        date = dt.datetime.fromtimestamp(int(period_end), tz=dt.timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        date = dt.date.today().isoformat()
    currency = str(info.get("financialCurrency") or info.get("currency") or "")
    row = {
        "date": date,
        "periodType": "ttm",
        metric_key: float(value),
        "value": float(value),
        "sourceMetric": f"quoteSummary.{info_field}",
    }
    meta = {"currency": currency}
    details = {
        "method": f"Yahoo quote summary latest {info_field}",
        "source": "Yahoo Finance quote summary",
        "financialPeriodType": "ttm",
        "fetchPeriods": 1,
        "actualPeriods": 1,
        "fallbackApplied": True,
        "fallbackReason": "quarterly and annual time-series unavailable; used latest Yahoo financial snapshot",
    }
    return [row], meta, details


def fetch_financial_metric_rows(
    symbol: str,
    metric_key: str,
    period_type: str | None,
    range_value: str,
    range_length: int | None,
    timeout: float,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    metric_types = FINANCIAL_METRIC_TYPES.get(metric_key)
    if not metric_types:
        raise SystemExit(f"Unsupported Yahoo financial metric '{metric_key}'.")
    normalized_period = normalize_financial_period_type(period_type)
    yahoo_type = metric_types[normalized_period]
    limit = requested_financial_periods(range_value, range_length)
    # Request a sufficiently wide window because Yahoo's quarterly statements
    # are sparse and the CLI range may be a point count such as "8".
    lookback_range = "10y" if normalized_period == "annual" else "5y"
    raw = fetch_fundamentals(symbol, yahoo_type, lookback_range, timeout)
    rows = fundamentals_series_rows(raw, yahoo_type)
    fallback_applied = False
    # A number of non-US issuers do not publish US-style quarterly income
    # statements through Yahoo. When the caller did not explicitly select a
    # period, retry annual data instead of failing an otherwise valid request.
    if not rows and period_type is None and normalized_period == "quarterly":
        normalized_period = "annual"
        yahoo_type = metric_types[normalized_period]
        raw = fetch_fundamentals(symbol, yahoo_type, "10y", timeout)
        rows = fundamentals_series_rows(raw, yahoo_type)
        fallback_applied = bool(rows)
    if not rows and period_type is None:
        snapshot = latest_financial_snapshot(symbol, metric_key)
        if snapshot:
            return snapshot
    if not rows:
        raise SystemExit(f"Yahoo Finance returned no usable {yahoo_type} data for {symbol}.")
    selected_rows = rows[-limit:]
    normalized_rows = [
        {
            "date": row["date"],
            "periodType": normalized_period,
            metric_key: row["value"],
            "value": row["value"],
            "sourceMetric": yahoo_type,
        }
        for row in selected_rows
    ]
    currency = ""
    for series in (raw.get("timeseries") or {}).get("result") or []:
        values = series.get(yahoo_type) if isinstance(series, dict) else None
        if isinstance(values, list):
            currency = next((str(item.get("currencyCode") or "") for item in reversed(values) if isinstance(item, dict)), "")
            if currency:
                break
    meta = {"currency": currency}
    details = {
        "method": f"Yahoo fundamentals-timeseries {yahoo_type}",
        "source": "Yahoo Finance fundamentals-timeseries",
        "financialPeriodType": normalized_period,
        "fetchPeriods": limit,
        "fetchPurpose": "structured data window; may include prior-year comparison periods for derived metrics",
        "actualPeriods": len(normalized_rows),
        "fallbackApplied": fallback_applied,
        "fallbackReason": "quarterly series unavailable; used annual series" if fallback_applied else "",
    }
    return normalized_rows, meta, details


def fetch_financial_position_rows(
    symbol: str,
    period_type: str | None,
    range_value: str,
    range_length: int | None,
    timeout: float,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    requested_symbol = symbol
    # A generic financial-position request is more useful as an annual history.
    # Explicit --period-type quarterly remains strict and is never rewritten.
    normalized_period = normalize_financial_period_type(period_type or "annual")
    yahoo_types = [mapping[normalized_period] for mapping in FINANCIAL_POSITION_FIELD_TYPES.values()]
    limit = requested_financial_periods(range_value, range_length)
    raw = fetch_fundamentals(symbol, ",".join(yahoo_types), "10y" if normalized_period == "annual" else "5y", timeout)

    rows: list[dict[str, Any]] = []
    for field, mapping in FINANCIAL_POSITION_FIELD_TYPES.items():
        yahoo_type = mapping[normalized_period]
        for item in fundamentals_series_rows(raw, yahoo_type):
            rows.append(
                {
                    "date": item["date"],
                    "periodType": normalized_period,
                    "financialField": field,
                    "financialFieldLabel": FINANCIAL_POSITION_FIELD_LABELS[field],
                    "sourceMetric": yahoo_type,
                    "value": item["value"],
                }
            )
    if not rows:
        raise SystemExit(f"Yahoo Finance returned no usable {normalized_period} financial-position data for {symbol}.")

    selected_dates = sorted({str(row["date"]) for row in rows})[-limit:]
    rows = sorted(
        [row for row in rows if str(row["date"]) in selected_dates],
        key=lambda row: (str(row["date"]), str(row["financialField"])),
    )
    currency = ""
    for series in (raw.get("timeseries") or {}).get("result") or []:
        if not isinstance(series, dict):
            continue
        yahoo_type = next((item for item in yahoo_types if isinstance(series.get(item), list)), "")
        values = series.get(yahoo_type) if yahoo_type else None
        if isinstance(values, list):
            currency = next((str(item.get("currencyCode") or "") for item in reversed(values) if isinstance(item, dict)), "")
            if currency:
                break
    meta = {"currency": currency, "resolvedSymbol": symbol, "requestedSymbol": requested_symbol}
    details = {
        "method": "Yahoo fundamentals-timeseries financial-position bundle",
        "source": "Yahoo Finance fundamentals-timeseries",
        "financialPeriodType": normalized_period,
        "fetchPeriods": limit,
        "actualPeriods": len(selected_dates),
        "availableFields": sorted({str(row["financialField"]) for row in rows}),
        "requestedSymbol": requested_symbol,
        "resolvedSymbol": symbol,
    }
    return rows, meta, details


def market_cap_rows(
    symbol: str,
    price_rows: list[dict[str, Any]],
    meta: dict[str, Any],
    range_value: str,
    timeout: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    fundamentals = fetch_fundamentals(symbol, "trailingMarketCap", range_value, timeout)
    actual_rows = fundamentals_series_rows(fundamentals, "trailingMarketCap")
    if len(actual_rows) >= min(8, max(2, len(price_rows) // 12)):
        return actual_rows, {
            "method": "Yahoo fundamentals trailingMarketCap",
            "source": "Yahoo Finance fundamentals-timeseries trailingMarketCap",
            "actualMarketCapPoints": len(actual_rows),
        }

    latest_market_cap = actual_rows[-1]["value"] if actual_rows else None
    latest_price = meta.get("regularMarketPrice") or next(
        (row.get("close") for row in reversed(price_rows) if isinstance(row.get("close"), (int, float)) and row.get("close")),
        None,
    )
    if not latest_market_cap or not latest_price:
        try:
            snapshot = ticker_info(symbol)
        except RuntimeError:
            snapshot = {}
        latest_market_cap = latest_market_cap or snapshot.get("marketCap")
        latest_price = latest_price or snapshot.get("regularMarketPrice") or snapshot.get("currentPrice")
    if not latest_market_cap or not latest_price:
        raise SystemExit(f"Yahoo Finance returned no usable market cap basis for {symbol}.")

    shares_estimate = float(latest_market_cap) / float(latest_price)
    rows: list[dict[str, Any]] = []
    for row in price_rows:
        close = row.get("close")
        if not isinstance(close, (int, float)) or not math.isfinite(close):
            continue
        market_cap = float(close) * shares_estimate
        rows.append({**row, "marketCap": market_cap, "value": market_cap})
    return rows, {
        "method": "estimated marketCap = Yahoo historical close * latest implied shares",
        "source": "Yahoo Finance chart close + latest Yahoo market-cap snapshot",
        "actualMarketCapPoints": len(actual_rows),
        "latestMarketCap": latest_market_cap,
        "latestPrice": latest_price,
        "impliedSharesOutstanding": shares_estimate,
    }


def parse_symbols(args: argparse.Namespace) -> list[str]:
    raw = args.symbols or args.code or "AAPL"
    symbols: list[str] = []
    for item in re.split(r"[,，;；/、\s]+", str(raw)):
        symbol = canonicalize_yahoo_symbol(item.strip().upper())
        if not symbol or symbol in {"AND", "VS", "V", "WITH"}:
            continue
        if re.fullmatch(r"\^?[A-Z0-9][A-Z0-9.=\-]{0,14}", symbol):
            symbols.append(symbol)
    deduped: list[str] = []
    for symbol in symbols:
        if symbol not in deduped:
            deduped.append(symbol)
    return deduped or ["AAPL"]


def canonicalize_yahoo_symbol(symbol: str) -> str:
    value = str(symbol or "").strip().upper()
    match = re.fullmatch(r"0*(\d{1,6})\.HK", value)
    if not match:
        return value
    digits = match.group(1)
    if len(digits) <= 4:
        return f"{digits.zfill(4)}.HK"
    return f"{digits[-4:]}.HK"


def fetch_valuation_measure_rows(
    symbol: str,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Return Yahoo quote-summary valuation measures as a tidy snapshot.

    Keeping the measures in rows lets an agent select only comparable units
    for a chart instead of accidentally treating valuation snapshots as a
    historical price or return series.
    """
    info = ticker_info(symbol)
    market_time = info.get("regularMarketTime")
    try:
        snapshot_date = dt.datetime.fromtimestamp(int(market_time), tz=dt.timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        snapshot_date = dt.date.today().isoformat()
    currency = str(info.get("currency") or "USD")
    rows: list[dict[str, Any]] = []
    for field, (info_field, label, unit_type) in VALUATION_INFO_FIELDS.items():
        raw_value = info.get(info_field)
        if not isinstance(raw_value, (int, float)) or not math.isfinite(float(raw_value)):
            continue
        value = float(raw_value)
        rows.append(
            {
                "date": snapshot_date,
                "symbol": symbol,
                "series": symbol,
                "valuationField": field,
                "valuationFieldLabel": label,
                "value": value,
                "unitType": unit_type,
                "unit": currency if unit_type == "currency" else "x",
                "sourceMetric": f"quoteSummary.{info_field}",
            }
        )
    if not rows:
        raise SystemExit(f"Yahoo Finance returned no usable valuation measures for {symbol}.")
    meta = {
        "currency": currency,
        "exchangeName": info.get("exchange") or info.get("exchangeName"),
        "regularMarketPrice": info.get("regularMarketPrice"),
    }
    details = {
        "method": "Yahoo quote summary valuation snapshot; values are raw Yahoo fields and are not derived by the agent",
        "source": "Yahoo Finance quote summary / key statistics",
        "snapshotDate": snapshot_date,
        "availableFields": [row["valuationField"] for row in rows],
        "missingFields": [field for field in VALUATION_INFO_FIELDS if field not in {row["valuationField"] for row in rows}],
    }
    return rows, meta, details


def fetch_metric_rows(
    symbol: str,
    metric_key: str,
    range_value: str,
    interval: str,
    timeout: float,
    period_type: str | None = None,
    range_length: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    if metric_key == "valuationMeasures":
        return fetch_valuation_measure_rows(symbol)
    if metric_key == "financialPosition":
        return fetch_financial_position_rows(symbol, period_type, range_value, range_length, timeout)
    if metric_key in FINANCIAL_METRIC_TYPES:
        return fetch_financial_metric_rows(symbol, metric_key, period_type, range_value, range_length, timeout)
    raw = fetch_chart(symbol, range_value, interval, timeout)
    rows, meta = rows_from_response(raw, symbol, metric_key)
    method_details: dict[str, Any] = {
        "source": meta.get("_aimeSource"),
        "method": meta.get("_aimeMethod"),
        "providerSymbol": meta.get("_aimeProviderSymbol"),
    }
    if metric_key == "marketCap":
        rows, method_details = market_cap_rows(symbol, rows, meta, range_value, timeout)
    return rows, meta, method_details


def fetch_metric_rows_for_period(
    symbol: str,
    metric_key: str,
    start_date: dt.date,
    end_date: dt.date,
    interval: str,
    timeout: float,
    period_type: str | None = None,
    range_length: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    if metric_key == "valuationMeasures":
        return fetch_valuation_measure_rows(symbol)
    if metric_key == "financialPosition":
        range_value = "10y" if normalize_financial_period_type(period_type or "annual") == "annual" else "5y"
        return fetch_financial_position_rows(symbol, period_type, range_value, range_length, timeout)
    if metric_key in FINANCIAL_METRIC_TYPES:
        range_value = "10y" if normalize_financial_period_type(period_type) == "annual" else "5y"
        return fetch_financial_metric_rows(symbol, metric_key, period_type, range_value, range_length, timeout)
    raw = fetch_chart_period(symbol, start_date, end_date, interval, timeout)
    rows, meta = rows_from_response(raw, symbol, metric_key)
    method_details: dict[str, Any] = {
        "source": meta.get("_aimeSource"),
        "method": meta.get("_aimeMethod"),
        "providerSymbol": meta.get("_aimeProviderSymbol"),
    }
    if metric_key == "marketCap":
        rows, method_details = market_cap_rows(symbol, rows, meta, "custom-period", timeout)
    return rows, meta, method_details


def parse_optional_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value).strip())
    except ValueError:
        return None


def build_dynamic_line_payload(
    args: argparse.Namespace,
    symbols: list[str],
    metric_key: str,
    range_value: str,
    interval: str,
) -> dict[str, Any]:
    combined_rows: list[dict[str, Any]] = []
    metas: dict[str, Any] = {}
    method_by_symbol: dict[str, Any] = {}
    start_date = parse_optional_date(args.start_date)
    end_date = parse_optional_date(args.end_date)
    has_explicit_period = bool(start_date and end_date and start_date <= end_date)
    for symbol in symbols:
        if has_explicit_period and start_date and end_date:
            rows, meta, method_details = fetch_metric_rows_for_period(
                symbol, metric_key, start_date, end_date, interval, args.timeout, args.period_type, args.range_length
            )
        else:
            rows, meta, method_details = fetch_metric_rows(
                symbol, metric_key, range_value, interval, args.timeout, args.period_type, args.range_length
            )
        metas[symbol] = meta
        method_by_symbol[symbol] = method_details
        for row in rows:
            value = row.get("value")
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                continue
            rounded_value = round(float(value), 4)
            combined_rows.append(
                {
                    **row,
                    "symbol": symbol,
                    "series": (
                        f"{symbol} {row.get('financialFieldLabel')}"
                        if metric_key == "financialPosition" and len(symbols) > 1
                        else row.get("financialFieldLabel")
                        if metric_key == "financialPosition"
                        else symbol
                    ),
                    "value": rounded_value,
                    "label": value_format(metric_key, rounded_value),
                }
            )
    if not combined_rows:
        raise SystemExit("Yahoo Finance returned no usable comparison data.")

    title = args.title or f"{' vs '.join(symbols)} {metric_label(metric_key)}"
    range_label = f"{start_date.isoformat()}..{end_date.isoformat()}" if has_explicit_period and start_date and end_date else range_value
    fallback_methods = [
        item for item in method_by_symbol.values() if isinstance(item, dict) and item.get("source")
    ]
    source = (
        "Yahoo Finance public chart API"
        if not fallback_methods
        else "Yahoo Finance with Nasdaq historical API fallback"
    )
    method = (
        "Yahoo chart values"
        if not fallback_methods
        else "; ".join(
            f"{item_symbol}: {details.get('method')}"
            for item_symbol, details in method_by_symbol.items()
            if isinstance(details, dict) and details.get("method")
        )
    )
    if metric_key in FINANCIAL_METRIC_TYPES or metric_key == "financialPosition":
        method_values = [item for item in method_by_symbol.values() if isinstance(item, dict)]
        financial_period_type = next(
            (str(item.get("financialPeriodType") or "").strip() for item in method_values if item.get("financialPeriodType")),
            normalize_financial_period_type(args.period_type),
        )
        actual_periods = max(
            [int(item.get("actualPeriods") or 0) for item in method_values if str(item.get("actualPeriods") or "").isdigit()] or [0]
        )
        if financial_period_type:
            range_label = f"latest {actual_periods or args.range_length or ''} {financial_period_type} periods".replace("  ", " ").strip()
        source = next(
            (str(item.get("source") or "").strip() for item in method_values if item.get("source")),
            "Yahoo Finance fundamentals-timeseries",
        )
        method = next(
            (str(item.get("method") or "").strip() for item in method_values if item.get("method")),
            (
                "Yahoo fundamentals-timeseries financial-position bundle"
                if metric_key == "financialPosition"
                else f"Yahoo fundamentals-timeseries {FINANCIAL_METRIC_TYPES[metric_key].get(financial_period_type or 'annual', metric_key)}"
            ),
        )
    series_field = "series" if metric_key == "financialPosition" else "symbol"
    chart_data = [
        {"date": row.get("date"), series_field: row.get(series_field), "value": row.get("value")}
        for row in sorted(combined_rows, key=lambda item: (str(item.get("date") or ""), str(item.get(series_field) or "")))
    ]
    chart_payload = {
        "chartName": "dynamic_line",
        "title": title,
        "theme": "light",
        "unit": "%" if metric_key in {"returnPct", "cumulativeReturnPct"} else "",
        "encode": {"x": "date", "y": "value", "series": series_field},
        "data": chart_data,
    }
    primary_meta = metas.get(symbols[0], {}) if symbols else {}
    primary_method = method_by_symbol.get(symbols[0], {}) if symbols else {}
    is_financial_payload = metric_key in FINANCIAL_METRIC_TYPES or metric_key == "financialPosition"
    return {
        "type": "yahoo-data",
        "source": source,
        "symbols": symbols,
        "symbol": ",".join(symbols),
        "title": title,
        "subtitle": range_label if is_financial_payload else f"{range_label} / {interval}",
        "metric": metric_key,
        "metricLabel": metric_label(metric_key),
        "method": method,
        "range": range_label,
        "interval": interval,
        "chartType": "dynamic_line",
        "theme": "light",
        "rows": combined_rows,
        "chartPayload": chart_payload,
        "lineChartData": {
            "xField": "date",
            "yField": "value",
            "seriesField": series_field,
            "series": [
                {"field": item, "label": item}
                for item in dict.fromkeys(str(row.get(series_field) or "") for row in combined_rows)
                if item
            ],
            "data": chart_data,
        },
        "metaBySymbol": metas,
        "methodBySymbol": method_by_symbol,
        "currency": primary_meta.get("currency"),
        "financialPeriodType": primary_method.get("financialPeriodType"),
        "actualPeriods": primary_method.get("actualPeriods"),
        "availableFields": primary_method.get("availableFields"),
        "requestedSymbol": primary_method.get("requestedSymbol") or primary_meta.get("requestedSymbol"),
        "resolvedSymbol": primary_method.get("resolvedSymbol") or primary_meta.get("resolvedSymbol"),
        "fallbackApplied": bool(primary_method.get("fallbackApplied")),
        "fallbackReason": primary_method.get("fallbackReason") or "",
    }


def build_valuation_measures_payload(args: argparse.Namespace, symbols: list[str]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    metas: dict[str, dict[str, Any]] = {}
    details_by_symbol: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        symbol_rows, meta, details = fetch_valuation_measure_rows(symbol)
        rows.extend(symbol_rows)
        metas[symbol] = meta
        details_by_symbol[symbol] = details
    multiples = [row for row in rows if row.get("unitType") == "multiple"]
    snapshot_date = max((str(row.get("date") or "") for row in rows), default=dt.date.today().isoformat())
    title = args.title or f"{' vs '.join(symbols)} valuation comparison"
    primary_meta = metas.get(symbols[0], {})
    return {
        "type": "yahoo-data",
        "source": "Yahoo Finance quote summary / key statistics",
        "symbols": symbols,
        "symbol": ",".join(symbols),
        "title": title,
        "subtitle": f"Snapshot as of {snapshot_date}; valuation multiples only in chart",
        "metric": "valuationMeasures",
        "metricLabel": "Valuation measures",
        "method": "Raw Yahoo quote-summary fields; no agent-derived valuation values",
        "range": "latest",
        "interval": "snapshot",
        "chartType": "grouped_bar",
        "theme": "light",
        "currency": primary_meta.get("currency"),
        "rows": rows,
        "detailsBySymbol": details_by_symbol,
        "chartPayload": {
            "chartName": "grouped_bar",
            "title": title,
            "theme": "light",
            "unit": "x",
            "encode": {"x": "valuationFieldLabel", "y": "value", "series": "symbol"},
            "data": [
                {
                    "valuationFieldLabel": row["valuationFieldLabel"],
                    "symbol": row["symbol"],
                    "value": round(float(row["value"]), 4),
                }
                for row in multiples
            ],
        },
    }


def build_event_window_payload(
    args: argparse.Namespace,
    symbol: str,
    events: list[dict[str, str]],
    window_days: int,
) -> dict[str, Any]:
    language = detect_output_language(args.title or "")
    localized_events = localize_event_labels(events, language)
    label_by_original = {
        original.get("label", ""): localized.get("label", original.get("label", ""))
        for original, localized in zip(events, localized_events)
    }
    chart_rows: list[dict[str, Any]] = []
    source_rows: list[dict[str, Any]] = []
    meta: dict[str, Any] = {}
    for event in events:
        try:
            start_date = dt.date.fromisoformat(event["date"])
        except ValueError:
            continue
        end_date = start_date + dt.timedelta(days=max(1, window_days))
        raw = fetch_chart_period(symbol, start_date, end_date, "1d", args.timeout)
        window_rows, meta = rows_from_response(raw, symbol, "close")
        window = [(dt.date.fromisoformat(str(row.get("date"))), row) for row in window_rows if row.get("date")]
        if not window:
            continue
        base_date, base_row = window[0]
        base_close = base_row.get("close")
        if not isinstance(base_close, (int, float)) or not math.isfinite(base_close) or base_close == 0:
            continue
        label = label_by_original.get(event["label"], event["label"])
        previous_close: float | None = None
        values_by_day: dict[int, float] = {0: 0.0}
        for trade_date, row in window:
            close = row.get("close")
            if not isinstance(close, (int, float)) or not math.isfinite(close):
                continue
            day = (trade_date - start_date).days
            if day < 0 or day > window_days:
                continue
            value = round((float(close) / float(base_close) - 1) * 100, 2)
            values_by_day[day] = value
            daily_return = None
            if previous_close not in (None, 0):
                daily_return = round((float(close) / float(previous_close) - 1) * 100, 2)
            item = {
                "event": label,
                "eventDate": event["date"],
                "sourceEvent": event["label"],
                "date": trade_date.isoformat(),
                "day": day,
                "close": round(float(close), 4),
                "dailyReturnPct": daily_return,
                "value": value,
            }
            previous_close = float(close)
            source_rows.append(item)
        last_value: float | None = None
        for day in range(0, window_days + 1):
            if day in values_by_day:
                last_value = values_by_day[day]
            if last_value is None:
                continue
            chart_rows.append({"day": day, "event": label, "value": last_value})
    if not chart_rows:
        raise SystemExit("No event-window rows could be calculated from Yahoo data.")

    title = args.title or localized_event_window_title(symbol, window_days, language)
    chart_data = sorted(chart_rows, key=lambda item: (int(item.get("day") or 0), str(item.get("event") or "")))
    chart_payload = {
        "chartName": "dynamic_line",
        "title": title,
        "theme": "light",
        "unit": "%",
        "encode": {"x": "day", "y": "value", "series": "event"},
        "data": chart_data,
    }
    return {
        "type": "yahoo-data",
        "source": "Yahoo Finance public chart API",
        "symbol": symbol,
        "title": title,
        "subtitle": f"event window / {window_days} calendar days",
        "metric": "eventWindowCumulativeReturnPct",
        "metricLabel": localized_event_window_metric_label(window_days, language),
        "method": "Yahoo daily close values; dailyReturnPct is day-over-day return, value is cumulative return normalized to each event's first available trading day",
        "range": "max",
        "interval": "1d",
        "chartType": "dynamic_line",
        "theme": "light",
        "windowDays": window_days,
        "events": localized_events,
        "rows": source_rows,
        "chartPayload": chart_payload,
        "lineChartData": {
            "xField": "day",
            "yField": "value",
            "seriesField": "event",
            "series": [{"field": event["label"], "label": event["label"]} for event in localized_events],
            "data": chart_data,
        },
        "currency": meta.get("currency"),
        "exchangeName": meta.get("exchangeName"),
    }


def build_event_day_bar_payload(
    args: argparse.Namespace,
    symbols: list[str],
    events: list[dict[str, str]],
) -> dict[str, Any]:
    language = detect_output_language(args.title or "")
    primary_symbol = symbols[0] if symbols else "AAPL"
    multi_symbol = len(symbols) > 1
    localized_events = localize_event_labels(events, language)
    label_by_original = {
        original.get("label", ""): localized.get("label", original.get("label", ""))
        for original, localized in zip(events, localized_events)
    }
    rows: list[dict[str, Any]] = []
    meta: dict[str, Any] = {}
    for symbol in symbols:
        for event in events:
            try:
                event_date = dt.date.fromisoformat(event["date"])
            except ValueError:
                continue
            start_date = event_date - dt.timedelta(days=8)
            end_date = event_date + dt.timedelta(days=8)
            raw = fetch_chart_period(symbol, start_date, end_date, "1d", args.timeout)
            window_rows, symbol_meta = rows_from_response(raw, symbol, "close")
            if not meta:
                meta = symbol_meta
            dated_rows: list[tuple[dt.date, dict[str, Any]]] = []
            for row in window_rows:
                try:
                    trade_date = dt.date.fromisoformat(str(row.get("date")))
                except ValueError:
                    continue
                close = row.get("close")
                if isinstance(close, (int, float)) and math.isfinite(close):
                    dated_rows.append((trade_date, row))
            dated_rows.sort(key=lambda item: item[0])
            target_index = next((index for index, (trade_date, _row) in enumerate(dated_rows) if trade_date >= event_date), None)
            if target_index is None or target_index <= 0:
                continue
            trade_date, target_row = dated_rows[target_index]
            previous_date, previous_row = dated_rows[target_index - 1]
            close = target_row.get("close")
            previous_close = previous_row.get("close")
            if (
                not isinstance(close, (int, float))
                or not isinstance(previous_close, (int, float))
                or not math.isfinite(close)
                or not math.isfinite(previous_close)
                or previous_close == 0
            ):
                continue
            value = round((float(close) / float(previous_close) - 1) * 100, 2)
            label = label_by_original.get(event["label"], event["label"])
            rows.append(
                {
                    "date": event["date"],
                    "tradingDate": trade_date.isoformat(),
                    "previousTradingDate": previous_date.isoformat(),
                    "event": label,
                    "sourceEvent": event["label"],
                    "symbol": symbol,
                    "close": round(float(close), 4),
                    "previousClose": round(float(previous_close), 4),
                    "value": value,
                }
            )
    if not rows:
        raise SystemExit("No event-day return rows could be calculated from Yahoo data.")

    title = args.title or localized_event_day_title(",".join(symbols), language)
    rows.sort(key=lambda item: (str(item.get("date") or ""), str(item.get("symbol") or "")))
    chart_name = "grouped-bar" if multi_symbol else "bar"
    chart_payload = {
        "chartName": chart_name,
        "title": title,
        "theme": "light",
        "unit": "%",
        "encode": {"x": "date", "y": "value", "series": "symbol" if multi_symbol else "event"},
        "data": [
            {"date": row["date"], "event": row["event"], "symbol": row["symbol"], "value": row["value"]}
            for row in rows
        ],
    }
    return {
        "type": "yahoo-data",
        "source": "Yahoo Finance public chart API",
        "symbol": primary_symbol,
        "symbols": symbols,
        "title": title,
        "subtitle": "event day / first trading day on or after event",
        "metric": "eventDayReturnPct",
        "metricLabel": localized_event_day_metric_label(language),
        "method": "Yahoo daily close values; value is the first trading day's close divided by the previous trading day's close minus 1",
        "range": "event-day",
        "interval": "1d",
        "chartType": chart_name,
        "theme": "light",
        "events": localized_events,
        "rows": rows,
        "chartPayload": chart_payload,
        "currency": meta.get("currency"),
        "exchangeName": meta.get("exchangeName"),
    }


def main() -> int:
    args = parse_args()
    style_config = parse_style_config(args.style_json)
    macro_indicators = parse_macro_indicators(args.macro_indicators)
    if macro_indicators:
        payload = build_us_macro_payload(args, macro_indicators)
        emit_payload_result(args, payload)
        return 0
    symbols = parse_symbols(args)
    symbol = symbols[0]
    range_value = normalize_range(args)
    interval = normalize_interval(range_value, args.interval)
    start_date = parse_optional_date(args.start_date)
    end_date = parse_optional_date(args.end_date)
    metric_key = normalize_metric(args.metric)
    event_windows = parse_event_windows(args.event_windows_json)
    if event_windows:
        if args.chart_type == "bar":
            payload = build_event_day_bar_payload(args, symbols, event_windows)
        else:
            payload = build_event_window_payload(args, symbol, event_windows, max(1, min(args.window_days, 370)))
    elif metric_key == "valuationMeasures":
        payload = build_valuation_measures_payload(args, symbols)
    elif metric_key == "financialPosition" or args.chart_type == "dynamic_line" or len(symbols) > 1:
        payload = build_dynamic_line_payload(args, symbols, metric_key, range_value, interval)
    else:
        try:
            if start_date and end_date and start_date <= end_date:
                rows, meta, method_details = fetch_metric_rows_for_period(
                    symbol, metric_key, start_date, end_date, interval, args.timeout, args.period_type, args.range_length
                )
                range_label = f"{start_date.isoformat()}..{end_date.isoformat()}"
            else:
                rows, meta, method_details = fetch_metric_rows(
                    symbol, metric_key, range_value, interval, args.timeout, args.period_type, args.range_length
                )
                range_label = range_value
        except SystemExit as exc:
            message = str(exc)
            if not (
                metric_key in FINANCIAL_METRIC_TYPES
                and args.period_type
                and "Yahoo Finance returned no usable" in message
            ):
                raise
            payload = build_unavailable_financial_payload(args, symbol, metric_key, message)
        else:
            if method_details.get("financialPeriodType"):
                range_label = f"latest {method_details.get('actualPeriods') or len(rows)} {method_details['financialPeriodType']} periods"
            source = method_details.get("source") or "Yahoo Finance public chart API"
            payload = {
                "type": "yahoo-data",
                "source": source,
                "symbol": symbol,
                "title": args.title or f"{symbol} {metric_label(metric_key)}",
                "subtitle": range_label if method_details.get("financialPeriodType") else f"{range_label} / {interval}",
                "metric": metric_key,
                "metricLabel": metric_label(metric_key),
                "method": method_details.get("method") or "Yahoo chart values",
                "range": range_label,
                "interval": interval,
                "chartType": args.chart_type,
                "currency": meta.get("currency"),
                "exchangeName": meta.get("exchangeName"),
                "regularMarketPrice": meta.get("regularMarketPrice"),
                "rows": rows,
                "visualStyle": style_config,
                "markers": build_threshold_markers(args, rows, style_config),
                "lineChartData": {
                    "xField": "date",
                    "yField": "value",
                    "series": [{"field": "value", "label": metric_label(metric_key)}],
                    "data": [{"date": row.get("date"), "value": row.get("value")} for row in rows],
                },
                **method_details,
            }
    emit_payload_result(args, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
