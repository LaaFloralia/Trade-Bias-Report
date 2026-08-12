"""retail_analytics（P/L 構造・リクイディティプール・スイープ検証）のテスト。

実行:
    .venv/bin/python3 -m pytest tests/test_retail_analytics.py -v
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scrapers import retail_analytics as ra  # noqa: E402


# ---------------------------------------------------------------- fixtures

def _write_h1_csv(path: Path, bars: list[tuple[datetime, float, float, float, float]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "open", "high", "low", "close"])
        for ts, o, h, lo, c in bars:
            w.writerow([int(ts.timestamp() * 1000), o, h, lo, c])


def _flat_history(path: Path, base_price: float, days: int = 30,
                  end: datetime | None = None) -> list:
    """ATR 計算が成立する程度の日足履歴を H1 で合成する（日次レンジ = 10）。"""
    end = end or datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    bars = []
    start = end - timedelta(days=days)
    ts = start
    while ts <= end:
        # 各時間足: レンジ ±5 の往復（日足 TR ≒ 10 になる）
        bars.append((ts, base_price, base_price + 5, base_price - 5, base_price))
        ts += timedelta(hours=1)
    return bars


def _pool(low, high, volume=50, share=60.0):
    return {"low": low, "high": high, "volume_sum": volume, "entries": 3, "share_pct": share}


# ---------------------------------------------------------------- P/L 構造

def test_pl_structure_identifies_losing_side_as_fuel():
    retail = {
        "long_pct": 41.0, "short_pct": 59.0,
        "avg_long_entry": 4506.46, "avg_short_entry": 4136.07,
        "long_volume_lots": 841.4, "short_volume_lots": 1226.16,
        "long_positions": 7775, "short_positions": 6956,
    }
    pl = ra.build_pl_structure(retail, current_price=4396.0)
    assert pl["short"]["state"] == "含み損"       # 4136 で売り → 4396 で -6.28%
    assert pl["short"]["pl_pct"] == -6.28
    assert pl["long"]["state"] == "含み損"        # 4506 で買い → 4396 で -2.45%
    assert pl["long"]["pl_pct"] == -2.45


def test_pl_structure_handles_missing_avg_entries():
    retail = {"long_pct": 40.0, "short_pct": 60.0,
              "avg_long_entry": None, "avg_short_entry": None}
    pl = ra.build_pl_structure(retail, current_price=100.0)
    assert pl["short"]["pl_pct"] is None
    assert pl["short"]["pct"] == 60.0


# ---------------------------------------------------------------- ATR / H1

def test_atr20d_from_synthetic_bars(tmp_path):
    csv_path = tmp_path / "h1.csv"
    _write_h1_csv(csv_path, _flat_history(csv_path, 4400.0, days=30))
    bars = ra.load_h1_bars(csv_path)
    atr = ra.compute_atr20d(bars)
    assert atr is not None
    assert 9.0 <= atr <= 11.0  # 日次レンジ 10 の合成データ


def test_load_h1_bars_missing_file_returns_empty(tmp_path):
    assert ra.load_h1_bars(tmp_path / "nonexistent.csv") == []


# ---------------------------------------------------------------- スイープ検証

def _sweep_scenario(tmp_path, post_sweep_closes: list[float]):
    """現値 4400、BSL プール 4430-4435。スイープ後の値動きを引数で制御。"""
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    bars = _flat_history(tmp_path, 4400.0, days=30, end=end - timedelta(hours=6))
    # 直近 6 時間: スイープ足（高値 4440 でプール 4430 を刈る）→ その後の推移
    sweep_ts = end - timedelta(hours=5)
    bars.append((sweep_ts, 4400.0, 4440.0, 4395.0, 4438.0))
    ts = sweep_ts
    for c in post_sweep_closes:
        ts += timedelta(hours=1)
        bars.append((ts, c, c + 2, c - 2, c))
    csv_path = tmp_path / "h1.csv"
    _write_h1_csv(csv_path, bars)
    return ra.load_h1_bars(csv_path)


def test_sweep_then_reversal(tmp_path):
    # スイープ後に大きく反落（4440 → 4420、ATR≒10 の 0.5 倍 = 5 を大幅超過）
    bars = _sweep_scenario(tmp_path, [4435.0, 4428.0, 4420.0])
    pools = {"bsl": [_pool(4430.0, 4435.0)], "ssl": []}
    events = ra.detect_sweeps(pools, bars, atr=10.0)
    assert len(events) == 1
    assert events[0]["verdict"] == "sweep→reversal"
    assert events[0]["retrace_atr"] >= 1.5


def test_sweep_then_continuation(tmp_path):
    # スイープ後も高値圏を維持（戻りが 0.5×ATR 未満）
    bars = _sweep_scenario(tmp_path, [4439.0, 4441.0, 4440.0])
    pools = {"bsl": [_pool(4430.0, 4435.0)], "ssl": []}
    events = ra.detect_sweeps(pools, bars, atr=10.0)
    assert events[0]["verdict"] == "sweep→continuation"


def test_no_sweep_when_pool_untouched(tmp_path):
    bars = _sweep_scenario(tmp_path, [4400.0])
    pools = {"bsl": [_pool(4470.0, 4475.0)], "ssl": [_pool(4330.0, 4335.0)]}
    events = ra.detect_sweeps(pools, bars, atr=10.0)
    assert all(e["verdict"] == "no sweep" for e in events)
    assert len(events) == 2


# ---------------------------------------------------------------- プール履歴

def test_record_and_load_baseline_pools(tmp_path):
    path = tmp_path / "pools.json"
    oo = {"bsl_candidates": [_pool(4430, 4435)], "ssl_candidates": [_pool(4350, 4355)]}
    assert ra.record_pools(oo, 4400.0, date(2026, 8, 11), path) is True

    baseline = ra.load_baseline_pools(date(2026, 8, 12), path)
    assert baseline is not None
    key, snap = baseline
    assert key == "2026-08-11"
    assert snap["bsl"][0]["low"] == 4430


def test_baseline_ignores_same_day_and_too_old(tmp_path):
    path = tmp_path / "pools.json"
    oo = {"bsl_candidates": [_pool(1, 2)], "ssl_candidates": []}
    ra.record_pools(oo, 100.0, date(2026, 8, 12), path)   # 当日
    ra.record_pools(oo, 100.0, date(2026, 8, 1), path)    # 11 日前（window 外）
    assert ra.load_baseline_pools(date(2026, 8, 12), path) is None


# ---------------------------------------------------------------- 統合

def test_build_retail_analytics_full_flow(tmp_path):
    csv_path = tmp_path / "h1.csv"
    _write_h1_csv(csv_path, _flat_history(csv_path, 4400.0, days=30))
    pools_path = tmp_path / "pools.json"

    # 前日プールを仕込む
    ra.record_pools(
        {"bsl_candidates": [_pool(4430, 4435)], "ssl_candidates": [_pool(4350, 4355)]},
        4400.0, date.today() - timedelta(days=1), pools_path,
    )

    retail = {"long_pct": 41.0, "short_pct": 59.0,
              "avg_long_entry": 4506.46, "avg_short_entry": 4136.07}
    oo = {"current_price": 4400.0,
          "bsl_candidates": [_pool(4430, 4435)],
          "ssl_candidates": [_pool(4350, 4355, volume=30, share=40.0)]}

    result = ra.build_retail_analytics(
        retail=retail, open_orders=oo, current_price=4400.0,
        h1_path=csv_path, pools_path=pools_path,
    )
    assert result["error"] is None
    assert result["atr20d"] is not None
    assert result["baseline_date"] is not None          # 前日プールを採用
    assert result["baseline_note"] is None
    assert len(result["sweep_events"]) == 2
    assert result["top_pools"]["bsl"][0]["distance_pct"] is not None
    # 当日プールが履歴に保存されている
    history = json.loads(pools_path.read_text())
    assert date.today().isoformat() in history

    # 整形出力のスモーク
    lines = ra.format_retail_analytics_lines(result)
    text = "\n".join(lines)
    assert "### リテール分析" in text
    assert "損切り燃料" in text
    assert "リクイディティプール" in text
    assert "スイープ検証" in text


def test_build_retail_analytics_without_h1(tmp_path):
    result = ra.build_retail_analytics(
        retail={}, open_orders={}, current_price=None,
        h1_path=tmp_path / "missing.csv", pools_path=tmp_path / "pools.json",
    )
    assert result["error"] is not None
    assert result["sweep_events"] == []
