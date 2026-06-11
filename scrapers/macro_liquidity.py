"""マクロ流動性 (Fed BS / Reverse Repo / TGA) スクレイパー

FRED データソース:
  WALCL    ... 連邦準備制度の総資産 (週次、水曜時点。Fed BS)
  RRPONTSYD... Overnight Reverse Repo Operations (日次)
  WTREGEN  ... U.S. Treasury General Account (週次、水曜時点)

ネットリクイディティ近似:
  Net Liquidity ≈ WALCL - RRPONTSYD - WTREGEN

ICT 用途:
  - Net Liquidity 拡大 → リスクアセット (BTC, NQ, SPX) bullish 寄与
  - 縮小 → bearish 寄与
  - 週次変化を Bias の中期裏付けに使う
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from scrapers.fred import fetch_fred_series, get_fred_api_key

SERIES = {
    "WALCL":     {"label": "Fed Total Assets",                "unit": "M USD"},
    "RRPONTSYD": {"label": "Overnight Reverse Repo (RRP)",    "unit": "B USD"},
    "WTREGEN":   {"label": "Treasury General Account (TGA)",  "unit": "M USD"},
}


async def scrape_macro_liquidity() -> dict:
    """3 系列を並列取得し、Net Liquidity と週次変化を計算する。

    Returns:
        {
            "source": "FRED (WALCL / RRPONTSYD / WTREGEN)",
            "series": {
                "WALCL":     {"value": ..., "prev_value": ..., "change": ..., "as_of_date": ...},
                "RRPONTSYD": {...},
                "WTREGEN":   {...},
            },
            "net_liquidity_b": float | None,            # 単位 B USD
            "net_liquidity_change_b": float | None,     # 前回比 B USD
            "regime": "expansion" | "contraction" | "flat" | "unknown",
            "error": str | None,
        }
    """
    result = {
        "source": "FRED (WALCL / RRPONTSYD / WTREGEN)",
        "series": {},
        "net_liquidity_b": None,
        "net_liquidity_change_b": None,
        "regime": None,
        "error": None,
    }

    api_key = get_fred_api_key()
    if not api_key:
        result["error"] = "FRED_API_KEY not configured"
        return result

    loop = asyncio.get_event_loop()
    tasks = [
        loop.run_in_executor(None, fetch_fred_series, sid, api_key)
        for sid in SERIES
    ]
    fetched = await asyncio.gather(*tasks, return_exceptions=True)

    for sid, res in zip(SERIES, fetched):
        if isinstance(res, Exception):
            result["series"][sid] = {"error": f"unhandled: {type(res).__name__}: {res}"}
            continue
        result["series"][sid] = res

    stale_series = [
        sid for sid, series_data in result["series"].items()
        if isinstance(series_data, dict) and series_data.get("stale")
    ]
    if stale_series:
        result["stale"] = True
        result["note"] = "stale series: " + ", ".join(stale_series)
    else:
        result["stale"] = False

    # Net Liquidity 計算
    walcl = result["series"].get("WALCL", {})
    rrp = result["series"].get("RRPONTSYD", {})
    tga = result["series"].get("WTREGEN", {})

    # WALCL は M USD 単位、RRP は B USD 単位、TGA は M USD 単位
    # Net Liquidity を B USD に正規化
    def _val(d: dict) -> Optional[float]:
        v = d.get("value")
        return float(v) if v is not None else None

    walcl_v = _val(walcl)
    rrp_v = _val(rrp)
    tga_v = _val(tga)

    if all(v is not None for v in (walcl_v, rrp_v, tga_v)):
        net_b = (walcl_v / 1000.0) - rrp_v - (tga_v / 1000.0)
        result["net_liquidity_b"] = round(net_b, 2)

        # 前回比
        walcl_p = _val({"value": walcl.get("prev_value")})
        rrp_p = _val({"value": rrp.get("prev_value")})
        tga_p = _val({"value": tga.get("prev_value")})
        if all(v is not None for v in (walcl_p, rrp_p, tga_p)):
            net_b_prev = (walcl_p / 1000.0) - rrp_p - (tga_p / 1000.0)
            change = net_b - net_b_prev
            result["net_liquidity_change_b"] = round(change, 2)
            if change > 20:
                result["regime"] = "expansion"
            elif change < -20:
                result["regime"] = "contraction"
            else:
                result["regime"] = "flat"
        else:
            result["regime"] = "unknown"
    else:
        result["error"] = "WALCL/RRPONTSYD/WTREGEN のいずれかが取得不可"
        result["regime"] = "unknown"

    return result


if __name__ == "__main__":
    data = asyncio.run(scrape_macro_liquidity())
    print("--- Macro Liquidity ---")
    print(f"  source: {data['source']}")
    print(f"  net_liquidity_b: {data['net_liquidity_b']} B USD")
    print(f"  net_liquidity_change_b: {data['net_liquidity_change_b']} B USD (vs prior)")
    print(f"  regime: {data['regime']}")
    print(f"  error: {data['error']}")
    for sid, s in data.get("series", {}).items():
        print(f"  {sid}: value={s.get('value')} as_of={s.get('as_of_date')} prev={s.get('prev_value')}")
