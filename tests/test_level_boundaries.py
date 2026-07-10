"""twelvedata の前週・前月レベル境界 (_calc_prev_periods) を検証する。

旧実装はローリング 5 本 / 22 本の高安を PWH/PWL・PMH/PML と誤ラベルしており、
現行週の混入や前月高値の取り逃しで xauusd-smc-quant (カレンダー境界) と乖離していた。
本テストは「現行週・当月のバーが混入しないこと」を判別できるフィクスチャで固定する。"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scrapers.twelvedata import _calc_prev_periods  # noqa: E402


def _bar(d: str, h: float, l: float) -> dict:
    return {"datetime": d, "high": str(h), "low": str(l)}


# 基準日 2026-07-08 (水)。今週月曜 = 7/6、前週 = 6/29(月)〜7/5(日)、前月 = 6月。
# 現行週のバーに極端値 (H 4600 / L 3900) を仕込み、旧ローリング実装との差を判別する。
BARS = [
    _bar("2026-07-08", 4130, 4050),   # 当日 (現行週)
    _bar("2026-07-07", 4600, 3900),   # 現行週 — 混入すると PWH/PMH/PWL が壊れる
    _bar("2026-07-06", 4180, 4080),   # 現行週 (月曜)
    _bar("2026-07-03", 4200, 4100),   # 前週
    _bar("2026-07-02", 4190, 3950),   # 前週
    _bar("2026-06-30", 4210, 4000),   # 前週かつ前月 (両方に算入されるのが正しい)
    _bar("2026-06-25", 4300, 4150),   # 前月
    _bar("2026-06-10", 4550, 4400),   # 前月 (月の高値)
    _bar("2026-06-03", 4450, 4300),   # 前月
]


def test_prev_week_is_monday_based_and_excludes_current_week():
    p = _calc_prev_periods(BARS)
    assert p["pwh"] == 4210  # 6/29〜7/5 の最大 (7/7 の 4600 は現行週なので除外)
    assert p["pwl"] == 3950  # 同最小 (7/7 の 3900 は除外)


def test_prev_month_is_calendar_month():
    p = _calc_prev_periods(BARS)
    assert p["pmh"] == 4550  # 6月の暦月高値 (旧ローリング22本なら 7/7 の 4600 を誤採用)
    assert p["pml"] == 4000  # 6/30 が前月にも算入される


def test_month_start_edge():
    # 基準日 2026-07-01 (水): 前週 = 6/22〜6/28、前月 = 6月全体
    bars = [
        _bar("2026-07-01", 4100, 4040),  # 当日 (7月・現行週)
        _bar("2026-06-30", 4210, 4000),  # 現行週だが前月 → PM* のみ算入
        _bar("2026-06-26", 4260, 4120),  # 前週 (金)
        _bar("2026-06-24", 4240, 4110),  # 前週
        _bar("2026-06-10", 4550, 4400),  # 前月のみ
    ]
    p = _calc_prev_periods(bars)
    assert (p["pwh"], p["pwl"]) == (4260, 4110)  # 6/30 は今週なので前週に入らない
    assert (p["pmh"], p["pml"]) == (4550, 4000)  # 6/30 は前月には入る


def test_graceful_without_datetime():
    # datetime 欠落バー (旧テストフィクスチャ形式) では None を返し「取得不可」表示に落ちる
    p = _calc_prev_periods([{"high": "100", "low": "90"}])
    assert p == {"pwh": None, "pwl": None, "pmh": None, "pml": None}
