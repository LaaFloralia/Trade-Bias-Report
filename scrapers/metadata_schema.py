"""共通メタデータスキーマ helper

`scrapers/fred.py` で確立した metadata schema を、他スクレイパーへ
**非破壊** で横展開するための補完ユーティリティ。

設計方針:
  - 各スクレイパーが既に返している dict を **破壊しない**
    （上書きしない、既存キーは尊重する）。
  - 欠けているメタデータフィールドのみ補完する（冪等）。
  - main.py / format_scraped_data() / 既存テストの呼び出し側互換を維持する。

統一メタデータスキーマ（共通フィールド）:
  source         : 取得元の人間可読名（"FRED", "MyFXBook", "CoinGlass", ...）
  symbol         : 銘柄 / 系列 ID（"XAUUSD", "DGS10", "BTCUSDT", ...）None 可
  timestamp      : スクレイパーが値を取得したサーバ時刻 (UTC ISO8601)
  as_of_date     : データ自体の基準日 (YYYY-MM-DD)。リアルタイム系は None でよい
  stale          : True なら値が許容鮮度を超えている / フォールバック由来
  fallback_used  : True なら直近キャッシュやサブソースから復元した値
  error          : str | None。取得失敗時の理由
  note           : str | None。補足メモ（フォールバック理由など）

`fetch_fred_series()` の戻り値（`source`/`series_id`/`value`/`as_of_date`/
`stale`/`fallback_used`/`error`/`note` を完備）をリファレンスとする。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

# 共通スキーマで保証するキー一覧（順序を保つため tuple）
COMMON_FIELDS: tuple[str, ...] = (
    "source",
    "symbol",
    "timestamp",
    "as_of_date",
    "stale",
    "fallback_used",
    "error",
    "note",
)


def now_utc_iso() -> str:
    """UTC ISO8601 (秒精度、末尾 Z) を返す。fred.py の `_now_utc_iso` と同じ表記。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def empty_metadata(source: str, symbol: Optional[str] = None) -> dict:
    """共通フィールドだけ持つ空のメタデータ dict を返す。

    新規スクレイパー実装の起点として利用する想定。
    """
    return {
        "source": source,
        "symbol": symbol,
        "timestamp": now_utc_iso(),
        "as_of_date": None,
        "stale": False,
        "fallback_used": False,
        "error": None,
        "note": None,
    }


def ensure_metadata(
    obj: Any,
    *,
    source: Optional[str] = None,
    symbol: Optional[str] = None,
) -> Any:
    """`obj` が dict なら共通メタデータフィールドを **欠けている分だけ** 補完して返す。

    既存キーは絶対に上書きしない（冪等・非破壊）。`obj` を in-place で更新する
    と同時に同じ参照を return する（呼び出し側で `d = ensure_metadata(d, ...)`
    としても、戻り値を捨てて元の参照を使い続けても同じ結果になる）。

    dict 以外（None / list / プリミティブ）はそのまま返す（既存スクレイパーの
    一部は str や None を返すため、安全側に振る）。

    Args:
        obj:    スクレイパー戻り値
        source: 推奨される source 名（obj["source"] が無いときだけセット）
        symbol: 推奨される symbol（obj["symbol"] が無いときだけセット）
    """
    if not isinstance(obj, dict):
        return obj

    # source / symbol: 引数のヒントで欠けてる場合のみ埋める
    if source is not None and not obj.get("source"):
        obj["source"] = source
    if symbol is not None and not obj.get("symbol"):
        obj["symbol"] = symbol

    # 残りの共通フィールド: 未定義キーだけデフォルト値で埋める
    if "timestamp" not in obj:
        obj["timestamp"] = now_utc_iso()
    if "as_of_date" not in obj:
        obj["as_of_date"] = None
    if "stale" not in obj:
        obj["stale"] = False
    if "fallback_used" not in obj:
        obj["fallback_used"] = False
    if "error" not in obj:
        obj["error"] = None
    if "note" not in obj:
        obj["note"] = None

    return obj


def normalize_scraper_results(results: dict) -> dict:
    """`main.collect_all_data()` 戻り値の各エントリに対し ensure_metadata を適用。

    既知のキー → (source, symbol) のヒントテーブルで、source/symbol が未設定なら
    補完する。ネストした nested dict（`retail_sentiment`, `coinglass`,
    `myfxbook_open_orders`）は内側の dict にも適用する。

    `timestamp`（results 直下の取得日時）と `price_data`（整形済みテキスト）は
    dict ではないため通過するだけ。
    """
    if not isinstance(results, dict):
        return results

    # ヒント: key → (source, default_symbol)
    hints: dict[str, tuple[str, Optional[str]]] = {
        "dxy":                  ("DXY scraper", "DXY"),
        "economic_calendar":    ("Investing.com", None),
        "fedwatch":             ("CME FedWatch", None),
        "btc_etf":              ("BTC ETF source", None),
        "dxy_components":       ("Twelve Data", "DXY"),
        "vix_structure":        ("CBOE/FRED/Twelve Data", "VIX"),
        "premarket":            ("Twelve Data", None),
        "macro_liquidity":      ("FRED", None),
        "rate_spreads":         ("FRED", None),
        "crypto_funding":       ("Binance/Bybit/OKX", "BTCUSDT"),
        "binance_btc_sentiment": ("Binance Futures", "BTCUSDT"),
        "cot":                  ("CFTC", None),
    }

    for key, value in results.items():
        if key in ("timestamp", "price_data"):
            continue

        if key in hints:
            src, sym = hints[key]
            ensure_metadata(value, source=src, symbol=sym)

        # ネスト構造（dict-of-dict）に対しても適用
        if key == "retail_sentiment" and isinstance(value, dict):
            for sym, inner in value.items():
                ensure_metadata(inner, source=None, symbol=sym)
        elif key == "coinglass" and isinstance(value, dict):
            for sym, inner in value.items():
                ensure_metadata(inner, source="CoinGlass", symbol=sym)
        elif key == "myfxbook_open_orders" and isinstance(value, dict):
            for sym, inner in value.items():
                ensure_metadata(inner, source="MyFXBook Open Orders", symbol=sym)
        elif key == "fred" and isinstance(value, dict):
            # fred は既に完全準拠だが、念のため通す
            for sid, inner in value.items():
                ensure_metadata(inner, source="FRED", symbol=sid)

    return results
