"""CFTC COT Disaggregated (Futures Only) — 金の機関ポジショニング内訳

データソース: CFTC Socrata API
エンドポイント: https://publicreporting.cftc.gov/resource/72hh-3qpy.json

Legacy レポート（scrapers/cot.py）の "Large Speculators" は Managed Money（ファンド）と
Other Reportables（事業法人以外の大口）を混ぜてしまうため、金の需給の実体が読めない。
Disaggregated は以下に分解する:

  - Managed Money   : CTA / ヘッジファンド。トレンドフォローの主体で、極端化が反転の前兆になる
  - Swap Dealers    : ディーラー。OTC 取引のヘッジで反対側を持つため、実質的な「売り手の在庫」
  - Producer/Merchant: 鉱山会社・現物商。生産ヘッジ（構造的なショート）
  - Other Reportables: 上記以外の大口

金では Swap Dealer のショートが OI の 6 割を超えることが常態で、これは弱気材料ではなく
OTC 需要の裏返し。Legacy の "Commercials"（= Swap + Producer 合算）ではこの区別がつかない。

トレーダー数（traders_*）も返るため、「少数の業者に偏ったポジション」を検出できる
（例: ショート業者 13 社に対しロング 81 社 = 一方向に混雑）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

import requests

BASE_URL = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"

FIELDS = [
    "report_date_as_yyyy_mm_dd",
    "open_interest_all",
    "m_money_positions_long_all",
    "m_money_positions_short_all",
    "change_in_m_money_long_all",
    "change_in_m_money_short_all",
    "swap_positions_long_all",
    "swap__positions_short_all",       # API 側のフィールド名がアンダースコア2つ（原文ママ）
    "change_in_swap_long_all",
    "change_in_swap_short_all",
    "prod_merc_positions_long",
    "prod_merc_positions_short",
    "other_rept_positions_long",
    "other_rept_positions_short",
    "traders_m_money_long_all",
    "traders_m_money_short_all",
]


def _to_int(row: dict, key: str) -> Optional[int]:
    val = row.get(key)
    try:
        return int(float(val)) if val is not None else None
    except (TypeError, ValueError):
        return None


def _net(long_v: Optional[int], short_v: Optional[int]) -> Optional[int]:
    return (long_v - short_v) if (long_v is not None and short_v is not None) else None


def _parse_row(row: dict) -> dict:
    mm_l, mm_s = _to_int(row, "m_money_positions_long_all"), _to_int(row, "m_money_positions_short_all")
    sw_l, sw_s = _to_int(row, "swap_positions_long_all"), _to_int(row, "swap__positions_short_all")
    pm_l, pm_s = _to_int(row, "prod_merc_positions_long"), _to_int(row, "prod_merc_positions_short")
    or_l, or_s = _to_int(row, "other_rept_positions_long"), _to_int(row, "other_rept_positions_short")
    oi = _to_int(row, "open_interest_all")

    mm_net = _net(mm_l, mm_s)
    mm_chg = _net(_to_int(row, "change_in_m_money_long_all"),
                  _to_int(row, "change_in_m_money_short_all"))
    sw_chg = _net(_to_int(row, "change_in_swap_long_all"),
                  _to_int(row, "change_in_swap_short_all"))

    return {
        "date": (row.get("report_date_as_yyyy_mm_dd") or "")[:10],
        "open_interest": oi,
        "managed_money": {
            "long": mm_l, "short": mm_s, "net": mm_net,
            "net_change": mm_chg,
            "net_pct_oi": round(mm_net / oi * 100, 1) if (mm_net is not None and oi) else None,
            "traders_long": _to_int(row, "traders_m_money_long_all"),
            "traders_short": _to_int(row, "traders_m_money_short_all"),
        },
        "swap_dealers": {
            "long": sw_l, "short": sw_s, "net": _net(sw_l, sw_s),
            "net_change": sw_chg,
            "short_pct_oi": round(sw_s / oi * 100, 1) if (sw_s is not None and oi) else None,
        },
        "producer_merchant": {"long": pm_l, "short": pm_s, "net": _net(pm_l, pm_s)},
        "other_reportables": {"long": or_l, "short": or_s, "net": _net(or_l, or_s)},
    }


def fetch_cot_disaggregated(market_name: str) -> dict:
    """指定市場の最新 Disaggregated レポートを取得する。

    Returns:
        {"market": str, "data": dict | None, "error": str | None}
    """
    result = {"market": market_name, "data": None, "error": None}
    params = {
        "$where": f"market_and_exchange_names='{market_name}'",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": "1",
        "$select": ",".join(FIELDS),
    }
    try:
        resp = requests.get(BASE_URL, params=params, timeout=30)
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    if not rows:
        result["error"] = "データなし"
        return result
    result["data"] = _parse_row(rows[0])
    return result


def format_disaggregated_lines(res: dict) -> list[str]:
    """main.format_scraped_data 用の整形行。"""
    lines = ["### COT Disaggregated (XAUUSD / 機関ポジショニング内訳)"]
    if not isinstance(res, dict) or res.get("error") or not res.get("data"):
        err = res.get("error", "取得不可") if isinstance(res, dict) else "取得不可"
        lines.append(f"- 取得不可（{err}）")
        return lines

    d = res["data"]
    mm, sw, pm = d["managed_money"], d["swap_dealers"], d["producer_merchant"]
    lines.append(f"- Report Date: {d['date']} / Open Interest: {d['open_interest']:,}")

    if mm["net"] is not None:
        chg = f"（前週比 {mm['net_change']:+,}）" if mm["net_change"] is not None else ""
        lines.append(
            f"- Managed Money（ファンド、トレンド主体）: Long {mm['long']:,} / Short {mm['short']:,} "
            f"→ Net {mm['net']:+,}{chg}、OI比 {mm['net_pct_oi']}%"
        )
        if mm["traders_long"] and mm["traders_short"]:
            lines.append(
                f"  業者数: Long {mm['traders_long']} 社 / Short {mm['traders_short']} 社"
                f"（比 {mm['traders_long'] / mm['traders_short']:.1f}:1）"
            )
    if sw["short"] is not None:
        chg = f"（Net 前週比 {sw['net_change']:+,}）" if sw["net_change"] is not None else ""
        lines.append(
            f"- Swap Dealers（ディーラー、OTC ヘッジの裏返し）: Long {sw['long']:,} / "
            f"Short {sw['short']:,} → Short が OI の {sw['short_pct_oi']}%{chg}"
        )
    if pm["net"] is not None:
        lines.append(
            f"- Producer/Merchant（鉱山・現物商、構造的ショート）: "
            f"Long {pm['long']:,} / Short {pm['short']:,} → Net {pm['net']:+,}"
        )
    lines.append(
        "- 読み方: Managed Money の極端化＝反転リスク（Legacy の Large Spec では "
        "Other Reportables と混ざるため判別不能）。Swap Dealer の大量ショートは "
        "OTC 需要の裏返しであり弱気材料ではない。業者数の偏りは混雑度の指標。"
    )
    return lines


if __name__ == "__main__":
    import json

    r = fetch_cot_disaggregated("GOLD - COMMODITY EXCHANGE INC.")
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print()
    print("\n".join(format_disaggregated_lines(r)))
