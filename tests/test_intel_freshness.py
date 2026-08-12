from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import intel  # noqa: E402

VALID_OBJ = {
    "bias": 0.2,
    "no_trade": False,
    "no_trade_reason": None,
    "risk_events_next_24h": [],
    "positioning_summary": "リテールは中立。",
    "confidence": 0.6,
}


def test_extract_data_date_normal_and_fallback(monkeypatch):
    assert intel.extract_data_date(Path("scraped_data_2026-07-05.txt")) == "2026-07-05"
    assert intel.extract_data_date(Path("scraped_data_weekly_2026-07-05.txt")) == "2026-07-05"

    class FixedDatetime:
        @classmethod
        def now(cls):
            class D:
                @staticmethod
                def strftime(fmt):
                    return "2026-07-06"
            return D()

    monkeypatch.setattr(intel, "datetime", FixedDatetime)
    assert intel.extract_data_date(Path("bad_name.txt")) == "2026-07-06"


def test_cmd_brief_stale_data_adds_md_banner_and_json_metadata(tmp_path, monkeypatch):
    brain = tmp_path / "Brain"
    scraped = tmp_path / "scraped_data_2026-07-04.txt"
    scraped.write_text("XAUUSD 4000", encoding="utf-8")

    monkeypatch.setenv("BRAIN_PATH", str(brain))
    monkeypatch.setattr(intel, "INTEL_DIR", tmp_path / "intel")
    monkeypatch.setattr(intel, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(intel, "JSONL_PATH", tmp_path / "logs" / "intel_runs.jsonl")
    monkeypatch.setattr(
        intel, "collect_data",
        lambda weekly, reuse, date_str, quick=False, symbol=None: scraped,
    )

    from scrapers import xsearch_ingest

    monkeypatch.setattr(xsearch_ingest, "load_xsearch", lambda run_date: None)
    monkeypatch.setattr(xsearch_ingest, "LAST_SKIP_REASON", "file_not_found")
    monkeypatch.setattr(xsearch_ingest, "LAST_SOURCE_FILE", None)

    calls = []

    def runner(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return "# Report\nbody"
        return json.dumps(VALID_OBJ, ensure_ascii=False)

    monkeypatch.setattr(intel, "run_llm", runner)
    monkeypatch.setattr(intel, "run_llm_with_tools", runner)
    # PDF 発行 (subprocess → playwright → Drive コピー) はテストでは実行しない
    monkeypatch.setattr(intel, "publish_report_pdf", lambda md_path, timeout=300: {})

    rc = intel.cmd_brief(argparse.Namespace(weekly=False, reuse_data=False, quick=True))
    assert rc == 0

    md_path = next((brain / "Calendar" / "Daily-Bias").glob("Daily_Bias_Report_*.md"))
    md = md_path.read_text(encoding="utf-8")
    assert md.startswith("> [!warning] STALE DATA\n")
    assert "**2026-07-04 時点の取得データ**" in md

    json_path = next((tmp_path / "intel").glob("intel_daily_*.json"))
    obj = json.loads(json_path.read_text(encoding="utf-8"))
    assert obj["data_as_of"] == "2026-07-04"
    assert "generated_at" in obj

    run_log = json.loads((tmp_path / "logs" / "intel_runs.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert run_log["data_as_of"] == "2026-07-04"


def test_fallback_json_can_receive_pipeline_metadata():
    obj = intel.attach_pipeline_metadata(
        intel.fallback_intel_json("bad output"),
        "2026-07-04",
        "2026-07-05T10:00:00+09:00",
    )
    assert obj["no_trade"] is True
    assert obj["data_as_of"] == "2026-07-04"
    assert obj["generated_at"] == "2026-07-05T10:00:00+09:00"
