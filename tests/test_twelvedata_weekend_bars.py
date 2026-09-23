"""Twelve Data 日足の土日バー除外（2026-09-23）の回帰テスト

月曜の series[1] が日曜の補間足になり、PDH/PDL が金曜の実レンジから外れていた。
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from scrapers import twelvedata  # noqa: E402


def _bar(day: date, high: float, low: float) -> dict:
    return {"datetime": day.isoformat(), "open": str(low), "high": str(high),
            "low": str(low), "close": str(high)}


def _daily_series(end: date, days: int) -> list[dict]:
    """新しい順。土日は値幅0.5の補間足、平日は日付ごとに異なる実レンジ。"""
    bars = []
    for offset in range(days):
        day = end - timedelta(days=offset)
        if day.weekday() >= 5:
            bars.append(_bar(day, 4380.5, 4380.0))
        else:
            bars.append(_bar(day, 4400.0 + offset, 4300.0 - offset))
    return bars


def _fake_get_factory(series_by_symbol: dict):
    quote = {"close": "4343.80", "previous_close": "4380.13", "change": "-36.33",
             "percent_change": "-0.829", "open": "4380.05", "high": "4384.53", "low": "4321.23"}

    def _fake_get(endpoint, params):
        if endpoint == "/quote":
            return {symbol: dict(quote) for symbol in series_by_symbol}
        if endpoint == "/time_series":
            return {symbol: {"values": values} for symbol, values in series_by_symbol.items()}
        return None
    return _fake_get


def test_weekend_trading_symbols_come_from_config():
    assert config.WEEKEND_TRADING_SYMBOLS == frozenset({"BTCUSD"})


def test_drop_weekend_bars_keeps_weekdays_and_undated_rows():
    monday = date(2026, 9, 21)
    series = _daily_series(monday, 4) + [{"high": "1", "low": "1"}]
    kept, dropped = twelvedata._drop_weekend_bars(series)
    assert dropped == 2
    assert [row.get("datetime") for row in kept] == ["2026-09-21", "2026-09-18", None]


def test_monday_pdh_pdl_use_friday_not_sunday(monkeypatch):
    monday = date(2026, 9, 21)
    series = _daily_series(monday, 90)
    monkeypatch.setattr(twelvedata, "_get", _fake_get_factory({"XAU/USD": series}))
    text, _, raw_series = twelvedata.fetch_price_data_with_raw(instruments=["XAUUSD"])

    friday = next(row for row in series if row["datetime"] == "2026-09-18")
    assert f"PDH: {float(friday['high']):,.2f} / PDL: {float(friday['low']):,.2f}" in text
    assert all(date.fromisoformat(row["datetime"]).weekday() < 5 for row in raw_series["XAUUSD"])


def test_ipda_ranges_count_trading_days_only(monkeypatch):
    monday = date(2026, 9, 21)
    series = _daily_series(monday, 90)
    monkeypatch.setattr(twelvedata, "_get", _fake_get_factory({"XAU/USD": series}))
    text, _, _ = twelvedata.fetch_price_data_with_raw(instruments=["XAUUSD"])

    weekdays = [row for row in series if date.fromisoformat(row["datetime"]).weekday() < 5]
    previous_weekdays = weekdays[1:21]
    expected_high = max(float(row["high"]) for row in previous_weekdays)
    expected_low = min(float(row["low"]) for row in previous_weekdays)
    assert len(previous_weekdays) == 20
    assert f"IPDA 20日 High/Low: {expected_high:,.2f} / {expected_low:,.2f}" in text


def test_weekend_trading_symbol_keeps_weekend_bars(monkeypatch):
    monday = date(2026, 9, 21)
    series = _daily_series(monday, 10)
    monkeypatch.setattr(twelvedata, "_get", _fake_get_factory({"BTC/USD": series}))
    _, _, raw_series = twelvedata.fetch_price_data_with_raw(instruments=["BTCUSD"])
    assert raw_series["BTCUSD"] == series
