"""国債利回りスプレッドスクレイパー

対象スプレッド:
  US10Y - DE10Y ... ドル円バイアスの中期方向
  US10Y - JGB10Y ... USDJPY 中期方向 (実質金利差)
  US10Y - UK10Y ... GBPUSD 補助
  US10Y - CA10Y ... USDCAD 補助

データソース:
  FRED: DGS10 (US 10Y) — daily
  FRED: IRLTLT01DEM156N (Germany Long-term Government Bond Yield) — monthly
  FRED: IRLTLT01JPM156N (Japan Long-term Government Bond Yield) — monthly
  FRED: IRLTLT01GBM156N (UK Long-term Government Bond Yield) — monthly
  FRED: IRLTLT01CAM156N (Canada Long-term Government Bond Yield) — monthly

注意: 海外利回りは FRED OECD 系列で月次のため、stale 上限を 35 日まで許容。
最新を優先しつつ、新鮮度をフラグで明示する。

ICT 用途: 中期通貨方向の裏付け。スプレッド拡大 = USD bullish (ドル金利優位)。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from scrapers.fred import fetch_fred_series, get_fred_api_key

SERIES = {
    "US10Y":  "DGS10",
    "DE10Y":  "IRLTLT01DEM156N",
    "JGB10Y": "IRLTLT01JPM156N",
    "UK10Y":  "IRLTLT01GBM156N",
    "CA10Y":  "IRLTLT01CAM156N",
}

PAIRS = [
    ("US-DE", "US10Y", "DE10Y", "EURUSD バイアス補助 (USD Bullish 寄与)"),
    ("US-JP", "US10Y", "JGB10Y", "USDJPY バイアス中期方向 (USD Bullish 寄与)"),
    ("US-UK", "US10Y", "UK10Y", "GBPUSD バイアス補助"),
    ("US-CA", "US10Y", "CA10Y", "USDCAD バイアス補助"),
]


def _val(d: dict) -> Optional[float]:
    if not isinstance(d, dict):
        return None
    v = d.get("value")
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


async def scrape_rate_spreads() -> dict:
    """国債利回りスプレッド一式を取得する。

    Returns:
        {
            "source": "FRED (DGS10 / OECD long-term yields)",
            "yields": {"US10Y": {...}, "DE10Y": {...}, ...},
            "spreads": [
                {"pair": "US-JP", "spread": 4.42, "prev_spread": 4.38, "change": +0.04,
                 "interpretation": "...", "stale": bool},
                ...
            ],
            "error": str | None,
        }
    """
    result = {
        "source": "FRED (DGS10 / OECD long-term yields)",
        "yields": {},
        "spreads": [],
        "error": None,
    }

    api_key = get_fred_api_key()
    if not api_key:
        result["error"] = "FRED_API_KEY not configured"
        return result

    loop = asyncio.get_event_loop()
    tasks = [
        loop.run_in_executor(None, fetch_fred_series, sid, api_key)
        for sid in SERIES.values()
    ]
    fetched = await asyncio.gather(*tasks, return_exceptions=True)

    for label, res in zip(SERIES.keys(), fetched):
        if isinstance(res, Exception):
            result["yields"][label] = {"error": f"unhandled: {type(res).__name__}: {res}"}
            continue
        result["yields"][label] = res

    # スプレッド計算
    for pair_label, base_label, sub_label, interp in PAIRS:
        base = result["yields"].get(base_label, {})
        sub = result["yields"].get(sub_label, {})
        base_v = _val(base)
        sub_v = _val(sub)
        if base_v is None or sub_v is None:
            result["spreads"].append({
                "pair": pair_label,
                "spread": None,
                "prev_spread": None,
                "change": None,
                "interpretation": interp,
                "stale": True,
                "error": "yield missing",
            })
            continue

        spread = base_v - sub_v
        base_p = _val({"value": base.get("prev_value")})
        sub_p = _val({"value": sub.get("prev_value")})
        prev_spread = (base_p - sub_p) if (base_p is not None and sub_p is not None) else None
        change = (spread - prev_spread) if prev_spread is not None else None

        # stale 判定: いずれかが stale なら spread も stale
        stale = bool(base.get("stale") or sub.get("stale"))

        result["spreads"].append({
            "pair": pair_label,
            "spread": round(spread, 3),
            "prev_spread": round(prev_spread, 3) if prev_spread is not None else None,
            "change": round(change, 3) if change is not None else None,
            "base_as_of": base.get("as_of_date"),
            "sub_as_of": sub.get("as_of_date"),
            "interpretation": interp,
            "stale": stale,
        })

    return result


if __name__ == "__main__":
    data = asyncio.run(scrape_rate_spreads())
    print("--- Rate Spreads ---")
    print(f"  source: {data['source']}")
    if data["error"]:
        print(f"  error: {data['error']}")
    for label, y in data.get("yields", {}).items():
        print(f"  {label}: value={y.get('value')} as_of={y.get('as_of_date')} stale={y.get('stale')}")
    for s in data.get("spreads", []):
        print(f"  spread {s['pair']}: {s['spread']} (Δ {s['change']}) stale={s['stale']}")
