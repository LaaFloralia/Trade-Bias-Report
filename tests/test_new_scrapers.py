"""新規 Deep Bias 強化スクレイパー群のスモークテスト。

ネットワーク依存をできる限り排除し、戻り値スキーマと整形ロジックを検証する。
実ネットワーク呼び出しが必要なものは monkeypatch でフェイクを注入する。

実行:
    .venv/bin/python3 -m pytest tests/test_new_scrapers.py -v
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scrapers import (  # noqa: E402
    crypto_funding,
    dxy_components,
    macro_liquidity,
    myfxbook_open_orders,
    premarket,
    rate_spreads,
    vix_structure,
)


# ---------- DXY components ----------

def test_dxy_components_schema_with_mocked_quotes(monkeypatch):
    """全構成通貨の quote が揃った状態でのスキーマと寄与計算を検証。"""

    def _fake_fetch():
        return {
            "EUR/USD": {"close": "1.08", "percent_change": "-0.10"},  # USD bull (-1 * -0.10 = +0.10)
            "USD/JPY": {"close": "157.0", "percent_change": "+0.20"},
            "GBP/USD": {"close": "1.27", "percent_change": "-0.05"},
            "USD/CAD": {"close": "1.39", "percent_change": "+0.10"},
            "USD/SEK": {"close": "10.40", "percent_change": "+0.15"},
            "USD/CHF": {"close": "0.91", "percent_change": "+0.05"},
        }

    monkeypatch.setattr(dxy_components, "_fetch_quotes", _fake_fetch)
    result = asyncio.run(dxy_components.scrape_dxy_components())
    assert result["error"] is None
    assert result["estimated_dxy_change_pct"] is not None
    assert result["leading_driver"] in {c[0] for c in dxy_components.COMPONENTS}
    assert len(result["components"]) == 6
    # EUR/USD wt 0.576, change -0.10 (inverse → +0.10), 寄与 +0.0576
    eur = next(c for c in result["components"] if c["symbol"] == "EUR/USD")
    assert eur["dxy_contribution"] == pytest.approx(0.0576, abs=1e-4)


def test_dxy_components_handles_missing_quote(monkeypatch):
    """quote が一部欠損していても落ちず、欠損は error フィールドに記録される。"""
    monkeypatch.setattr(dxy_components, "_fetch_quotes", lambda: {"EUR/USD": {"close": "1.08", "percent_change": "0.0"}})
    result = asyncio.run(dxy_components.scrape_dxy_components())
    # EUR/USD 1 件分は寄与計算済み、他は error 付き
    error_components = [c for c in result["components"] if c.get("error")]
    assert len(error_components) >= 5


# ---------- VIX structure ----------

def test_vix_structure_classifier_contango():
    judg = vix_structure._classify_term_structure({"VIX": 15.0, "VIX3M": 18.0, "VIX9D": 14.5})
    assert judg["term_structure"] == "contango"
    assert judg["vix_level_regime"] == "normal"


def test_vix_structure_classifier_backwardation():
    judg = vix_structure._classify_term_structure({"VIX": 32.0, "VIX3M": 24.0, "VIX9D": 35.0})
    assert judg["term_structure"] == "backwardation"
    assert judg["vix_level_regime"] == "panic"
    assert judg["short_term_event_alert"] is True


def test_vix_structure_handles_no_data(monkeypatch):
    """FRED / CBOE / Twelve Data / Yahoo すべて空を返した時の error 応答を検証。"""
    async def _none_async(*args, **kwargs):
        return None

    monkeypatch.setattr(vix_structure, "_fetch_fred_vix", _none_async)
    monkeypatch.setattr(vix_structure, "_fetch_cboe_dashboard", _none_async)
    monkeypatch.setattr(vix_structure, "_fetch_twelvedata_batch", lambda: None)
    monkeypatch.setattr(vix_structure, "_fetch_yahoo", _none_async)

    result = asyncio.run(vix_structure.scrape_vix_structure())
    assert result["error"] is not None
    assert result["values"] == {}


# ---------- Premarket ----------

def test_premarket_risk_regime_risk_on(monkeypatch):
    def _fake_quotes():
        return {
            "SPX": {"close": "5000", "previous_close": "4980", "change": "20", "percent_change": "0.40",
                    "open": "4985", "high": "5010", "low": "4982"},
            "NDX": {"close": "20100", "previous_close": "20000", "change": "100", "percent_change": "0.50",
                    "open": "20020", "high": "20150", "low": "20010"},
            "DJI": {"close": "39200", "previous_close": "39100", "change": "100", "percent_change": "0.25",
                    "open": "39120", "high": "39250", "low": "39110"},
        }

    monkeypatch.setattr(premarket, "_fetch_quotes", _fake_quotes)
    result = asyncio.run(premarket.scrape_premarket())
    assert result["error"] is None
    assert result["risk_regime"] == "risk-on"
    assert "SPX" in result["indices"]
    assert result["indices"]["SPX"]["change_pct"] == pytest.approx(0.40, abs=1e-3)


def test_premarket_risk_regime_mixed(monkeypatch):
    def _fake_quotes():
        return {
            "SPX": {"close": "5000", "previous_close": "5050", "change": "-50", "percent_change": "-1.0",
                    "open": "5040", "high": "5045", "low": "4995"},
            "NDX": {"close": "20100", "previous_close": "20000", "change": "100", "percent_change": "0.50",
                    "open": "20020", "high": "20150", "low": "20010"},
            "DJI": {"close": "39200", "previous_close": "39100", "change": "100", "percent_change": "0.25",
                    "open": "39120", "high": "39250", "low": "39110"},
        }

    monkeypatch.setattr(premarket, "_fetch_quotes", _fake_quotes)
    result = asyncio.run(premarket.scrape_premarket())
    assert result["risk_regime"] == "mixed"


# ---------- Macro liquidity ----------

def test_macro_liquidity_net_calculation(monkeypatch):
    def _fake_fetch(series_id, api_key=None):
        # WALCL: 6,800,000 M USD = 6,800 B; RRP: 100 B; TGA: 700,000 M USD = 700 B
        # Net = 6800 - 100 - 700 = 6000 B
        # prev: 6,750,000 M; 110 B; 750,000 M
        # prev Net = 6750 - 110 - 750 = 5890 B → change = +110 B
        canned = {
            "WALCL":     {"value": 6_800_000, "prev_value": 6_750_000, "change": 50_000, "stale": False, "as_of_date": "2026-05-08", "prev_as_of_date": "2026-05-01"},
            "RRPONTSYD": {"value": 100.0, "prev_value": 110.0, "change": -10.0, "stale": False, "as_of_date": "2026-05-13", "prev_as_of_date": "2026-05-12"},
            "WTREGEN":   {"value": 700_000, "prev_value": 750_000, "change": -50_000, "stale": False, "as_of_date": "2026-05-08", "prev_as_of_date": "2026-05-01"},
        }
        d = canned[series_id]
        return {"source": "FRED", "series_id": series_id, **d, "error": None, "fallback_used": False}

    monkeypatch.setattr(macro_liquidity, "fetch_fred_series", _fake_fetch)
    monkeypatch.setattr(macro_liquidity, "get_fred_api_key", lambda: "fake_key")

    result = asyncio.run(macro_liquidity.scrape_macro_liquidity())
    assert result["error"] is None
    assert result["net_liquidity_b"] == pytest.approx(6000.0, abs=0.01)
    assert result["net_liquidity_change_b"] == pytest.approx(110.0, abs=0.01)
    assert result["regime"] == "expansion"


def test_macro_liquidity_no_api_key(monkeypatch):
    monkeypatch.setattr(macro_liquidity, "get_fred_api_key", lambda: None)
    result = asyncio.run(macro_liquidity.scrape_macro_liquidity())
    assert "FRED_API_KEY" in result["error"]


# ---------- Rate spreads ----------

def test_rate_spreads_calculation(monkeypatch):
    def _fake_fetch(series_id, api_key=None):
        canned = {
            "DGS10":            {"value": 4.42, "prev_value": 4.38, "stale": False, "as_of_date": "2026-05-13", "prev_as_of_date": "2026-05-12"},
            "IRLTLT01DEM156N":  {"value": 2.55, "prev_value": 2.52, "stale": True,  "as_of_date": "2026-04-01", "prev_as_of_date": "2026-03-01"},
            "IRLTLT01JPM156N":  {"value": 1.50, "prev_value": 1.48, "stale": True,  "as_of_date": "2026-04-01", "prev_as_of_date": "2026-03-01"},
            "IRLTLT01GBM156N":  {"value": 4.10, "prev_value": 4.08, "stale": True,  "as_of_date": "2026-04-01", "prev_as_of_date": "2026-03-01"},
            "IRLTLT01CAM156N":  {"value": 3.40, "prev_value": 3.38, "stale": True,  "as_of_date": "2026-04-01", "prev_as_of_date": "2026-03-01"},
        }
        d = canned[series_id]
        return {"source": "FRED", "series_id": series_id, **d, "error": None, "fallback_used": False, "change": None}

    monkeypatch.setattr(rate_spreads, "fetch_fred_series", _fake_fetch)
    monkeypatch.setattr(rate_spreads, "get_fred_api_key", lambda: "fake_key")

    result = asyncio.run(rate_spreads.scrape_rate_spreads())
    assert result["error"] is None
    by_pair = {s["pair"]: s for s in result["spreads"]}
    assert by_pair["US-JP"]["spread"] == pytest.approx(2.92, abs=1e-3)
    assert by_pair["US-DE"]["spread"] == pytest.approx(1.87, abs=1e-3)
    assert by_pair["US-JP"]["stale"] is True


# ---------- Crypto funding ----------

def test_crypto_funding_aggregates(monkeypatch):
    monkeypatch.setattr(crypto_funding, "_binance", lambda: {"exchange": "Binance", "funding_rate": 0.012, "next_funding_time": None, "mark_price": 80000})
    monkeypatch.setattr(crypto_funding, "_bybit",   lambda: {"exchange": "Bybit",   "funding_rate": 0.015, "next_funding_time": None, "mark_price": 80010})
    monkeypatch.setattr(crypto_funding, "_okx",     lambda: {"exchange": "OKX",     "funding_rate": 0.011, "next_funding_time": None, "mark_price": None})

    result = asyncio.run(crypto_funding.scrape_crypto_funding())
    assert result["error"] is None
    assert result["average_funding_rate"] == pytest.approx((0.012 + 0.015 + 0.011) / 3, abs=1e-5)
    assert result["max_dispersion"] == pytest.approx(0.004, abs=1e-5)
    assert result["regime"] == "long crowded"


def test_crypto_funding_all_unreachable(monkeypatch):
    monkeypatch.setattr(crypto_funding, "_binance", lambda: None)
    monkeypatch.setattr(crypto_funding, "_bybit",   lambda: None)
    monkeypatch.setattr(crypto_funding, "_okx",     lambda: None)

    result = asyncio.run(crypto_funding.scrape_crypto_funding())
    assert result["error"] == "all 3 exchanges unreachable"
    assert result["regime"] == "unknown"


# ---------- MyFXBook Open Orders summarizer ----------

def test_open_orders_summarizer_picks_max_concentration():
    """旧スキーマ互換ヘルパー (Buy Stop / Sell Stop ラベル形式)。"""
    buckets = [
        {"type": "Buy Stop",  "low": 1.20, "high": 1.21, "share_pct": 5.0},
        {"type": "Buy Stop",  "low": 1.22, "high": 1.23, "share_pct": 18.0},   # 最大 BSL
        {"type": "Sell Stop", "low": 1.10, "high": 1.11, "share_pct": 12.0},   # 最大 SSL
        {"type": "Sell Stop", "low": 1.08, "high": 1.09, "share_pct": 7.0},
        {"type": "Buy Limit", "low": 1.18, "high": 1.19, "share_pct": 8.0},
    ]
    agg = myfxbook_open_orders._summarize_buckets(buckets)
    assert agg["bsl_concentration"]["share_pct"] == 18.0
    assert agg["ssl_concentration"]["share_pct"] == 12.0


def test_open_orders_summarizer_handles_empty():
    agg = myfxbook_open_orders._summarize_buckets([])
    assert agg["bsl_concentration"] is None
    assert agg["ssl_concentration"] is None


def test_orderbook_pair_extractor_parses_volume_price_pairs():
    """新パーサー: MyFXBook Order Book セクションから (volume, price) ペアを抽出。"""
    body = (
        "Some other content...\n"
        "Order Book (Positions)\n"
        "\nChart\n\n"
        "47\n3,268.04\n"
        "46\n4,707.94\n"
        "29\n5,175.77\n"
        "Asks\nPrice\nBids\n"
        "End of interactive chart.\n"
        "Top Forex Brokers\n"
    )
    bids, asks = myfxbook_open_orders._parse_orderbook_pairs(body)
    # 3 ペアが取れて、半分で割って前半=bids, 後半=asks
    assert len(bids) + len(asks) == 3
    all_pairs = bids + asks
    prices = sorted([p["price"] for p in all_pairs])
    assert prices == [3268.04, 4707.94, 5175.77]
    volumes = sorted([p["volume"] for p in all_pairs])
    assert volumes == [29, 46, 47]


def test_orderbook_cluster_classification_above_below_current():
    """current_price を境に BSL (上方) / SSL (下方) クラスタを分類。"""
    bids = [
        {"volume": 50, "price": 100.0},   # SSL
        {"volume": 30, "price": 100.2},   # SSL (近接、同クラスタ候補)
        {"volume": 10, "price": 90.0},    # SSL 下方
    ]
    asks = [
        {"volume": 40, "price": 102.0},   # BSL
        {"volume": 25, "price": 102.3},   # BSL (近接)
        {"volume": 8, "price": 110.0},    # BSL 上方
    ]
    cls = myfxbook_open_orders._cluster_and_classify(bids, asks, current_price=101.0, cluster_pct=0.005)
    # 102.0 と 102.3 はクラスタ閾値 0.5% (=±0.51) を超えるので別クラスタになる可能性高
    # SSL 100.0 と 100.2 は ±0.5 = ~0.5% 内で同クラスタ
    assert len(cls["bsl_candidates"]) >= 1
    assert len(cls["ssl_candidates"]) >= 1
    # トップ SSL は volume_sum が最大
    top_ssl = cls["ssl_candidates"][0]
    assert top_ssl["volume_sum"] >= 30  # 100.0 + 100.2 集約 = 80 程度を期待


def test_extract_current_price_from_market_depth_section():
    body = (
        "Some other content...\n"
        "Market Depth (Lots)\n"
        "Chart\n"
        "4,696.61\n4,696.91\n4,697.21\n4,697.51\n4,697.81\n"
        "Mid-Market price\n"
        "End of interactive chart.\n"
    )
    cp = myfxbook_open_orders._extract_current_price(body, "XAUUSD")
    assert cp is not None
    # 中央値あたり (4697 付近) を期待
    assert 4696.0 < cp < 4698.0


# ---- 2026-08 修理分: 妥当性フィルタ / share_pct / MyFXBook 文言変更対応 ----

def test_orderbook_plausibility_filter_drops_far_clusters():
    """現在価格 ±10% 超の異常値（軸ラベル混入等）はクラスタから除外される。

    実例: 2026-08-11 の XAUUSD で現値 4411 に対し 5175.77 の BSL クラスタが出力された。
    """
    bids = [{"volume": 16, "price": 4346.12}]
    asks = [
        {"volume": 94, "price": 4372.04},
        {"volume": 20, "price": 5175.77},   # +17.3% → 除外対象
        {"volume": 13, "price": 5175.78},   # 同上
        {"volume": 10, "price": 4400.00},
    ]
    cls = myfxbook_open_orders._cluster_and_classify(bids, asks, current_price=4396.0)
    all_prices = [
        c[k] for c in cls["bsl_candidates"] + cls["ssl_candidates"] for k in ("low", "high")
    ]
    assert all(p < 5000 for p in all_prices), f"5175 系の異常クラスタが残存: {all_prices}"
    # 4400 (上方 +0.1%) は BSL として残る
    assert any(c["low"] <= 4400.0 <= c["high"] for c in cls["bsl_candidates"])


def test_orderbook_share_pct_is_true_percentage():
    """share_pct はサイド内総ボリュームに対する百分率（旧実装は volume 生値を格納していた）。"""
    bids = [
        {"volume": 75, "price": 98.0},
        {"volume": 25, "price": 95.0},
    ]
    asks = [{"volume": 60, "price": 103.0}]
    cls = myfxbook_open_orders._cluster_and_classify(bids, asks, current_price=100.0)
    ssl_top = cls["ssl_candidates"][0]
    assert ssl_top["share_pct"] == 75.0  # 75 / (75+25) * 100
    bsl_top = cls["bsl_candidates"][0]
    assert bsl_top["share_pct"] == 100.0


def test_myfxbook_parses_2026_08_copy_with_percent_sign():
    """2026-08 の実ページ文言（% 付き）から pct / 平均価格 / 建玉実数を抽出できる。"""
    from scrapers import myfxbook

    page_text = (
        "XAUUSD Forex Sentiment\n\n"
        "59% of the forex traders are currently going short with XAU/USD, "
        "with an average price of 4136.0708, meanwhile 41% of the forex traders "
        "are going long with XAU/USD, with an average price of 4506.4624.\n\n"
        "Current Metrics\n"
        "Symbol\tAction\tPercentage\tVolume\tPositions\n"
        "XAUUSD\n"
        "Short\t59 %\t1,226.16 lots\t6,956\n"
        "Long\t41 %\t841.40 lots\t7,775\n"
    )
    result = {
        "short_pct": None, "long_pct": None,
        "avg_short_entry": None, "avg_long_entry": None,
        "short_volume_lots": None, "long_volume_lots": None,
        "short_positions": None, "long_positions": None,
    }
    myfxbook._parse_outlook_text(page_text, result)
    assert result["short_pct"] == 59.0
    assert result["long_pct"] == 41.0
    assert result["avg_short_entry"] == 4136.0708
    assert result["avg_long_entry"] == 4506.4624
    assert result["short_volume_lots"] == 1226.16
    assert result["long_volume_lots"] == 841.40
    assert result["short_positions"] == 6956
    assert result["long_positions"] == 7775


def test_myfxbook_parses_legacy_copy_without_percent_sign():
    """旧文言（% なし）でも従来どおり抽出できる（後方互換）。"""
    from scrapers import myfxbook

    page_text = (
        "62 of the forex traders are currently going short with USD/JPY, "
        "with an average price of 155.1234, meanwhile 38 of the forex traders "
        "are going long with USD/JPY, with an average price of 158.9876."
    )
    result = {
        "short_pct": None, "long_pct": None,
        "avg_short_entry": None, "avg_long_entry": None,
        "short_volume_lots": None, "long_volume_lots": None,
        "short_positions": None, "long_positions": None,
    }
    myfxbook._parse_outlook_text(page_text, result)
    assert result["short_pct"] == 62.0
    assert result["avg_short_entry"] == 155.1234
    assert result["long_pct"] == 38.0
    assert result["avg_long_entry"] == 158.9876
