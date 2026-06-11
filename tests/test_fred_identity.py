"""G2 回帰テスト: FRED 拡張系列（DFII10/T10YIE）と恒等式チェック"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scrapers import fred  # noqa: E402
from scrapers.validation import validate_fred_identity, validate_all  # noqa: E402


def _entry(value, as_of="2026-06-10"):
    return {"value": value, "as_of_date": as_of, "stale": False, "error": None}


def test_series_config_has_dfii10_and_t10yie():
    assert fred.SERIES_CONFIG["DFII10"]["stale_days"] == 5
    assert fred.SERIES_CONFIG["T10YIE"]["stale_days"] == 5
    # main フロー取得対象に含まれる（実走で取得されることの静的ガード）
    assert fred.SERIES_IDS == ["DGS10", "DGS2", "DTWEXBGS", "DFII10", "T10YIE"]


def test_identity_ok_within_tolerance():
    fred_data = {
        "DGS10": _entry(4.53),
        "DFII10": _entry(2.10),
        "T10YIE": _entry(2.43),  # 2.10 + 2.43 = 4.53 → 差 0
    }
    assert validate_fred_identity(fred_data) == []
    # 丸め誤差レベル（差 0.02）も許容
    fred_data["T10YIE"] = _entry(2.45)
    assert validate_fred_identity(fred_data) == []


def test_identity_violation_detected():
    fred_data = {
        "DGS10": _entry(4.53),
        "DFII10": _entry(2.10),
        "T10YIE": _entry(2.80),  # 合計 4.90 → 差 0.37 > 0.05
    }
    issues = validate_fred_identity(fred_data)
    assert len(issues) == 1
    assert "恒等式違反" in issues[0]
    # validate_all 経由でも FRED キーに載る
    results = validate_all({"fred": fred_data})
    assert "FRED" in results


def test_identity_skipped_on_mismatched_as_of_or_missing():
    # as_of 不一致 → 偽陽性を出さない
    fred_data = {
        "DGS10": _entry(4.53, as_of="2026-06-10"),
        "DFII10": _entry(2.10, as_of="2026-06-09"),
        "T10YIE": _entry(2.80, as_of="2026-06-10"),
    }
    assert validate_fred_identity(fred_data) == []
    # 系列欠落 → 検査対象外
    assert validate_fred_identity({"DGS10": _entry(4.53)}) == []
    assert validate_fred_identity(None) == []
