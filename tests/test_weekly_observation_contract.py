"""Fixed public-shaped fixtures: source dates must not acquire publication times."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import main
from scrapers import cot, cot_disaggregated as cotd


OBSERVED = "2026-09-22"
STARTED = "2026-09-26T10:00:00Z"
RETRIEVED = "2026-09-26T10:00:05Z"


def freeze_collection_clock(monkeypatch, module):
    times = iter((STARTED, RETRIEVED))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime.fromisoformat(next(times).replace("Z", "+00:00"))
            return value.astimezone(tz or timezone.utc)

    monkeypatch.setattr(module, "datetime", Clock)


def legacy_row(day=OBSERVED):
    return {
        "report_date_as_yyyy_mm_dd": day + "T00:00:00.000",
        "open_interest_all": "1000",
        "noncomm_positions_long_all": "120",
        "noncomm_positions_short_all": "100",
        "comm_positions_long_all": "100",
        "comm_positions_short_all": "110",
        "nonrept_positions_long_all": "50",
        "nonrept_positions_short_all": "30",
    }


def test_legacy_observation_publication_and_completed_fetch_remain_distinct(monkeypatch):
    freeze_collection_clock(monkeypatch, cot)
    monkeypatch.setattr(cot, "_fetch_instrument", lambda _: [legacy_row()])
    result = cot.fetch_cot_data([("GOLD", "gold")])
    saved = json.loads(json.dumps(result))
    assert saved["report_date"] == saved["as_of_date"] == OBSERVED
    assert saved["timestamp"] == STARTED
    assert saved["retrieved_at"] == RETRIEVED
    # Neither the usual Friday schedule nor Saturday's fetch confirms publication.
    assert saved["published_at"] is None
    text = main.format_scraped_data({"timestamp": STARTED, "cot": saved})
    assert f"Report Date: {OBSERVED}（観測日）" in text
    assert "公表日時: 未確認" in text
    assert f"取得完了時刻: {RETRIEVED}" in text
    assert "2026-09-25" not in text


@pytest.mark.parametrize("day", ["2020-01-07", "2026-09-27"])
def test_rejected_legacy_observation_has_no_successful_retrieval_or_publication(monkeypatch, day):
    freeze_collection_clock(monkeypatch, cot)
    monkeypatch.setattr(cot, "_fetch_instrument", lambda _: [legacy_row(day)])
    result = cot.fetch_cot_data([("GOLD", "gold")])
    assert result["as_of_date"] is None
    assert result["published_at"] is result["retrieved_at"] is None
    assert "取得完了時刻: 未記録（採用値なし）" in result["text"]


def test_disaggregated_timing_survives_json_and_input_formatting(monkeypatch):
    freeze_collection_clock(monkeypatch, cotd)

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return [{
                "report_date_as_yyyy_mm_dd": OBSERVED + "T00:00:00.000",
                "open_interest_all": "1000",
                "m_money_positions_long_all": "120",
                "m_money_positions_short_all": "100",
                "swap_positions_long_all": "100",
                "swap__positions_short_all": "110",
                "prod_merc_positions_long": "50",
                "prod_merc_positions_short": "30",
            }]

    monkeypatch.setattr(cotd.requests, "get", lambda *args, **kwargs: Response())
    result = json.loads(json.dumps(cotd.fetch_cot_disaggregated("gold")))
    assert result["as_of_date"] == result["data"]["date"] == OBSERVED
    assert result["timestamp"] == STARTED
    assert result["retrieved_at"] == RETRIEVED
    assert result["published_at"] is None
    text = main.format_scraped_data({"timestamp": STARTED, "cot_disaggregated": result})
    assert f"Report Date: {OBSERVED}（観測日）" in text
    assert f"公表日時: 未確認 / 取得完了時刻: {RETRIEVED}" in text
    # Legacy saved inputs have only the start timestamp. It cannot fill the gap.
    result.pop("retrieved_at")
    text = "\n".join(cotd.format_disaggregated_lines(result))
    assert "公表日時: 未確認 / 取得完了時刻: 未記録" in text
    assert STARTED not in text


def test_weekly_template_has_no_order_inference_directives():
    prompt = (Path(__file__).resolve().parents[1] / "master_prompt_weekly.md").read_text()
    for obsolete in (
        "COT データ公開日:",
        "最新の公開日を必ず明記",
        "偏りが 60% 超 →",
        "含み損側の SL 集中帯 →",
        "リクイディティプール テーブル",
        "オーダーブック由来のリクイディティプール",
        "前日プール vs 実際",
        "リテール分析のプールを優先参照",
    ):
        assert obsolete not in prompt
    assert "Report Date は観測日" in prompt
    assert "通常の公開予定から実公表日時を推測しない" in prompt
    assert "固定比率60%だけで逆張り、過熱、Draw on Liquidity を導かない" in prompt
    assert "未検証の計算済み値も事実として転記しない" in prompt
    assert "比率（COT・個人）と前回アンカーは票にしない" in prompt
