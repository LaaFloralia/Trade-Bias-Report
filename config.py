"""プロジェクト共通設定

銘柄・シンボル・URL の定義は config.yaml（SSoT）から読み込む。
このモジュールは YAML をロードし、各スクレイパーが使う形に整形して公開する。
銘柄の追加・変更は config.yaml の編集だけで完結させること
（コードへの直書きは tests/test_config_ssot.py が検出する）。
"""

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "")

# --- 銘柄定義 SSoT のロード ---
_CONFIG_YAML = Path(__file__).parent / "config.yaml"
if not _CONFIG_YAML.exists():
    raise FileNotFoundError(
        f"config.yaml が見つかりません: {_CONFIG_YAML}\n"
        "銘柄定義 SSoT が必須です（リポジトリ直下の config.yaml を復元してください）。"
    )
with _CONFIG_YAML.open(encoding="utf-8") as _f:
    _CFG = yaml.safe_load(_f)

# メイン 4 銘柄の定義（キー = レポート上の銘柄名）
INSTRUMENTS = _CFG["instruments"]

# --- 派生テーブル（各スクレイパーへの供給形） ---

# Twelve Data シンボル: {銘柄名: TD シンボル}（TD 非対応銘柄は含めない）
TWELVEDATA_SYMBOLS = {
    sym: cfg["twelvedata_symbol"]
    for sym, cfg in INSTRUMENTS.items()
    if cfg.get("twelvedata_symbol")
}

# CFTC COT 対象: [(表示名, market_and_exchange_names)] を order 順で
COT_TARGETS = [
    (cfg["cot"]["label"], cfg["cot"]["market"])
    for sym, cfg in sorted(
        ((s, c) for s, c in INSTRUMENTS.items() if c.get("cot")),
        key=lambda item: item[1]["cot"]["order"],
    )
]

# FXSSI 抽出対象: メイン銘柄（fxssi: true）+ 参考ペア
FXSSI_SYMBOLS = [
    sym for sym, cfg in INSTRUMENTS.items() if cfg.get("fxssi")
] + list(_CFG.get("fxssi_extra_pairs", []))

# IG Client Sentiment: {銘柄名: URL}（ページが存在する銘柄のみ）
IG_URLS = {
    sym: cfg["ig_url"] for sym, cfg in INSTRUMENTS.items() if cfg.get("ig_url")
}

# MyFXBook Open Orders 対象銘柄
OPEN_ORDER_SYMBOLS = [
    sym for sym, cfg in INSTRUMENTS.items() if cfg.get("open_orders")
]

# DXY の EUR/USD 逆数推定（最終フォールバック）
DXY_ESTIMATE_SYMBOL = _CFG["dxy_estimate"]["twelvedata_symbol"]
EURUSD_DXY_FACTOR = float(_CFG["dxy_estimate"]["factor"])

# DXY 構成 6 通貨: [(TD ペア, ウェイト, inverse)]
DXY_COMPONENTS = [
    (c["pair"], float(c["weight"]), bool(c["inverse"]))
    for c in _CFG["dxy_components"]
]

# プリマーケット指数（premarket.py）
PREMARKET_TD_SYMBOLS = {
    label: c["twelvedata_symbol"] for label, c in _CFG["premarket_indices"].items()
}
PREMARKET_YAHOO_CHART_SYMBOLS = {
    label: c["yahoo_chart_symbol"] for label, c in _CFG["premarket_indices"].items()
}
PREMARKET_YAHOO_URLS = {
    label: c["yahoo_url"] for label, c in _CFG["premarket_indices"].items()
}

# VIX ターム構造系列（vix_structure.py）
VIX_TD_SYMBOLS = {
    label: c["twelvedata_symbol"] for label, c in _CFG["vix_series"].items()
}
VIX_FRED_SYMBOLS = {
    label: c["fred_series"]
    for label, c in _CFG["vix_series"].items()
    if c.get("fred_series")
}
VIX_CBOE_URLS = {
    label: c["cboe_url"] for label, c in _CFG["vix_series"].items()
}
VIX_YAHOO_URLS = {
    label: c["yahoo_url"] for label, c in _CFG["vix_series"].items()
}

# BTC 現物 ETF ティッカー（btc_etf.py）
BTC_ETF_TICKERS = list(_CFG["btc_etf_tickers"])

# FOMC 日程（各会合の初日）
# 一次情報: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
# 2026-12 は旧値 12-15 が公式日程 (Dec 8-9) と不一致だったため修正済み (2026-06-11)。
FOMC_DATES_2026 = [
    "2026-01-27",
    "2026-03-17",
    "2026-04-28",
    "2026-06-16",
    "2026-07-28",
    "2026-09-15",
    "2026-10-27",
    "2026-12-08",
]
FOMC_DATES_2027 = [
    "2027-01-26",
    "2027-03-16",
    "2027-04-27",
    "2027-06-08",
    "2027-07-27",
    "2027-09-14",
    "2027-10-26",
    "2027-12-07",
]
# 全年程の結合リスト（main.py はこれを参照する）
FOMC_DATES = FOMC_DATES_2026 + FOMC_DATES_2027

# FOMC 日程テーブル枯渇の事前警告: 最終登録日まで残りこの日数を切ったら警告
FOMC_SCHEDULE_WARN_DAYS = 90

# Playwright 設定
BROWSER_TIMEOUT = 30000  # ms
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def load_x_search_config() -> dict:
    """Hermes X-Search 統合設定を返す。セクション欠落時は完全無効。"""
    section = _CFG.get("x_search")
    if not isinstance(section, dict):
        return {"enabled": False}
    return dict(section)


def get_output_setting(key: str):
    """config.yaml `output` セクションの設定値を返す。

    セクションまたはキーが欠落している場合は None（呼び出し側でスキップ判断）。
    例: get_output_setting("gdrive_pdf_dir") → PDF の Google Drive 同期先パス。
    """
    section = _CFG.get("output")
    if not isinstance(section, dict):
        return None
    return section.get(key)
