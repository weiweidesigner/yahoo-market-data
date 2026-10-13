from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from urllib.error import HTTPError


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "app" / "skills" / "yahoo-data" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import yfinance_safe  # noqa: E402


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def test_yahoo_json_tries_query2_after_query1_rate_limit(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[str, float]] = []

    def fake_urlopen(request, timeout: float):
        calls.append((request.full_url, timeout))
        if "query1.finance.yahoo.com" in request.full_url:
            raise HTTPError(request.full_url, 429, "Too Many Requests", {}, io.BytesIO())
        return _Response({"chart": {"result": [{"meta": {"symbol": "^GSPC"}}]}})

    monkeypatch.setattr(yfinance_safe, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(yfinance_safe.urllib.request, "urlopen", fake_urlopen)

    payload = yfinance_safe.yahoo_json(
        "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?range=5d&interval=1d",
        "test failover",
        timeout=7,
    )

    assert payload["chart"]["result"][0]["meta"]["symbol"] == "^GSPC"
    assert ["query1.finance.yahoo.com" in url for url, _ in calls] == [True, False]
    assert [timeout for _, timeout in calls] == [7, 7]


def test_yahoo_json_limits_rate_limit_backoff_before_agent_fallback(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []
    sleeps: list[float] = []

    def fake_urlopen(request, timeout: float):
        calls.append(request.full_url)
        raise HTTPError(request.full_url, 429, "Too Many Requests", {}, io.BytesIO())

    monkeypatch.setattr(yfinance_safe, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(yfinance_safe.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(yfinance_safe.time, "sleep", sleeps.append)
    monkeypatch.setattr(yfinance_safe, "_nasdaq_chart_fallback", lambda _url, _timeout: None)

    try:
        yfinance_safe.yahoo_json(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?range=5d&interval=1d",
            "test bounded retries",
        )
    except RuntimeError as exc:
        assert "429" in str(exc)
    else:
        raise AssertionError("Yahoo failure should be surfaced for agent fallback")

    assert len(calls) == 4
    assert sleeps == [2]


def test_yahoo_json_uses_and_caches_nasdaq_fallback_after_429(monkeypatch, tmp_path: Path) -> None:
    fallback_payload = {"chart": {"result": [{"meta": {"_aimeSource": "Nasdaq historical API"}}]}}

    def rate_limited(request, timeout: float):
        raise HTTPError(request.full_url, 429, "Too Many Requests", {}, io.BytesIO())

    monkeypatch.setattr(yfinance_safe, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(yfinance_safe.urllib.request, "urlopen", rate_limited)
    monkeypatch.setattr(yfinance_safe.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(yfinance_safe, "_nasdaq_chart_fallback", lambda _url, _timeout: fallback_payload)
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?range=5d&interval=1d"

    assert yfinance_safe.yahoo_json(url, "fallback") == fallback_payload

    monkeypatch.setattr(
        yfinance_safe,
        "_nasdaq_chart_fallback",
        lambda _url, _timeout: (_ for _ in ()).throw(AssertionError("cache was not used")),
    )
    assert yfinance_safe.yahoo_json(url, "cached fallback") == fallback_payload


def test_nasdaq_fallback_supports_ordinary_us_stock(monkeypatch) -> None:
    payload = {
        "data": {
            "tradesTable": {
                "rows": [
                    {
                        "date": "09/21/2026",
                        "close": "$501.61",
                        "volume": "27,959,940",
                        "open": "$494.945",
                        "high": "$501.87",
                        "low": "$491.33",
                    }
                ]
            }
        }
    }
    calls: list[str] = []

    def fake_urlopen(request, timeout: float):
        calls.append(request.full_url)
        return _Response(payload)

    monkeypatch.setattr(yfinance_safe.urllib.request, "urlopen", fake_urlopen)
    result = yfinance_safe._nasdaq_chart_fallback(
        "https://query1.finance.yahoo.com/v8/finance/chart/MSFT?range=5d&interval=1d",
        7,
    )

    meta = result["chart"]["result"][0]["meta"]
    assert meta["symbol"] == "MSFT"
    assert meta["_aimeSource"] == "Nasdaq historical API"
    assert meta["_aimeMethod"] == "US-listed stock daily history fallback"
    assert meta["_aimeAdjustedCloseAvailable"] is False
    assert result["chart"]["result"][0]["indicators"]["adjclose"][0]["adjclose"] == [None]
    assert "assetclass=stocks" in calls[0]


def test_nasdaq_fallback_does_not_guess_non_us_symbol(monkeypatch) -> None:
    monkeypatch.setattr(
        yfinance_safe.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network should not be called")),
    )
    assert yfinance_safe._nasdaq_chart_fallback(
        "https://query1.finance.yahoo.com/v8/finance/chart/0700.HK?range=5d&interval=1d",
        7,
    ) is None
