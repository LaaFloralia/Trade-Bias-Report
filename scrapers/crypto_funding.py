"""暗号資産の Funding Rate (Binance / Bybit / OKX) 取得

対象: BTCUSDT 永久先物の Funding Rate を 3 取引所から取得し、
平均と最大乖離を集計する。

API エンドポイント (パブリック、認証不要):
  Binance: https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT
  Bybit  : https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT
  OKX    : https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP

ICT 用途:
  - Funding 高 (positive) = ロング過熱 → SSL 集中 (Short squeeze 待ち)
  - Funding 低 (negative) = ショート過熱 → BSL 集中
  - 取引所間乖離大 → アービトラージフロー、構造的歪み

注意: 既存の coinglass.py は CoinGlass 経由 1 ソースのみ。本スクレイパーは
直接 3 取引所から取得することで信頼度を上げる (CoinGlass 不通時のフォールバック)。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

import requests

HTTP_TIMEOUT = 10


def _binance() -> Optional[dict]:
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/premiumIndex",
            params={"symbol": "BTCUSDT"},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        rate = data.get("lastFundingRate")
        if rate is None:
            return None
        return {
            "exchange": "Binance",
            "funding_rate": float(rate) * 100,  # 単位 % に変換
            "next_funding_time": data.get("nextFundingTime"),
            "mark_price": float(data.get("markPrice")) if data.get("markPrice") else None,
        }
    except Exception:
        return None


def _bybit() -> Optional[dict]:
    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/tickers",
            params={"category": "linear", "symbol": "BTCUSDT"},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        items = data.get("result", {}).get("list", [])
        if not items:
            return None
        item = items[0]
        rate = item.get("fundingRate")
        if rate is None:
            return None
        return {
            "exchange": "Bybit",
            "funding_rate": float(rate) * 100,
            "next_funding_time": item.get("nextFundingTime"),
            "mark_price": float(item.get("markPrice")) if item.get("markPrice") else None,
        }
    except Exception:
        return None


def _okx() -> Optional[dict]:
    try:
        r = requests.get(
            "https://www.okx.com/api/v5/public/funding-rate",
            params={"instId": "BTC-USDT-SWAP"},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        items = data.get("data", [])
        if not items:
            return None
        item = items[0]
        rate = item.get("fundingRate")
        if rate is None:
            return None
        return {
            "exchange": "OKX",
            "funding_rate": float(rate) * 100,
            "next_funding_time": item.get("nextFundingTime"),
            "mark_price": None,
        }
    except Exception:
        return None


async def scrape_crypto_funding() -> dict:
    """3 取引所の BTCUSDT Funding Rate を並列取得する。

    Returns:
        {
            "source": "Binance / Bybit / OKX (direct API)",
            "exchanges": [{"exchange": "Binance", "funding_rate": 0.01, ...}, ...],
            "average_funding_rate": float | None,  # 単位 %
            "max_dispersion": float | None,        # 最大 - 最小、単位 %
            "regime": "long crowded" | "short crowded" | "neutral" | "unknown",
            "error": str | None,
        }
    """
    result = {
        "source": "Binance / Bybit / OKX (direct API)",
        "exchanges": [],
        "average_funding_rate": None,
        "max_dispersion": None,
        "regime": None,
        "error": None,
    }

    loop = asyncio.get_event_loop()
    tasks = [
        loop.run_in_executor(None, _binance),
        loop.run_in_executor(None, _bybit),
        loop.run_in_executor(None, _okx),
    ]
    fetched = await asyncio.gather(*tasks)

    for entry in fetched:
        if entry is not None:
            result["exchanges"].append(entry)

    if not result["exchanges"]:
        result["error"] = "all 3 exchanges unreachable"
        result["regime"] = "unknown"
        return result

    rates = [e["funding_rate"] for e in result["exchanges"] if e.get("funding_rate") is not None]
    if rates:
        avg = sum(rates) / len(rates)
        result["average_funding_rate"] = round(avg, 5)
        result["max_dispersion"] = round(max(rates) - min(rates), 5)

        # レジーム判定 (% 単位、Binance 0.01% = 1bp)
        # ロング過熱: 平均 > 0.01% (年率 ~10%+)
        if avg > 0.01:
            result["regime"] = "long crowded"
        elif avg < -0.005:
            result["regime"] = "short crowded"
        else:
            result["regime"] = "neutral"

    return result


if __name__ == "__main__":
    data = asyncio.run(scrape_crypto_funding())
    print("--- Crypto Funding ---")
    print(f"  source: {data['source']}")
    print(f"  average_funding_rate: {data['average_funding_rate']}%")
    print(f"  max_dispersion: {data['max_dispersion']}%")
    print(f"  regime: {data['regime']}")
    print(f"  error: {data['error']}")
    for e in data.get("exchanges", []):
        print(f"  {e['exchange']}: {e['funding_rate']}% (mark={e.get('mark_price')})")
