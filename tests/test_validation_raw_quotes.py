from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scrapers import twelvedata  # noqa: E402
from scrapers.validation import validate_all  # noqa: E402


def test_twelvedata_raws_enable_non_dxy_price_validation(monkeypatch):
    def _fake_get(endpoint, params):
        if endpoint == "/quote":
            return {
                "XAU/USD": {
                    "close": "2350.00",
                    "previous_close": "2340.00",
                    "change": "10.00",
                    "percent_change": "0.427",
                    "open": "2341.00",
                    "high": "2355.00",
                    "low": "2335.00",
                },
                "USD/JPY": {
                    "close": "157.00",
                    "previous_close": "156.80",
                    "change": "0.20",
                    "percent_change": "0.128",
                    "open": "156.70",
                    "high": "157.20",
                    "low": "156.50",
                },
                "BTC/USD": {
                    "close": "67000.00",
                    "previous_close": "66500.00",
                    "change": "500.00",
                    "percent_change": "0.752",
                    "open": "66550.00",
                    "high": "67200.00",
                    "low": "66200.00",
                },
            }
        if endpoint == "/time_series":
            return {
                "XAU/USD": {"values": [
                    {"high": "2355.00", "low": "2335.00"},
                    {"high": "2348.00", "low": "2332.00"},
                ]},
                "USD/JPY": {"values": [
                    {"high": "157.20", "low": "156.50"},
                    {"high": "156.40", "low": "156.90"},
                ]},
                "BTC/USD": {"values": [
                    {"high": "67200.00", "low": "66200.00"},
                    {"high": "66800.00", "low": "66000.00"},
                ]},
            }
        return None

    monkeypatch.setattr(twelvedata, "_get", _fake_get)

    price_text, raw_quotes, raw_series = twelvedata.fetch_price_data_with_raw()
    scraped_data = {"price_data": price_text}
    for sym, quote in raw_quotes.items():
        if quote:
            scraped_data[f"_raw_quote_{sym}"] = quote
            scraped_data[f"_raw_series_{sym}"] = raw_series.get(sym, [])

    validation = validate_all(scraped_data)

    assert "_raw_quote_USDJPY" in scraped_data
    assert "_raw_series_USDJPY" in scraped_data
    assert raw_quotes["USDJPY"]["close"] == "157.00"
    assert raw_series["USDJPY"][1]["high"] == "156.40"
    assert "USDJPY" in validation
    assert any("PDH/PDL" in issue and "逆転" in issue for issue in validation["USDJPY"])
