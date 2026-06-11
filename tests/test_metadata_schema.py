"""scrapers/metadata_schema.py の単体テスト。

非破壊・冪等の補完仕様と、main.collect_all_data() が返す形に近いネスト構造
への適用を網羅する。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scrapers.metadata_schema import (  # noqa: E402
    COMMON_FIELDS,
    empty_metadata,
    ensure_metadata,
    normalize_scraper_results,
    now_utc_iso,
)


# ---------- now_utc_iso / empty_metadata ----------

def test_now_utc_iso_matches_fred_format():
    s = now_utc_iso()
    assert s.endswith("Z")
    # 例: 2026-05-19T12:34:56Z
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", s)


def test_empty_metadata_has_all_common_fields():
    meta = empty_metadata("MyFXBook", symbol="XAUUSD")
    for f in COMMON_FIELDS:
        assert f in meta
    assert meta["source"] == "MyFXBook"
    assert meta["symbol"] == "XAUUSD"
    assert meta["stale"] is False
    assert meta["fallback_used"] is False
    assert meta["error"] is None


# ---------- ensure_metadata ----------

def test_ensure_metadata_fills_missing_keys_only():
    d = {"source": "CoinGlass", "symbol": "BTCUSD", "long_pct": 65.2, "error": None}
    out = ensure_metadata(d, source="ignored_because_already_set", symbol="ignored")
    assert out is d  # in-place + 同参照を return
    assert out["source"] == "CoinGlass"     # 既存値は尊重
    assert out["symbol"] == "BTCUSD"         # 既存値は尊重
    assert out["long_pct"] == 65.2
    # 欠けていたメタデータは補完される
    assert out["timestamp"].endswith("Z")
    assert out["as_of_date"] is None
    assert out["stale"] is False
    assert out["fallback_used"] is False
    assert out["error"] is None
    assert out["note"] is None


def test_ensure_metadata_does_not_overwrite_existing_error_and_note():
    d = {"source": "FRED", "error": "HTTP 429", "note": "rate limited", "stale": True, "fallback_used": True}
    out = ensure_metadata(d)
    assert out["error"] == "HTTP 429"
    assert out["note"] == "rate limited"
    assert out["stale"] is True
    assert out["fallback_used"] is True


def test_ensure_metadata_idempotent():
    d = {"source": "MyFXBook", "symbol": "USDJPY"}
    a = ensure_metadata(d)
    snapshot = dict(a)
    b = ensure_metadata(d)  # 二度目
    assert a is b
    assert b == snapshot


def test_ensure_metadata_passes_through_non_dict():
    assert ensure_metadata(None) is None
    assert ensure_metadata("just a string") == "just a string"
    assert ensure_metadata([1, 2, 3]) == [1, 2, 3]


def test_ensure_metadata_uses_hint_when_source_missing():
    d = {"long_pct": 55.0}
    out = ensure_metadata(d, source="FXSSI", symbol="XAUUSD")
    assert out["source"] == "FXSSI"
    assert out["symbol"] == "XAUUSD"


# ---------- normalize_scraper_results ----------

def test_normalize_scraper_results_skips_non_dict_entries():
    """price_data (str) / timestamp (str) はスキップされ、破壊しない。"""
    results = {
        "timestamp": "2026-05-19T00:00:00",
        "price_data": "整形済みテキスト...",
        "dxy": {"error": "boom"},
    }
    normalize_scraper_results(results)
    assert results["timestamp"] == "2026-05-19T00:00:00"
    assert results["price_data"] == "整形済みテキスト..."
    # dxy は補完される
    assert results["dxy"]["source"] == "DXY scraper"
    assert results["dxy"]["symbol"] == "DXY"
    assert results["dxy"]["stale"] is False


def test_normalize_scraper_results_handles_nested_retail_sentiment():
    results = {
        "timestamp": "2026-05-19T00:00:00",
        "retail_sentiment": {
            "XAUUSD": {"source": "MyFXBook", "long_pct": 30, "short_pct": 70},
            "USDJPY": {"long_pct": None, "error": "ig fetch failed"},
        },
    }
    normalize_scraper_results(results)
    # XAUUSD: source 維持、symbol 補完、メタ補完
    assert results["retail_sentiment"]["XAUUSD"]["source"] == "MyFXBook"
    assert results["retail_sentiment"]["XAUUSD"]["symbol"] == "XAUUSD"
    assert results["retail_sentiment"]["XAUUSD"]["fallback_used"] is False
    # USDJPY: source は引数 hint が None なのでセットされない
    assert results["retail_sentiment"]["USDJPY"]["symbol"] == "USDJPY"
    assert results["retail_sentiment"]["USDJPY"]["error"] == "ig fetch failed"


def test_normalize_scraper_results_handles_fred_subdict():
    """fred は既に完全準拠だが、追加で適用しても破壊しない。"""
    results = {
        "timestamp": "2026-05-19T00:00:00",
        "fred": {
            "DGS10": {
                "source": "FRED", "series_id": "DGS10",
                "value": 4.41, "prev_value": 4.36, "change": 0.05,
                "as_of_date": "2026-05-07", "prev_as_of_date": "2026-05-06",
                "timestamp": "2026-05-10T00:00:00Z",
                "stale": False, "fallback_used": False, "error": None,
            },
        },
    }
    before = dict(results["fred"]["DGS10"])
    normalize_scraper_results(results)
    after = results["fred"]["DGS10"]
    # 既存キーは一切上書きされない（symbol は欠けていたので追加される）
    for k, v in before.items():
        assert after[k] == v
    assert after["symbol"] == "DGS10"
    assert after["note"] is None  # 補完された


def test_normalize_scraper_results_coinglass_and_open_orders():
    results = {
        "timestamp": "2026-05-19T00:00:00",
        "coinglass": {"BTCUSD": {"long_pct": 65.2, "error": None}},
        "myfxbook_open_orders": {"XAUUSD": {"bid_count": 100, "ask_count": 80, "error": None}},
    }
    normalize_scraper_results(results)
    cg = results["coinglass"]["BTCUSD"]
    oo = results["myfxbook_open_orders"]["XAUUSD"]
    assert cg["source"] == "CoinGlass"
    assert cg["symbol"] == "BTCUSD"
    assert cg["fallback_used"] is False
    assert oo["source"] == "MyFXBook Open Orders"
    assert oo["symbol"] == "XAUUSD"
    assert oo["timestamp"].endswith("Z")
