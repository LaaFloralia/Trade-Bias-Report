"""CFTC COT (Commitments of Traders) スクレイパー

データソース: CFTC Socrata API (Legacy Futures Only)
エンドポイント: https://publicreporting.cftc.gov/resource/6dca-aqww.json

ウィークリーレポート専用モジュール。Playwrightは使用せずrequestsのみで完結する。
"""

import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).parent.parent))

import requests

# 対象銘柄 (表示名, market_and_exchange_names の値) は
# config.yaml（SSoT）の instruments.*.cot から供給される
from config import COT_TARGETS

BASE_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"

FIELDS = [
    "report_date_as_yyyy_mm_dd",
    "open_interest_all",
    "noncomm_positions_long_all",
    "noncomm_positions_short_all",
    "comm_positions_long_all",
    "comm_positions_short_all",
    "nonrept_positions_long_all",
    "nonrept_positions_short_all",
    "change_in_open_interest_all",
]


def _fetch_instrument(market_name: str) -> list[dict]:
    """指定銘柄の最新2週分データをAPIから取得する。"""
    where = f"market_and_exchange_names='{market_name}'"
    params = {
        "$where": where,
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": "2",
        "$select": ",".join(FIELDS),
    }
    resp = requests.get(BASE_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _parse_row(row: dict) -> dict:
    """1行分のAPIレスポンスを数値に変換する。"""
    def to_int(key: str):
        val = row.get(key)
        return int(float(val)) if val is not None else None

    ls_long  = to_int("noncomm_positions_long_all")
    ls_short = to_int("noncomm_positions_short_all")
    cm_long  = to_int("comm_positions_long_all")
    cm_short = to_int("comm_positions_short_all")
    ss_long  = to_int("nonrept_positions_long_all")
    ss_short = to_int("nonrept_positions_short_all")

    return {
        "date":          row.get("report_date_as_yyyy_mm_dd", "")[:10],
        "open_interest": to_int("open_interest_all"),
        "oi_change":     to_int("change_in_open_interest_all"),
        "ls_long":  ls_long,
        "ls_short": ls_short,
        "ls_net":   (ls_long - ls_short) if (ls_long is not None and ls_short is not None) else None,
        "cm_long":  cm_long,
        "cm_short": cm_short,
        "cm_net":   (cm_long - cm_short) if (cm_long is not None and cm_short is not None) else None,
        "ss_long":  ss_long,
        "ss_short": ss_short,
        "ss_net":   (ss_long - ss_short) if (ss_long is not None and ss_short is not None) else None,
    }


def fetch_cot_data(targets=None) -> dict:
    """対象銘柄のCOTデータを取得し、フォーマット済みテキストを返す。

    Args:
        targets: [(表示名, market_and_exchange_names)] のリスト。
            None なら config 由来の全銘柄 (COT_TARGETS)。
            銘柄スコープ実行時 (main.py --symbol) は絞ったリストが渡される。

    Returns:
        {
            "text": str,         # Claudeに渡すフォーマット済みテキスト
            "report_date": str,  # 最新レポート日付
            "error": str | None,
        }
    """
    sections = []
    latest_date = None
    errors = []

    for display_name, market_name in (targets if targets is not None else COT_TARGETS):
        try:
            rows = _fetch_instrument(market_name)
            if not rows:
                errors.append(f"{display_name}: データなし")
                sections.append(f"[{display_name}]\nCOT取得不可（データなし）")
                continue

            current = _parse_row(rows[0])
            prev = _parse_row(rows[1]) if len(rows) >= 2 else None

            if latest_date is None:
                latest_date = current["date"]

            def fmt(val) -> str:
                return f"{val:,}" if val is not None else "N/A"

            def fmt_net(val) -> str:
                if val is None:
                    return "N/A"
                sign = "+" if val >= 0 else ""
                return f"{sign}{val:,}"

            def fmt_diff(curr, prev_val) -> str:
                if curr is None or prev_val is None:
                    return "N/A"
                diff = curr - prev_val
                sign = "+" if diff >= 0 else ""
                return f"{sign}{diff:,}"

            ls_net_diff = fmt_diff(current["ls_net"], prev["ls_net"] if prev else None)
            cm_net_diff = fmt_diff(current["cm_net"], prev["cm_net"] if prev else None)
            ss_net_diff = fmt_diff(current["ss_net"], prev["ss_net"] if prev else None)

            section_lines = [
                f"[{display_name}]",
                f"Large Speculators: Long {fmt(current['ls_long'])} / Short {fmt(current['ls_short'])} / Net {fmt_net(current['ls_net'])} (前週比: {ls_net_diff})",
                f"Commercials:       Long {fmt(current['cm_long'])} / Short {fmt(current['cm_short'])} / Net {fmt_net(current['cm_net'])} (前週比: {cm_net_diff})",
                f"Small Speculators: Long {fmt(current['ss_long'])} / Short {fmt(current['ss_short'])} / Net {fmt_net(current['ss_net'])} (前週比: {ss_net_diff})",
                f"Open Interest: {fmt(current['open_interest'])} (変化: {fmt_net(current['oi_change'])})",
            ]
            sections.append("\n".join(section_lines))

        except Exception as e:
            errors.append(f"{display_name}: {e}")
            sections.append(f"[{display_name}]\nCOT取得不可（{e}）")

    header_lines = [
        "=== COT Data (CFTC Legacy Futures Only) ===",
        f"Report Date: {latest_date or '不明'}",
        "",
    ]
    text = "\n".join(header_lines) + "\n\n".join(sections)

    return {
        "text": text,
        "report_date": latest_date,
        "error": "; ".join(errors) if errors else None,
    }


if __name__ == "__main__":
    result = fetch_cot_data()
    print(result["text"])
    if result["error"]:
        print(f"\n[WARN] 一部エラー: {result['error']}")
