"""FRED 20 観測トレンド (change_20obs) の計算と scraped_data への出力を検証する。
master_prompt.md セクション1.5 (ファンダ大局バイアス) の中期トレンド判定入力。"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scrapers import fred  # noqa: E402
from main import format_scraped_data  # noqa: E402


def test_change_20obs_computed(monkeypatch):
    def _fake_fetch_series_observations(series_id, api_key):
        return {
            "value": 2.30,
            "as_of_date": "2026-07-07",
            "prev_value": 2.24,
            "prev_as_of_date": "2026-07-06",
            "value_20obs_ago": 2.19,
            "as_of_20obs_ago": "2026-06-05",
        }

    monkeypatch.setattr(fred, "_fetch_series_observations", _fake_fetch_series_observations)
    r = fred.fetch_fred_series("DFII10", api_key="fake_key")
    assert r["value_20obs_ago"] == 2.19
    assert r["as_of_20obs_ago"] == "2026-06-05"
    assert abs(r["change_20obs"] - 0.11) < 1e-9


def test_change_20obs_none_when_history_short(monkeypatch):
    """有効観測が 21 件未満 (新設系列等) でも落ちずに None を返す。"""
    def _fake_fetch_series_observations(series_id, api_key):
        return {
            "value": 2.30,
            "as_of_date": "2026-07-07",
            "prev_value": 2.24,
            "prev_as_of_date": "2026-07-06",
            "value_20obs_ago": None,
            "as_of_20obs_ago": None,
        }

    monkeypatch.setattr(fred, "_fetch_series_observations", _fake_fetch_series_observations)
    r = fred.fetch_fred_series("DFII10", api_key="fake_key")
    assert r["change_20obs"] is None


def _fred_entry(series_id, value, change_20obs):
    return {
        "source": "FRED", "series_id": series_id,
        "value": value, "prev_value": value - 0.05, "change": 0.05,
        "value_20obs_ago": (value - change_20obs) if change_20obs is not None else None,
        "as_of_20obs_ago": "2026-06-05" if change_20obs is not None else None,
        "change_20obs": change_20obs,
        "as_of_date": "2026-07-07", "prev_as_of_date": "2026-07-06",
        "stale": False, "fallback_used": False,
    }


def test_format_emits_20d_trend_line():
    data = {
        "timestamp": "2026-07-08T09:00:00",
        "fred": {
            "DGS10": _fred_entry("DGS10", 4.48, 0.02),      # |0.02| <= 0.05 → 横ばい
            "DGS2": _fred_entry("DGS2", 4.17, -0.10),        # < -0.05 → 低下
            "DFII10": _fred_entry("DFII10", 2.30, 0.11),     # > +0.05 → 上昇
            "T10YIE": _fred_entry("T10YIE", 2.23, None),     # 履歴不足 → 表記なし
            "DTWEXBGS": _fred_entry("DTWEXBGS", 120.887, -0.45),
        },
    }
    text = format_scraped_data(data)
    assert "20営業日比: +0.110 (トレンド: 上昇)" in text
    assert "20営業日比: -0.100 (トレンド: 低下)" in text
    assert "20営業日比: +0.020 (トレンド: 横ばい)" in text
    # Broad USD Index は数値のみ (トレンドラベルなし)
    assert "20営業日比: -0.450" in text
    assert "20営業日比: -0.450 (トレンド" not in text
    # T10YIE は履歴不足でもエラーにならず、20営業日比の表記を出さない
    t10_line = next(line for line in text.splitlines() if line.startswith("現在利回り: 2.230%"))
    assert "20営業日比" not in t10_line
