"""Binance Futures BTC Long/Short データ取得スクレイパー

MyFXBook が BTC を提供していない問題（2026-05-16 判明）への対応として、
取引所機関データから BTC のリテール／プロフェッショナル センチメントを補完する。

API 公式: https://binance-docs.github.io/apidocs/futures/en/#long-short-ratio
認証不要・無料・公開エンドポイント。

取得対象（3 種）:
- topLongShortPositionRatio: 実ポジション量比（トップ口座のみ） → プロ寄り
- topLongShortAccountRatio:  アカウント数比（トップ口座のみ）  → プロ寄り
- globalLongShortAccountRatio: 全アカウント数比             → リテール寄り

period=1h, limit=1 で最新値を取得する。
"""

from __future__ import annotations

import requests

BINANCE_API = "https://fapi.binance.com/futures/data"
TIMEOUT = 10


def _fetch_latest(endpoint: str) -> dict | None:
    """Binance のエンドポイントから BTCUSDT の最新 1 件を取得。"""
    try:
        r = requests.get(
            f"{BINANCE_API}/{endpoint}",
            params={"symbol": "BTCUSDT", "period": "1h", "limit": 1},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else None
    except Exception:
        return None


def fetch_binance_btc_sentiment() -> dict:
    """Binance Futures から BTC の Long/Short データ 3 種を取得する。

    Returns:
        {
            "source": "Binance Futures (BTCUSDT, 1h)",
            "symbol": "BTCUSDT",
            "top_trader_position_long_pct": float | None,   # ポジション量ベース Long%
            "top_trader_position_short_pct": float | None,
            "top_trader_position_ls_ratio": float | None,
            "top_trader_account_long_pct": float | None,    # アカウント数ベース Long%
            "top_trader_account_short_pct": float | None,
            "top_trader_account_ls_ratio": float | None,
            "global_long_pct": float | None,                 # 全アカウント Long%
            "global_short_pct": float | None,
            "global_ls_ratio": float | None,
            "error": str | None,
        }
    """
    result = {
        "source": "Binance Futures (BTCUSDT, 1h)",
        "symbol": "BTCUSDT",
        "top_trader_position_long_pct": None,
        "top_trader_position_short_pct": None,
        "top_trader_position_ls_ratio": None,
        "top_trader_account_long_pct": None,
        "top_trader_account_short_pct": None,
        "top_trader_account_ls_ratio": None,
        "global_long_pct": None,
        "global_short_pct": None,
        "global_ls_ratio": None,
        "error": None,
    }

    errors = []

    # 1. Top Trader Position Ratio（実ポジション量ベース、プロ寄り）
    d = _fetch_latest("topLongShortPositionRatio")
    if d:
        try:
            result["top_trader_position_ls_ratio"] = float(d["longShortRatio"])
            result["top_trader_position_long_pct"] = round(float(d["longAccount"]) * 100, 2)
            result["top_trader_position_short_pct"] = round(float(d["shortAccount"]) * 100, 2)
        except (KeyError, ValueError, TypeError) as e:
            errors.append(f"top_trader_position parse: {e}")
    else:
        errors.append("top_trader_position fetch failed")

    # 2. Top Trader Account Ratio（アカウント数ベース、プロ寄り）
    d = _fetch_latest("topLongShortAccountRatio")
    if d:
        try:
            result["top_trader_account_ls_ratio"] = float(d["longShortRatio"])
            result["top_trader_account_long_pct"] = round(float(d["longAccount"]) * 100, 2)
            result["top_trader_account_short_pct"] = round(float(d["shortAccount"]) * 100, 2)
        except (KeyError, ValueError, TypeError) as e:
            errors.append(f"top_trader_account parse: {e}")
    else:
        errors.append("top_trader_account fetch failed")

    # 3. Global Long/Short Account Ratio（全アカウント、リテール寄り）
    d = _fetch_latest("globalLongShortAccountRatio")
    if d:
        try:
            result["global_ls_ratio"] = float(d["longShortRatio"])
            result["global_long_pct"] = round(float(d["longAccount"]) * 100, 2)
            result["global_short_pct"] = round(float(d["shortAccount"]) * 100, 2)
        except (KeyError, ValueError, TypeError) as e:
            errors.append(f"global parse: {e}")
    else:
        errors.append("global fetch failed")

    # 全部 None なら error にまとめる
    if errors and all(
        result[k] is None
        for k in result
        if k.endswith(("_pct", "_ratio"))
    ):
        result["error"] = "; ".join(errors)

    return result


if __name__ == "__main__":
    data = fetch_binance_btc_sentiment()
    for k, v in data.items():
        print(f"  {k}: {v}")
