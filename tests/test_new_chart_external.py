"""2026-08-13 追加のチャート外要素のテスト。

対象: cot_disaggregated（機関内訳）/ correlation（相関定量）/ session_stats（セッション統計）

実行:
    .venv/bin/python3 -m pytest tests/test_new_chart_external.py -v
"""

from __future__ import annotations

import csv
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scrapers import correlation, session_stats  # noqa: E402
from scrapers import cot_disaggregated as cotd  # noqa: E402


# ---------------------------------------------------------------- COT 内訳

SAMPLE_ROW = {
    "report_date_as_yyyy_mm_dd": "2026-08-04T00:00:00.000",
    "open_interest_all": "371551",
    "m_money_positions_long_all": "139809",
    "m_money_positions_short_all": "9043",
    "change_in_m_money_long_all": "4716",
    "change_in_m_money_short_all": "-6255",
    "swap_positions_long_all": "20753",
    "swap__positions_short_all": "228388",
    "change_in_swap_long_all": "-2908",
    "change_in_swap_short_all": "12967",
    "prod_merc_positions_long": "15738",
    "prod_merc_positions_short": "34594",
    "other_rept_positions_long": "87204",
    "other_rept_positions_short": "20336",
    "traders_m_money_long_all": "81",
    "traders_m_money_short_all": "13",
}


def test_disaggregated_parses_managed_money_and_swap():
    d = cotd._parse_row(SAMPLE_ROW)
    assert d["date"] == "2026-08-04"
    mm = d["managed_money"]
    assert mm["net"] == 130766                    # 139809 - 9043
    assert mm["net_change"] == 10971              # 4716 - (-6255)
    assert mm["net_pct_oi"] == 35.2
    assert mm["traders_long"] == 81 and mm["traders_short"] == 13
    sw = d["swap_dealers"]
    assert sw["short"] == 228388
    assert sw["short_pct_oi"] == 61.5             # ディーラーの構造的ショート
    assert d["producer_merchant"]["net"] == -18856


def test_disaggregated_handles_missing_fields():
    d = cotd._parse_row({"report_date_as_yyyy_mm_dd": "2026-08-04T00:00:00.000"})
    assert d["managed_money"]["net"] is None
    assert d["swap_dealers"]["short_pct_oi"] is None


def test_disaggregated_format_reports_error():
    lines = cotd.format_disaggregated_lines({"error": "データなし", "data": None})
    assert "取得不可" in "\n".join(lines)


def test_disaggregated_format_includes_reading_guide():
    text = "\n".join(cotd.format_disaggregated_lines({"data": cotd._parse_row(SAMPLE_ROW)}))
    assert "Managed Money" in text and "Swap Dealers" in text
    assert "81 社 / Short 13 社" in text
    assert "Legacy" in text  # Legacy との違いの説明が入る


# ---------------------------------------------------------------- 相関

def _fred(series_id: str, values: list[float], start="2026-06-01") -> dict:
    """FRED 形式の observations（新しい順）を作る。"""
    base = datetime.strptime(start, "%Y-%m-%d").date()
    obs = [((base + timedelta(days=i)).isoformat(), v) for i, v in enumerate(values)]
    return {series_id: {"observations": list(reversed(obs))}}


def _closes(values: list[float], start="2026-06-01") -> dict:
    base = datetime.strptime(start, "%Y-%m-%d").date()
    return {(base + timedelta(days=i)).isoformat(): v for i, v in enumerate(values)}


def test_pearson_perfect_negative():
    assert abs(correlation._pearson([1, 2, 3, 4], [4, 3, 2, 1]) - (-1.0)) < 1e-9


def test_pearson_needs_variance():
    assert correlation._pearson([1, 1, 1, 1], [1, 2, 3, 4]) is None


def test_correlation_detects_normal_inverse_regime():
    """金が上がる日に実質金利が下がる系列 → 逆相関が「正常」と判定される。"""
    n = 40
    xau = [4000 + (10 if i % 2 == 0 else -10) * (i % 3 + 1) for i in range(n)]
    # 実質金利は XAU と逆向きに動かす
    rate = [2.0 - (0.01 if i % 2 == 0 else -0.01) * (i % 3 + 1) for i in range(n)]
    corr = correlation.build_correlations(_closes(xau), _fred("DFII10", rate))
    entry = next(p for p in corr["pairs"] if p["series_id"] == "DFII10")
    assert entry["r_20d"] is not None and entry["r_20d"] < -0.9
    assert entry["verdict"] == "正常"


