"""前回照合の時刻フィルタ + プール距離（近端/遠端）の回帰テスト。

2026-08-14 Daily で、前回レポート（8/13 18:07 JST 発行）の Draw 的中判定に
8/12 22:00 UTC のスイープ — 発行より 11 時間前の値動き — を使っていた。
予測より前に起きたことを実績に数えると Bias-Review-Log の判定が系統的に甘くなる。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scrapers import retail_analytics as ra  # noqa: E402
from scrapers.report_anchor import _extract_generated_at  # noqa: E402

JST = timezone(timedelta(hours=9))


def _bars(start: datetime, highs_lows: list[tuple[float, float]]) -> list[dict]:
    """(high, low) の列から H1 バー列を作る（close は low と high の中間）。"""
    out = []
    for i, (hi, lo) in enumerate(highs_lows):
        out.append({
            "ts": start + timedelta(hours=i),
            "open": (hi + lo) / 2,
            "high": hi,
            "low": lo,
            "close": (hi + lo) / 2,
        })
    return out


def test_sweep_before_previous_report_is_flagged():
    start = datetime(2026, 8, 13, 6, 0, tzinfo=timezone.utc)
    # 3 本目（08:00 UTC = 17:00 JST）で BSL を刈る
    bars = _bars(start, [(4380, 4360), (4385, 4370), (4410, 4390), (4395, 4380)])
    pools = {"bsl": [{"low": 4400, "high": 4435, "volume_sum": 100}], "ssl": []}

    # 前回レポート発行が刈りの後（18:07 JST = 09:07 UTC）→ 照合対象外
    events = ra.detect_sweeps(pools, bars, atr=20.0,
                              prev_report_at="2026-08-13T18:07:00+09:00")
    assert events[0]["verdict"].startswith("sweep")
    assert events[0]["after_prev_report"] is False

    # 前回レポート発行が刈りの前（12:00 JST = 03:00 UTC）→ 照合可
    events = ra.detect_sweeps(pools, bars, atr=20.0,
                              prev_report_at="2026-08-13T12:00:00+09:00")
    assert events[0]["after_prev_report"] is True


def test_without_prev_report_flag_is_none():
    start = datetime(2026, 8, 13, 6, 0, tzinfo=timezone.utc)
    bars = _bars(start, [(4410, 4390), (4395, 4380)])
    pools = {"bsl": [{"low": 4400, "high": 4435}], "ssl": []}
    events = ra.detect_sweeps(pools, bars, atr=20.0)
    assert events[0]["after_prev_report"] is None


def test_format_marks_events_and_publication_time():
    block = {
        "current_price": 4354.66,
        "prev_report_at": "2026-08-13T18:07+09:00",
        "sweep_events": [
            {"side": "BSL", "low": 4397.05, "high": 4435.03, "verdict": "sweep→reversal",
             "swept_at_utc": "2026-08-12 22:00", "retrace_atr": 1.39, "after_prev_report": False},
            {"side": "SSL", "low": 4275.26, "high": 4280.0, "verdict": "sweep→continuation",
             "swept_at_utc": "2026-08-14 01:00", "retrace_atr": 0.2, "after_prev_report": True},
        ],
    }
    text = "\n".join(ra.format_retail_analytics_lines(block))
    assert "前回レポート発行: 2026-08-13T18:07+09:00" in text
    assert "[発行前 = 照合対象外]" in text
    assert "[発行後 = 照合可]" in text


def test_pool_distance_near_far_and_straddle():
    cp = 4354.66
    # 現値を跨ぐ BSL（2026-08-14 の実データ）
    straddling = ra.pool_distances({"low": 4353.29, "high": 4407.63}, cp, "bsl")
    assert straddling["straddles_price"] is True
    assert straddling["distance_near_pct"] < 0 < straddling["distance_far_pct"]

    # 通常の BSL（現値より上）
    above = ra.pool_distances({"low": 4400.0, "high": 4435.0}, cp, "bsl")
    assert above["straddles_price"] is False
    assert 0 < above["distance_near_pct"] < above["distance_far_pct"]

    # SSL は近端が high、遠端が low（どちらも負）
    below = ra.pool_distances({"low": 4275.26, "high": 4300.0}, cp, "ssl")
    assert below["distance_far_pct"] < below["distance_near_pct"] < 0


def test_format_shows_range_and_straddle_note():
    block = {
        "current_price": 4354.66,
        "top_pools": {
            "bsl": [{"low": 4353.29, "high": 4407.63, "volume_sum": 251, "share_pct": 97.3,
                     **ra.pool_distances({"low": 4353.29, "high": 4407.63}, 4354.66, "bsl")}],
            "ssl": [{"low": 4275.26, "high": 4275.26, "volume_sum": 17, "share_pct": 100.0,
                     **ra.pool_distances({"low": 4275.26, "high": 4275.26}, 4354.66, "ssl")}],
        },
        "sweep_events": [],
    }
    text = "\n".join(ra.format_retail_analytics_lines(block))
    assert "現値を内包" in text, "現値を跨ぐ帯は跨ぎと明示する"
    assert "-1.82%" in text, "SSL は近端〜遠端の実数が出る"


def test_extract_generated_at_from_report_header():
    from datetime import date
    header = (
        "# ICT Daily Bias Report — 2026-08-14\n"
        "データ基準日: 2026-08-14 ｜ 生成: 08/14 08:05 JST ｜ データ充足: 18/18\n"
    )
    assert _extract_generated_at(header, date(2026, 8, 14)) == "2026-08-14T08:05+09:00"
    # 年つき表記も許容
    assert _extract_generated_at("生成: 2026-08-13 18:07 JST", date(2026, 8, 13)) == \
        "2026-08-13T18:07+09:00"
    # 取れなければ None（時刻フィルタなしで動く）
    assert _extract_generated_at("# タイトルのみ", date(2026, 8, 14)) is None
