"""FedWatch スナップショット履歴と前日比 / 前週比の算出。

デイリー/ウィークリーの定時実行（平日 18:00 / 土 07:00 JST）で毎回スナップショットを
`output/history/fedwatch.json` に保存し、レートレンジ別確率の前日比・前週比を
コード側で決定論的に計算する。LLM には計算済みの差分行だけを渡す。

比較ロジック:
  - 前日比: 今日より前の直近スナップショット（1〜4 暦日以内。週末・休日耐性）
  - 前週比: 6〜9 日前のスナップショット（7 日ちょうどを最優先）
  - 履歴が無い/条件を満たさない場合は Investing.com 自身の prev_day / prev_week
    フィールドにフォールバック（ソース内整合なので常に同一会合ベース）
  - FOMC 会合切替ガード: 比較先スナップショットの next_fomc_date が現在と異なる場合、
    レートレンジの意味が変わっているため履歴比較を放棄し Investing 値のみ使う

保存形式（date キー、120 日で剪定、同日再実行は上書き）:
  {"2026-08-12": {"next_fomc_date": "Sep 16, 2026", "future_price": 96.37,
                  "target_rates": [{"range": "3.50-3.75", "current": 50.9, ...}, ...]}}
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

HISTORY_PATH = Path(__file__).parent.parent / "output" / "history" / "fedwatch.json"
RETENTION_DAYS = 120

# 前日比: 1〜4 日前（月曜に金曜分と比較できる幅）/ 前週比: 6〜9 日前（7 日優先）
PREV_DAY_WINDOW = (1, 4)
PREV_WEEK_WINDOW = (6, 9)
PREV_WEEK_PREFERRED = 7
PROBABILITY_TOTAL_TOLERANCE = 0.2


def validate_target_rates(
    rates: object,
    *,
    require_total: bool = True,
    allow_missing_current: bool = False,
) -> Optional[str]:
    """FedWatchのレート別確率が履歴・判断に使えるか検査する。"""
    if not isinstance(rates, list) or not rates:
        return "レートレンジ別確率が未取得"
    current_total = 0.0
    missing_current = False
    for index, row in enumerate(rates, start=1):
        if not isinstance(row, dict) or not row.get("range"):
            return f"レートレンジ別確率 {index} 行目の形式が不正"
        for field in ("current", "prev_day", "prev_week"):
            value = row.get(field)
            if value is None and field != "current":
                continue
            if value is None and field == "current" and allow_missing_current:
                missing_current = True
                continue
            if isinstance(value, bool):
                return f"{row.get('range')} {field} が数値不正"
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                return f"{row.get('range')} {field} が数値不正"
            if not math.isfinite(number) or not 0.0 <= number <= 100.0:
                return f"{row.get('range')} {field} が0〜100の範囲外 ({value})"
            if field == "current":
                current_total += number
    if missing_current and require_total:
        return "current確率が欠測しているため合計を検証できない"
    if not require_total and current_total > 100.0 + PROBABILITY_TOTAL_TOLERANCE:
        return f"既知current確率だけで100%を超過 ({current_total:.3f}%)"
    if require_total and abs(current_total - 100.0) > PROBABILITY_TOTAL_TOLERANCE:
        return (
            f"current確率合計が100%と不整合 ({current_total:.3f}%, "
            f"許容±{PROBABILITY_TOTAL_TOLERANCE}pp)"
        )
    return None


def validate_meeting_date(value: object, today: date) -> Optional[str]:
    """次回FOMC日が有効かつ過去日でないことを検査する。"""
    if not isinstance(value, str) or not value.strip():
        return "次回FOMC日が未取得"
    try:
        meeting_date = datetime.strptime(value.strip(), "%b %d, %Y").date()
    except ValueError:
        return f"次回FOMC日が不正 ({value})"
    if meeting_date < today:
        return f"次回FOMC日が過去 ({meeting_date.isoformat()})"
    return None


def _load_history(path: Path = HISTORY_PATH) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def record_snapshot(fedwatch: dict, today: Optional[date] = None, path: Path = HISTORY_PATH) -> bool:
    """当日の FedWatch 取得結果を履歴に保存する。target_rates が無い日は保存しない。

    Returns: 保存したかどうか。
    """
    if (
        not isinstance(fedwatch, dict)
        or fedwatch.get("error")
        or fedwatch.get("completeness") == "partial"
        or fedwatch.get("unavailable_target_rates")
    ):
        return False
    today = today or date.today()
    if validate_meeting_date(fedwatch.get("next_fomc_date"), today):
        return False
    if validate_target_rates(fedwatch.get("target_rates")):
        return False

    history = _load_history(path)
    history[today.isoformat()] = {
        "next_fomc_date": fedwatch.get("next_fomc_date"),
        "future_price": fedwatch.get("future_price"),
        "target_rates": fedwatch["target_rates"],
    }

    # 剪定: RETENTION_DAYS より古いキーを落とす
    cutoff = (today - timedelta(days=RETENTION_DAYS)).isoformat()
    history = {k: v for k, v in history.items() if k >= cutoff}

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def _find_snapshot(history: dict, today: date, window: tuple[int, int],
                   preferred: Optional[int] = None) -> Optional[tuple[str, dict]]:
    """window = (min_days, max_days) 前のスナップショットを探す。

    preferred 日数ちょうどのものがあれば最優先、なければ window 内で最も新しいもの。
    """
    candidates = {}
    for key, snap in history.items():
        try:
            snap_date = datetime.strptime(key, "%Y-%m-%d").date()
        except ValueError:
            continue
        age = (today - snap_date).days
        if window[0] <= age <= window[1]:
            candidates[age] = (key, snap)
    if not candidates:
        return None
    if preferred is not None and preferred in candidates:
        return candidates[preferred]
    return candidates[min(candidates)]


def _deltas_from_snapshot(current_rates: list[dict], snap: dict) -> dict[str, Optional[float]]:
    prev_by_range = {r.get("range"): r.get("current") for r in snap.get("target_rates", [])}
    out = {}
    for r in current_rates:
        rng = r.get("range")
        cur, prev = r.get("current"), prev_by_range.get(rng)
        out[rng] = round(cur - prev, 1) if cur is not None and prev is not None else None
    return out


def _deltas_from_investing(current_rates: list[dict], field: str) -> dict[str, Optional[float]]:
    """Investing.com が返す prev_day / prev_week フィールドから差分を計算する。"""
    out = {}
    for r in current_rates:
        cur, prev = r.get("current"), r.get(field)
        out[r.get("range")] = round(cur - prev, 1) if cur is not None and prev is not None else None
    return out


def compute_deltas(fedwatch: dict, today: Optional[date] = None, path: Path = HISTORY_PATH) -> dict:
    """前日比 / 前週比を計算する。

    Returns:
        {
            "prev_day":  {"by_range": {range: float|None}, "source": str} | None,
            "prev_week": 同上 | None,
            "note": str | None,   # 会合切替などの注記
        }
    """
    result = {"prev_day": None, "prev_week": None, "note": None}
    if not isinstance(fedwatch, dict) or not fedwatch.get("target_rates"):
        return result

    today = today or date.today()
    current_rates = fedwatch["target_rates"]
    current_meeting = fedwatch.get("next_fomc_date")
    history = _load_history(path)
    # 当日分は比較対象から除外（同日再実行の上書きと独立にする）
    history = {k: v for k, v in history.items() if k < today.isoformat()}

    rollover = False

    def _resolve(window: tuple[int, int], preferred: Optional[int], investing_field: str) -> Optional[dict]:
        nonlocal rollover
        found = _find_snapshot(history, today, window, preferred)
        if found is not None:
            key, snap = found
            if current_meeting and snap.get("next_fomc_date") and snap["next_fomc_date"] != current_meeting:
                rollover = True
            else:
                age = (today - datetime.strptime(key, "%Y-%m-%d").date()).days
                return {
                    "by_range": _deltas_from_snapshot(current_rates, snap),
                    "source": f"履歴 {age}日前 ({key})",
                }
        # フォールバック: Investing 自身の前日/前週値（同一会合ベースで常に整合）
        deltas = _deltas_from_investing(current_rates, investing_field)
        if any(v is not None for v in deltas.values()):
            return {"by_range": deltas, "source": f"Investing {investing_field}"}
        return None

    result["prev_day"] = _resolve(PREV_DAY_WINDOW, None, "prev_day")
    result["prev_week"] = _resolve(PREV_WEEK_WINDOW, PREV_WEEK_PREFERRED, "prev_week")
    if rollover:
        result["note"] = "FOMC 会合切替のため履歴スナップショットとは比較不能（Investing 内部値を使用）"
    return result


def format_delta_lines(fedwatch: dict) -> list[str]:
    """main.format_scraped_data 用: レートレンジ別確率 + 前日比/前週比の行を返す。

    collect_all_data() が fedwatch["deltas"] に compute_deltas() の結果を
    格納している前提。deltas 未添付でも current 値だけは出力する。
    """
    rates = fedwatch.get("target_rates") or []
    unavailable = fedwatch.get("unavailable_target_rates") or []
    if not rates and not unavailable:
        return []

    deltas = fedwatch.get("deltas") or {}
    day, week = deltas.get("prev_day"), deltas.get("prev_week")

    def _fmt(delta_info: Optional[dict], rng: str) -> str:
        if not delta_info:
            return "N/A"
        v = delta_info["by_range"].get(rng)
        return f"{v:+.1f}pp" if v is not None else "N/A"

    lines = ["- レートレンジ別確率（前日比はデイリー用 / 前週比はウィークリー用）:"]
    if unavailable or fedwatch.get("completeness") == "partial":
        lines.append(
            "  現在確率分布: 一部欠測（欠測レンジを0%補完せず、全体分布は未確定）"
        )
    for r in rates:
        rng = r.get("range")
        cur = r.get("current")
        cur_str = f"{cur}%" if cur is not None else "N/A"
        lines.append(
            f"  * {rng}: 現在 {cur_str} | 前日比 {_fmt(day, rng)} | 前週比 {_fmt(week, rng)}"
        )
    for r in unavailable:
        rng = r.get("range", "レンジ不明")
        prior_day = r.get("prev_day")
        prior_week = r.get("prev_week")
        prior_day_str = f"{prior_day:g}%" if prior_day is not None else "取得不可"
        prior_week_str = f"{prior_week:g}%" if prior_week is not None else "取得不可"
        lines.append(
            f"  * {rng}: 現在 取得不可 | 提供表前日 {prior_day_str} | 提供表前週 {prior_week_str}"
        )
    if day:
        lines.append(f"  前日比ソース: {day['source']}")
    if week:
        lines.append(f"  前週比ソース: {week['source']}")
    if deltas.get("note"):
        lines.append(f"  ※ {deltas['note']}")
    if not day and not week:
        lines.append("  前日比/前週比: N/A（履歴なし・Investing 前回値なし）")
    return lines
