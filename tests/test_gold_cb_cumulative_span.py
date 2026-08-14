"""中銀ゴールド（IMF IRFCL）の累計表示テスト。

`cumulative_3m_t` は確定月（速報月を除く）3 ヶ月の合計だが、内訳表示は速報月を含む
直近 3 ヶ月なので、累計に入っている確定月が内訳に現れないことがある
（2026-08-14 の実データ: 内訳は M07/M06/M05、累計 +136.4t は M04〜M06 の合計で
内訳 M06+M05 = +77.5t と一致しない）。読み手が検算できるよう対象月を明示する。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import main  # noqa: E402

BASE_DATA = {
    "timestamp": "2026-08-14T08:00:00",
    "gold_cb": {
        "source": "IMF IRFCL (monthly)",
        "months": [
            {"period": "2026-M07", "net_tonnes": 1.7, "reporters": 17, "partial": True,
             "top_movers": [], "excluded": []},
            {"period": "2026-M06", "net_tonnes": 44.1, "reporters": 64, "partial": False,
             "top_movers": [], "excluded": []},
            {"period": "2026-M05", "net_tonnes": 33.4, "reporters": 65, "partial": False,
             "top_movers": [], "excluded": []},
        ],
        "cumulative_3m_t": 136.4,
        "cumulative_periods": ["2026-M06", "2026-M05", "2026-M04"],
        "regime": "net_buying",
        "as_of_date": "2026-M07",
        "note": "IRFCL 報告国ベースの集計",
        "error": None,
    },
}


def _gold_cb_block(data: dict) -> str:
    text = main.format_scraped_data(data)
    start = text.index("### 中銀ゴールド購入")
    return text[start:start + 700]


def test_cumulative_span_is_shown():
    block = _gold_cb_block(BASE_DATA)
    assert "確定月3ヶ月累計（2026-M04〜2026-M06）" in block


def test_months_missing_from_breakdown_are_disclosed():
    block = _gold_cb_block(BASE_DATA)
    assert "累計に含まれるが上の内訳に出ていない確定月: 2026-M04" in block


def test_no_note_when_all_periods_are_visible():
    data = {**BASE_DATA, "gold_cb": {**BASE_DATA["gold_cb"],
                                     "cumulative_periods": ["2026-M06", "2026-M05"],
                                     "cumulative_3m_t": 77.5}}
    block = _gold_cb_block(data)
    assert "上の内訳に出ていない確定月" not in block
    assert "確定月3ヶ月累計（2026-M05〜2026-M06）: +77.5 t" in block


def test_missing_cumulative_periods_is_tolerated():
    """旧フォーマット（cumulative_periods なし）でも落ちない。"""
    gold = {k: v for k, v in BASE_DATA["gold_cb"].items() if k != "cumulative_periods"}
    block = _gold_cb_block({**BASE_DATA, "gold_cb": gold})
    assert "確定月3ヶ月累計: +136.4 t" in block
