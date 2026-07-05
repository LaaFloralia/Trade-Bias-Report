from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import intel  # noqa: E402


def test_find_latest_scraped_isolates_daily_and_weekly(tmp_path, monkeypatch):
    monkeypatch.setattr(intel, "OUTPUT_DIR", tmp_path)
    (tmp_path / "scraped_data_2026-07-01.txt").write_text("daily old", encoding="utf-8")
    (tmp_path / "scraped_data_2026-07-03.txt").write_text("daily new", encoding="utf-8")
    (tmp_path / "scraped_data_weekly_2026-07-04.txt").write_text("weekly", encoding="utf-8")

    assert intel.find_latest_scraped("daily").name == "scraped_data_2026-07-03.txt"
    assert intel.find_latest_scraped("weekly").name == "scraped_data_weekly_2026-07-04.txt"


def test_collect_data_quick_fails_when_matching_mode_is_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(intel, "OUTPUT_DIR", tmp_path)
    (tmp_path / "scraped_data_weekly_2026-07-04.txt").write_text("weekly", encoding="utf-8")

    try:
        intel.collect_data(False, False, "2026-07-05", quick=True)
    except RuntimeError as exc:
        assert "--quick" in str(exc)
        assert "daily" in str(exc)
    else:
        raise AssertionError("daily ファイルが無いのに RuntimeError にならない")
