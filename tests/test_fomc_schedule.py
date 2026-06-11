"""G3 回帰テスト: FOMC 日程テーブルと枯渇警告 / COT 種別の整合"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from main import _get_fomc_metadata  # noqa: E402


def test_fomc_dates_cover_2026_and_2027():
    assert len(config.FOMC_DATES_2026) == 8
    assert len(config.FOMC_DATES_2027) == 8
    assert config.FOMC_DATES == config.FOMC_DATES_2026 + config.FOMC_DATES_2027
    # 2026-12 会合は公式日程 (Dec 8-9) — 旧値 12-15 への回帰を防ぐ
    assert "2026-12-08" in config.FOMC_DATES_2026
    assert "2026-12-15" not in config.FOMC_DATES_2026
    # すべて YYYY-MM-DD としてパース可能で昇順
    parsed = [datetime.strptime(d, "%Y-%m-%d").date() for d in config.FOMC_DATES]
    assert parsed == sorted(parsed)


def test_fomc_week_judgment_works_into_2027():
    # 2026-12-30（2026 年日程消化後）でも 2027-01-26 が次回として返る
    meta = _get_fomc_metadata(datetime(2026, 12, 30))
    assert meta["next_fomc_date"] == "2027-01-26"
    assert meta["is_fomc_week"] is False
    # 2027-06-07（月）は 6/8 開催週 → FOMC 週
    meta = _get_fomc_metadata(datetime(2027, 6, 7))
    assert meta["next_fomc_date"] == "2027-06-08"
    assert meta["is_fomc_week"] is True


def test_fomc_exhaustion_warning_90_days():
    last = datetime.strptime(config.FOMC_DATES[-1], "%Y-%m-%d")
    # 最終登録日の 200 日前: 警告なし
    meta = _get_fomc_metadata(datetime(2027, 1, 5))
    assert (last - datetime(2027, 1, 5)).days > config.FOMC_SCHEDULE_WARN_DAYS
    assert meta["schedule_warning"] is None
    # 最終登録日の 90 日前以内: 警告あり
    meta = _get_fomc_metadata(datetime(2027, 11, 1))
    assert meta["schedule_warning"] is not None
    assert "枯渇" in meta["schedule_warning"]
    # 全日程消化後も警告は維持される（未定表示 + 警告）
    meta = _get_fomc_metadata(datetime(2028, 1, 15))
    assert meta["next_fomc_date"].startswith("未定")
    assert meta["schedule_warning"] is not None


def test_cot_report_type_consistency():
    """プロンプトの COT 種別が実装 (Legacy Futures Only) と一致していること。"""
    for rel in ("master_prompt_deep.md", "master_prompt_deep_weekly.md",
                "master_prompt.md", "master_prompt_weekly.md"):
        src = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
        assert "Disaggregated" not in src, f"{rel}: Disaggregated 表記が残存"
    cot_src = (PROJECT_ROOT / "scrapers" / "cot.py").read_text(encoding="utf-8")
    assert "Legacy Futures Only" in cot_src
