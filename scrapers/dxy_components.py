"""DXY 構成通貨のスプレッド分解スクレイパー

DXY (US Dollar Index) の方向性を構成 6 通貨ペアの寄与度に分解する。
構成ウェイト (ICE 公式):
  EUR/USD : 57.6%
  USD/JPY : 13.6%
  GBP/USD : 11.9%
  USD/CAD :  9.1%
  USD/SEK :  4.2%
  USD/CHF :  3.6%

ICT 用途: USDJPY バイアスの裏付け (USDJPY 寄与が DXY 全体を押している/逆風)。

データソース: Twelve Data /quote (1 回でバッチ取得)。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

import requests
from config import TWELVEDATA_API_KEY

BASE_URL = "https://api.twelvedata.com"

# (twelve_data_symbol, weight, is_inverse) — DXY 上昇に寄与する向き。
# is_inverse=True の場合、その通貨ペアの値上がりが DXY を押し下げる (ペアが USD/XXX ではなく XXX/USD)。
COMPONENTS = [
    ("EUR/USD", 0.576, True),
    ("USD/JPY", 0.136, False),
    ("GBP/USD", 0.119, True),
    ("USD/CAD", 0.091, False),
    ("USD/SEK", 0.042, False),
    ("USD/CHF", 0.036, False),
]


def _fetch_quotes() -> Optional[dict]:
    symbols = ",".join(c[0] for c in COMPONENTS)
    import time
    for attempt in range(2):
        try:
            resp = requests.get(
                f"{BASE_URL}/quote",
                params={"symbol": symbols, "apikey": TWELVEDATA_API_KEY, "dp": "5"},
                timeout=15,
            )
            if resp.status_code == 429:
                time.sleep(8)  # rate limit, wait longer than 1 min/8 calls
                continue
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and data.get("status") == "error":
                # JSON-level rate limit
                if data.get("code") == 429:
                    time.sleep(8)
                    continue
                return None
            return data
        except Exception:
            if attempt == 0:
                time.sleep(5)
                continue
            return None
    return None


async def scrape_dxy_components() -> dict:
    """DXY 構成 6 通貨ペアの前日比を取得し、ウェイト按分後の寄与度を算出する。

    Returns:
        {
            "source": "Twelve Data (DXY components)",
            "components": [
                {"symbol": "EUR/USD", "weight": 0.576, "change_pct": -0.10,
                 "dxy_contribution": +0.0576, ...},
                ...
            ],
            "estimated_dxy_change_pct": float,  # 構成寄与度の合計
            "leading_driver": str,  # 寄与度絶対値が最大の通貨
            "leading_contribution": float,
            "error": str | None,
        }
    """
    result = {
        "source": "Twelve Data (DXY components)",
        "components": [],
        "estimated_dxy_change_pct": None,
        "leading_driver": None,
        "leading_contribution": None,
        "error": None,
    }

    if not TWELVEDATA_API_KEY:
        result["error"] = "TWELVEDATA_API_KEY not configured"
        return result

    # blocking I/O を thread に逃がす
    loop = asyncio.get_event_loop()
    quotes = await loop.run_in_executor(None, _fetch_quotes)
    if quotes is None:
        result["error"] = "Twelve Data /quote 取得失敗"
        return result

    total_contribution = 0.0
    components = []

    for symbol, weight, inverse in COMPONENTS:
        q = quotes.get(symbol) if isinstance(quotes, dict) else None
        if not q or "percent_change" not in q:
            components.append({
                "symbol": symbol,
                "weight": weight,
                "change_pct": None,
                "dxy_contribution": None,
                "error": "no quote",
            })
            continue
        try:
            pct = float(q["percent_change"])
        except (TypeError, ValueError):
            components.append({
                "symbol": symbol,
                "weight": weight,
                "change_pct": None,
                "dxy_contribution": None,
                "error": "parse error",
            })
            continue

        # DXY 上昇方向への寄与: USD ベースなら +、逆ペアなら符号反転
        directed_pct = -pct if inverse else pct
        contribution = directed_pct * weight
        total_contribution += contribution

        components.append({
            "symbol": symbol,
            "weight": weight,
            "change_pct": round(pct, 4),
            "dxy_contribution": round(contribution, 4),
            "current": float(q.get("close")) if q.get("close") else None,
        })

    result["components"] = components
    result["estimated_dxy_change_pct"] = round(total_contribution, 4)

    # 寄与度絶対値最大の通貨
    valid = [c for c in components if c.get("dxy_contribution") is not None]
    if valid:
        leader = max(valid, key=lambda c: abs(c["dxy_contribution"]))
        result["leading_driver"] = leader["symbol"]
        result["leading_contribution"] = leader["dxy_contribution"]

    return result


if __name__ == "__main__":
    data = asyncio.run(scrape_dxy_components())
    print("--- DXY Components ---")
    for k, v in data.items():
        if k != "components":
            print(f"  {k}: {v}")
    print("  components:")
    for c in data.get("components", []):
        print(f"    {c}")
