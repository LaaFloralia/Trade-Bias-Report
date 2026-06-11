"""config.yaml 銘柄定義 SSoT の回帰テスト

G1（銘柄定義の一元化）のガード:
  1. config.yaml がロードでき、派生テーブルが期待形であること
  2. 各スクレイパーのテーブルが config 由来であること（同一オブジェクト/同値）
  3. ソース走査: 旧定義サイト 9 ファイルに銘柄リテラルの直書きが復活していないこと
  4. 原油（WTI/Brent/crude）関連の定義・参照がアクティブなコード/プロンプトにないこと
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402


def test_config_yaml_loads_expected_shape():
    assert list(config.INSTRUMENTS) == ["DXY", "XAUUSD", "USDJPY", "BTCUSD"]
    assert config.TWELVEDATA_SYMBOLS == {
        "XAUUSD": "XAU/USD",
        "USDJPY": "USD/JPY",
        "BTCUSD": "BTC/USD",
    }
    # COT は order 指定どおり GC → 6J → DX → BTC の順
    assert [t[0] for t in config.COT_TARGETS] == [
        "GOLD - GC",
        "JAPANESE YEN - 6J",
        "USD INDEX - DX",
        "BITCOIN - CME",
    ]
    assert config.FXSSI_SYMBOLS == ["XAUUSD", "USDJPY", "EURUSD", "GBPUSD"]
    assert list(config.IG_URLS) == ["XAUUSD", "USDJPY"]
    assert config.OPEN_ORDER_SYMBOLS == ["XAUUSD", "USDJPY"]
    assert len(config.DXY_COMPONENTS) == 6
    assert abs(sum(w for _, w, _ in config.DXY_COMPONENTS) - 1.0) < 0.001
    assert list(config.PREMARKET_TD_SYMBOLS) == ["SPX", "NDX", "DJI"]
    assert config.VIX_FRED_SYMBOLS == {"VIX": "VIXCLS", "VIX3M": "VXVCLS"}
    assert list(config.VIX_TD_SYMBOLS) == ["VIX9D", "VIX", "VIX3M", "VIX6M"]
    assert config.BTC_ETF_TICKERS == ["IBIT", "FBTC", "GBTC"]
    assert config.EURUSD_DXY_FACTOR == 50.14348112


def test_scraper_tables_come_from_config():
    from scrapers import (
        btc_etf,
        cot,
        dxy_components,
        ig_sentiment,
        premarket,
        twelvedata,
        vix_structure,
    )

    assert twelvedata.SYMBOL_MAP is config.TWELVEDATA_SYMBOLS
    assert cot.COT_TARGETS is config.COT_TARGETS
    assert btc_etf.TARGET_ETFS is config.BTC_ETF_TICKERS
    assert ig_sentiment.IG_URLS is config.IG_URLS
    assert dxy_components.COMPONENTS is config.DXY_COMPONENTS
    assert premarket.SYMBOLS is config.PREMARKET_TD_SYMBOLS
    assert premarket.YAHOO_URLS is config.PREMARKET_YAHOO_URLS
    assert premarket.YAHOO_CHART_SYMBOLS is config.PREMARKET_YAHOO_CHART_SYMBOLS
    assert vix_structure.TD_SYMBOLS is config.VIX_TD_SYMBOLS
    assert vix_structure.FRED_SYMBOLS is config.VIX_FRED_SYMBOLS
    assert vix_structure.CBOE_URLS is config.VIX_CBOE_URLS
    assert vix_structure.YAHOO_URLS is config.VIX_YAHOO_URLS


# 旧定義サイト → 復活してはいけない直書きリテラル
_FORBIDDEN_LITERALS = {
    "scrapers/twelvedata.py": ['"XAU/USD"', '"USD/JPY"', '"BTC/USD"'],
    "scrapers/dxy_components.py": ['("EUR/USD"', '0.576', '"USD/SEK"'],
    "scrapers/premarket.py": ['"^GSPC"', '"%5EGSPC"', '"SPX": "SPX"'],
    "scrapers/vix_structure.py": ['"VIXCLS"', '"VXVCLS"', 'cboe.com/us/indices'],
    "scrapers/cot.py": ['"GOLD - COMMODITY EXCHANGE INC."', '"BITCOIN - CHICAGO'],
    "scrapers/btc_etf.py": ['["IBIT"', '"IBIT", "FBTC"'],
    "scrapers/fxssi.py": ['["XAUUSD", "USDJPY", "EURUSD", "GBPUSD"]'],
    "scrapers/ig_sentiment.py": ['ig.com/en/commodities', 'ig.com/en/forex'],
    "scrapers/dxy.py": ['EURUSD_DXY_FACTOR = 50.', '"symbol": "EUR/USD"'],
    "main.py": ['["XAUUSD", "USDJPY"]'],
}


def test_no_hardcoded_instrument_definitions_in_scrapers():
    violations = []
    for rel_path, literals in _FORBIDDEN_LITERALS.items():
        src = (PROJECT_ROOT / rel_path).read_text(encoding="utf-8")
        for lit in literals:
            if lit in src:
                violations.append(f"{rel_path}: {lit}")
    assert not violations, (
        "銘柄定義の直書きが復活しています（config.yaml へ集約してください）:\n"
        + "\n".join(violations)
    )


def test_no_crude_oil_references_in_active_files():
    """原油関連の定義・参照が削除済みであること（AUDIT.md 等の時点記録は対象外）。"""
    targets = [
        "config.yaml",
        "config.py",
        "main.py",
        "master_prompt.md",
        "master_prompt_weekly.md",
        "master_prompt_deep.md",
        "master_prompt_deep_weekly.md",
        "README.md",
        ".claude/commands/daily-bias.md",
        ".claude/commands/weekly-bias.md",
        ".claude/commands/deep-bias.md",
        ".claude/commands/deep-bias-weekly.md",
    ] + [str(p.relative_to(PROJECT_ROOT)) for p in (PROJECT_ROOT / "scrapers").glob("*.py")]

    violations = []
    for rel_path in targets:
        src = (PROJECT_ROOT / rel_path).read_text(encoding="utf-8")
        for kw in ("WTI", "Brent", "crude", "USOIL", "原油"):
            if kw in src:
                violations.append(f"{rel_path}: {kw}")
    assert not violations, "原油関連の参照が残存: " + ", ".join(violations)
