"""fedwatch_history のスナップショット保存・前日比/前週比計算のテスト。

実行:
    .venv/bin/python3 -m pytest tests/test_fedwatch_history.py -v
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scrapers.fedwatch_history import (  # noqa: E402
    compute_deltas,
    format_delta_lines,
    record_snapshot,
)


def _fedwatch(rates, meeting="Sep 16, 2026"):
    return {
        "next_fomc_date": meeting,
        "future_price": 96.37,
        "target_rates": rates,
    }


def _rate(rng, current, prev_day=None, prev_week=None):
    return {"range": rng, "current": current, "prev_day": prev_day, "prev_week": prev_week}


def _write_history(path: Path, entries: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries), encoding="utf-8")


def test_record_snapshot_writes_and_overwrites_same_day(tmp_path):
    path = tmp_path / "fedwatch.json"
    today = date(2026, 8, 12)

    assert record_snapshot(_fedwatch([_rate("3.50-3.75", 50.0)]), today, path) is True
    assert record_snapshot(_fedwatch([_rate("3.50-3.75", 55.0)]), today, path) is True

    history = json.loads(path.read_text())
    assert list(history.keys()) == ["2026-08-12"]
    assert history["2026-08-12"]["target_rates"][0]["current"] == 55.0


def test_record_snapshot_skips_without_target_rates(tmp_path):
    path = tmp_path / "fedwatch.json"
    assert record_snapshot({"target_rates": []}, date(2026, 8, 12), path) is False
    assert not path.exists()


def test_record_snapshot_prunes_old_entries(tmp_path):
    path = tmp_path / "fedwatch.json"
    _write_history(path, {"2026-01-01": _fedwatch([_rate("3.50-3.75", 40.0)])})

    record_snapshot(_fedwatch([_rate("3.50-3.75", 50.0)]), date(2026, 8, 12), path)
    history = json.loads(path.read_text())
    assert "2026-01-01" not in history  # 120 日超は剪定
    assert "2026-08-12" in history


def test_prev_day_from_history(tmp_path):
    path = tmp_path / "fedwatch.json"
    _write_history(path, {"2026-08-11": _fedwatch([_rate("3.50-3.75", 45.2)])})

    deltas = compute_deltas(_fedwatch([_rate("3.50-3.75", 50.9)]), date(2026, 8, 12), path)
    assert deltas["prev_day"]["by_range"]["3.50-3.75"] == 5.7
    assert "履歴 1日前" in deltas["prev_day"]["source"]


def test_prev_day_weekend_gap_uses_friday(tmp_path):
    """月曜実行時、3 日前（金曜）のスナップショットで前日比を出す。"""
    path = tmp_path / "fedwatch.json"
    _write_history(path, {"2026-08-07": _fedwatch([_rate("3.50-3.75", 48.0)])})  # 金曜

    deltas = compute_deltas(_fedwatch([_rate("3.50-3.75", 50.0)]), date(2026, 8, 10), path)  # 月曜
    assert deltas["prev_day"]["by_range"]["3.50-3.75"] == 2.0
    assert "履歴 3日前" in deltas["prev_day"]["source"]


def test_prev_week_prefers_exactly_seven_days(tmp_path):
    path = tmp_path / "fedwatch.json"
    _write_history(path, {
        "2026-08-05": _fedwatch([_rate("3.50-3.75", 37.9)]),  # 7 日前
        "2026-08-06": _fedwatch([_rate("3.50-3.75", 40.0)]),  # 6 日前
    })

    deltas = compute_deltas(_fedwatch([_rate("3.50-3.75", 50.9)]), date(2026, 8, 12), path)
    assert deltas["prev_week"]["by_range"]["3.50-3.75"] == 13.0  # 7 日前を優先
    assert "履歴 7日前" in deltas["prev_week"]["source"]


def test_fallback_to_investing_fields_when_no_history(tmp_path):
    path = tmp_path / "fedwatch.json"  # 履歴なし
    fw = _fedwatch([_rate("3.50-3.75", 50.9, prev_day=50.9, prev_week=37.9)])

    deltas = compute_deltas(fw, date(2026, 8, 12), path)
    assert deltas["prev_day"]["by_range"]["3.50-3.75"] == 0.0
    assert deltas["prev_day"]["source"] == "Investing prev_day"
    assert deltas["prev_week"]["by_range"]["3.50-3.75"] == 13.0
    assert deltas["prev_week"]["source"] == "Investing prev_week"


def test_meeting_rollover_guard_falls_back_to_investing(tmp_path):
    """FOMC 会合切替後はレンジ非互換のため履歴比較を放棄し Investing 値を使う。"""
    path = tmp_path / "fedwatch.json"
    _write_history(path, {"2026-08-11": _fedwatch([_rate("3.75-4.00", 60.0)], meeting="Sep 16, 2026")})

    fw = _fedwatch([_rate("3.50-3.75", 50.9, prev_day=49.0)], meeting="Oct 28, 2026")
    deltas = compute_deltas(fw, date(2026, 8, 12), path)
    assert deltas["prev_day"]["source"] == "Investing prev_day"
    assert deltas["prev_day"]["by_range"]["3.50-3.75"] == 1.9
    assert "会合切替" in deltas["note"]


def test_no_history_no_investing_returns_none(tmp_path):
    path = tmp_path / "fedwatch.json"
    deltas = compute_deltas(_fedwatch([_rate("3.50-3.75", 50.9)]), date(2026, 8, 12), path)
    assert deltas["prev_day"] is None
    assert deltas["prev_week"] is None


def test_same_day_snapshot_excluded_from_comparison(tmp_path):
    """当日分スナップショットは比較対象にしない（再実行の上書きと独立）。"""
    path = tmp_path / "fedwatch.json"
    _write_history(path, {"2026-08-12": _fedwatch([_rate("3.50-3.75", 99.0)])})

    deltas = compute_deltas(_fedwatch([_rate("3.50-3.75", 50.9)]), date(2026, 8, 12), path)
    assert deltas["prev_day"] is None


def test_format_delta_lines_renders_both_deltas():
    fw = _fedwatch([_rate("3.50-3.75", 50.9), _rate("3.75-4.00", 49.1)])
    fw["deltas"] = {
        "prev_day": {"by_range": {"3.50-3.75": -5.7, "3.75-4.00": 5.7}, "source": "履歴 1日前 (2026-08-11)"},
        "prev_week": {"by_range": {"3.50-3.75": 13.0, "3.75-4.00": None}, "source": "Investing prev_week"},
        "note": None,
    }
    lines = format_delta_lines(fw)
    text = "\n".join(lines)
    assert "3.50-3.75: 現在 50.9% | 前日比 -5.7pp | 前週比 +13.0pp" in text
    assert "3.75-4.00: 現在 49.1% | 前日比 +5.7pp | 前週比 N/A" in text
    assert "前日比ソース: 履歴 1日前 (2026-08-11)" in text
    assert "前週比ソース: Investing prev_week" in text


def test_format_delta_lines_without_rates_returns_empty():
    assert format_delta_lines({"target_rates": []}) == []
