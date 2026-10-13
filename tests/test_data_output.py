import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "app/skills/yahoo-data/scripts"
sys.path.insert(0, str(SCRIPTS))
import yahoo_data


def test_cli_writes_json_without_rendering(monkeypatch, tmp_path, capsys):
    target = tmp_path / "prices.json"
    monkeypatch.setattr(sys, "argv", ["yahoo_data.py", "--code", "AAPL", "--range", "1mo", "--payload-output", str(target)])
    monkeypatch.setattr(yahoo_data, "fetch_metric_rows", lambda *args: (
        [{"date": "2026-01-02", "value": 100}], {"currency": "USD", "exchangeName": "NASDAQ"}, {"method": "offline fixture"}
    ))
    assert yahoo_data.main() == 0
    status = json.loads(capsys.readouterr().out)
    payload = json.loads(target.read_text())
    assert status["rendered"] is False
    assert payload["rows"][0]["value"] == 100
    assert payload["method"] == "offline fixture"
    assert not list(tmp_path.glob("*.html"))


def test_unsupported_metric_is_not_silently_changed():
    import pytest
    with pytest.raises(SystemExit):
        yahoo_data.normalize_metric("made_up_metric")