def test_correlation_detects_sign_flip():
    """金と実質金利が同方向に動く（通常と逆）→ 反転判定。"""
    n = 40
    xau = [4000 + i * 5 + (7 if i % 2 else -7) for i in range(n)]
    rate = [2.0 + i * 0.01 + (0.02 if i % 2 else -0.02) for i in range(n)]
    corr = correlation.build_correlations(_closes(xau), _fred("DFII10", rate))
    entry = next(p for p in corr["pairs"] if p["series_id"] == "DFII10")
    assert entry["r_20d"] > 0.5
    assert "反転" in entry["verdict"]


def test_correlation_without_xau_data():
    corr = correlation.build_correlations({}, _fred("DFII10", [2.0] * 40))
    assert corr["error"] is not None


def test_correlation_reports_insufficient_overlap():
    corr = correlation.build_correlations(_closes([4000, 4010, 4020]), _fred("DFII10", [2.0, 2.1, 2.2]))
    entry = next(p for p in corr["pairs"] if p["series_id"] == "DFII10")
    assert "判定不能" in entry["verdict"]


def test_correlation_format_renders_table():
    n = 40
    corr = correlation.build_correlations(
        _closes([4000 + i for i in range(n)]), _fred("DFII10", [2.0 - i * 0.01 for i in range(n)])
    )
    text = "\n".join(correlation.format_correlation_lines(corr))
    assert "| ペア | 20日 r | 60日 r |" in text
    assert "日次リターン" in text  # 方法論の明示


# ---------------------------------------------------------------- セッション統計

def _write_h1(path: Path, days: list[dict]) -> None:
    """days: [{"date": date, "asia": (h,l), "london": (h,l,close)}] から H1 CSV を作る。"""
    rows = []
    for d in days:
        base = datetime(d["date"].year, d["date"].month, d["date"].day, tzinfo=timezone.utc)
        ah, al = d["asia"]
        for hour in range(0, 7):          # アジア
            rows.append((base + timedelta(hours=hour), ah, al, al))
        lh, ll, lc = d["london"]
        for hour in range(7, 12):         # London（最終足の close をセッション終値に）
            close = lc if hour == 11 else ll
            rows.append((base + timedelta(hours=hour), lh, ll, close))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "open", "high", "low", "close"])
        for ts, hi, lo, cl in rows:
            w.writerow([int(ts.timestamp() * 1000), lo, hi, lo, cl])


def _weekdays(n: int, start=datetime(2026, 1, 5)):
    out, d = [], start.date()
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def test_session_stats_counts_asia_sweep_and_reversal():
    """全日で London がアジア高値を上抜けし、終値はレンジ内へ戻る合成データ。"""
    import tempfile

    days = [
        {"date": d, "asia": (4100, 4000), "london": (4150, 4050, 4080)}
        for d in _weekdays(40)
    ]
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "h1.csv"
        _write_h1(path, days)
        st = session_stats.compute_session_stats(path, lookback=250)

    assert st["error"] is None
    assert st["asia_sweep"]["up_only_pct"] == 100.0
    assert st["asia_sweep"]["down_only_pct"] == 0.0
    # 上抜け後に終値 4080 < アジア高値 4100 → 全件が反転扱い
    assert st["asia_sweep"]["reversal_after_up_pct"] == 100.0


def test_session_stats_requires_minimum_sample():
    import tempfile

    days = [{"date": d, "asia": (4100, 4000), "london": (4150, 4050, 4080)} for d in _weekdays(5)]
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "h1.csv"
        _write_h1(path, days)
        st = session_stats.compute_session_stats(path)
    assert "標本不足" in st["error"]


def test_session_stats_missing_file():
    st = session_stats.compute_session_stats(Path("/nonexistent/h1.csv"))
    assert st["error"] is not None
    assert "H1 データなし" in st["error"]


def test_session_stats_format_includes_usage_note():
    st = {"sample_days": 250, "error": None,
          "asia_sweep": {"n": 250, "any_pct": 69.6, "up_only_pct": 39.6, "down_only_pct": 29.2,
                         "both_pct": 0.8, "none_pct": 30.4,
                         "reversal_after_up_pct": 51.5, "reversal_after_down_pct": 57.3},
          "pd_sweep": {"n": 249, "any_pct": 85.9, "pdh_only_pct": 45.4,
                       "pdl_only_pct": 34.5, "both_pct": 6.0},
          "weekday_range": [{"weekday": "月", "avg_range": 50.0, "vs_all_pct": -3.0, "n": 50}]}
    text = "\n".join(session_stats.format_session_stats_lines(st))
    assert "スイープ率" in text and "Judas swing" in text
    assert "待ちの妥当性判断" in text
