from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scrapers import xsearch_ingest  # noqa: E402
from scripts import intel  # noqa: E402


def _config(tmp_path: Path, enabled: bool = True, max_age_hours: int = 30) -> dict:
    return {
        "enabled": enabled,
        "input_glob": str(tmp_path / "xsearch_*.json"),
        "max_age_hours": max_age_hours,
    }


def _payload(fetched_at: str) -> dict:
    return {
        "fetched_at": fetched_at,
        "tier1": [
            {
                "account": "NickTimiraos",
                "label": "Nick Timiraos (WSJ Fed)",
                "posts": [
                    {
                        "id": "1",
                        "url": "https://x.com/NickTimiraos/status/1",
                        "time_jst": "2026-07-05 07:12",
                        "text": "Fed officials signal patience.",
                    }
                ],
            }
        ],
        "tier2": {
            "summary": "リテールセンチメント要約。",
            "sample_posts": [{"id": "2", "url": "https://x.com/a/status/2", "text": "Gold bid."}],
        },
    }


def test_load_xsearch_disabled_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(xsearch_ingest, "load_x_search_config", lambda: _config(tmp_path, enabled=False))
    assert xsearch_ingest.load_xsearch("2026-07-05") is None
    assert xsearch_ingest.LAST_SKIP_REASON == "disabled"


def test_load_xsearch_missing_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(xsearch_ingest, "load_x_search_config", lambda: _config(tmp_path))
    assert xsearch_ingest.load_xsearch("2026-07-05") is None
    assert xsearch_ingest.LAST_SKIP_REASON == "file_not_found"


def test_load_xsearch_stale_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(xsearch_ingest, "load_x_search_config", lambda: _config(tmp_path, max_age_hours=1))
    old = (datetime.now().astimezone() - timedelta(hours=3)).isoformat(timespec="seconds")
    (tmp_path / "xsearch_2026-07-05.json").write_text(
        json.dumps(_payload(old), ensure_ascii=False), encoding="utf-8"
    )

    assert xsearch_ingest.load_xsearch("2026-07-05") is None
    assert xsearch_ingest.LAST_SKIP_REASON == "stale"


def test_load_xsearch_valid_file_formats_prompt_block(tmp_path, monkeypatch):
    monkeypatch.setattr(xsearch_ingest, "load_x_search_config", lambda: _config(tmp_path))
    fetched_at = datetime.now().astimezone().isoformat(timespec="seconds")
    path = tmp_path / "xsearch_2026-07-05.json"
    path.write_text(json.dumps(_payload(fetched_at), ensure_ascii=False), encoding="utf-8")

    data = xsearch_ingest.load_xsearch("2026-07-05")
    assert data is not None
    assert data["_source_file"] == str(path)

    block = xsearch_ingest.format_xsearch_block(data)
    assert "取り扱い指示" in block
    assert "Tier 1" in block
    assert "スコアリング表の正式拡張" in block


def test_load_xsearch_empty_tiers_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(xsearch_ingest, "load_x_search_config", lambda: _config(tmp_path))
    fetched_at = datetime.now().astimezone().isoformat(timespec="seconds")
    path = tmp_path / "xsearch_2026-07-05.json"
    path.write_text(
        json.dumps({"fetched_at": fetched_at, "tier1": [], "tier2": {}}, ensure_ascii=False),
        encoding="utf-8",
    )

    assert xsearch_ingest.load_xsearch("2026-07-05") is None
    assert xsearch_ingest.LAST_SKIP_REASON == "empty"


def test_extra_block_none_keeps_prompt_identical_to_no_extra_block():
    prompt_without_arg = intel.build_report_prompt(
        "daily", "MASTER", "SCRAPED", "2026-07-05", "2026-07-05"
    )
    prompt_with_none = intel.build_report_prompt(
        "daily", "MASTER", "SCRAPED", "2026-07-05", "2026-07-05", extra_block=None
    )
    assert prompt_with_none == prompt_without_arg
