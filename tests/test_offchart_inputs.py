"""チャート外の追加入力（2026-09-24）: 指標サプライズ・予想補完・キリ番・ポジショニング履歴"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import main  # noqa: E402
from scrapers import macro_surprise as ms  # noqa: E402
from scrapers import positioning_history as ph  # noqa: E402
from scrapers.liquidity_levels import build_liquidity, expected_daily_move, format_liquidity_lines, round_levels  # noqa: E402


def _ev(indicator, actual, forecast, *, country="United States", date="Thursday, September 24, 2026", time="21:30"):
    return {"date": date, "time_jst": time, "country": country, "indicator": indicator,
            "actual": actual, "forecast": forecast, "previous": "196K"}


def test_parse_value_handles_units_and_rejects_text():
    assert ms.parse_value("201K") == (201.0, "K")
    assert ms.parse_value("-0.3%") == (-0.3, "%")
    assert ms.parse_value("1,234.5") == (1234.5, "")
    assert ms.parse_value("N/A") is None
    assert ms.parse_value("") is None


def test_surprise_direction_follows_indicator_meaning():
    strong_claims = ms.compute_surprise(_ev("Initial Jobless Claims", "215K", "201K"))
    assert strong_claims["diff"] == 14 and strong_claims["gold_direction"] == "金に上向き"
    hot_cpi = ms.compute_surprise(_ev("CPI (MoM) (Aug)", "0.4%", "0.2%"))
    assert hot_cpi["gold_direction"] == "金に下向き"
    assert ms.compute_surprise(_ev("CPI (MoM) (Aug)", "0.2%", "0.2%"))["gold_direction"] == "予想どおり"
    assert ms.compute_surprise(_ev("GDP (QoQ)", "0.4%", "0.2%", country="Japan"))["gold_direction"] == "判定対象外"
    assert ms.compute_surprise(_ev("Retail Sales", "0.4%", "400K")) is None  # 単位不一致


def test_released_surprises_only_recent_events_with_actuals():
    now = datetime(2026, 9, 25, 9, 0, tzinfo=ms.JST)
    events = [_ev("Initial Jobless Claims", "215K", "201K"),
              _ev("New Home Sales", "N/A", "615K", time="23:00"),
              _ev("PPI (MoM)", "0.1%", "0.2%", date="Monday, September 21, 2026")]
    out = ms.released_surprises(events, now)
    assert [e["indicator"] for e in out] == ["Initial Jobless Claims"]
    assert out[0]["released_at_jst"].startswith("2026-09-24T21:30")


def test_fill_missing_forecasts_requires_country_time_and_name_match():
    when = datetime(2026, 9, 29, 23, 0, tzinfo=ms.JST)
    ff = [{"country": "United States", "title": "JOLTS Job Openings", "forecast": "7.10M", "datetime_jst": when},
          {"country": "United States", "title": "CB Consumer Confidence", "forecast": "", "datetime_jst": when}]
    events = [_ev("JOLTS Job Openings  (Aug)", "N/A", "N/A", date="Tuesday, September 29, 2026", time="23:00"),
              _ev("CB Consumer Confidence  (Sep)", "N/A", "N/A", date="Tuesday, September 29, 2026", time="23:00"),
              _ev("JOLTS Job Openings  (Aug)", "N/A", "N/A", country="Japan", date="Tuesday, September 29, 2026", time="23:00")]
    assert ms.fill_missing_forecasts(events, ff) == 1
    assert events[0]["forecast"] == "7.10M" and events[0]["forecast_source"] == "ForexFactory"
    assert events[1]["forecast"] == "N/A" and events[2]["forecast"] == "N/A"


def test_round_levels_mark_hundreds_and_stay_within_band():
    levels = round_levels(4312.21)
    assert all(abs(lv["distance_pct"]) <= 2.0 for lv in levels)
    assert [lv["level"] for lv in levels if lv["major"]] == [4300.0]
    assert [lv["level"] for lv in levels] == [4250.0, 4300.0, 4350.0]
    text = "\n".join(format_liquidity_lines(build_liquidity(4312.21, {"value": 25.0, "as_of_date": "2026-09-25"})))
    assert "4,350" in text and "4,300★（-12.2, -0.28%・想定値幅内）" in text and "QuikStrike" in text
    assert "±67.9ドル" in text  # 4312.21 × 25% / √252
    no_gvz = "\n".join(format_liquidity_lines(build_liquidity(4312.21, {"error": "timeout"})))
    assert "想定値幅: 取得不可（GVZ timeout）" in no_gvz and "想定値幅内" not in no_gvz
    assert "キリ番: 取得不可" in "\n".join(format_liquidity_lines(None))
    assert expected_daily_move(4000, 0) is None


def test_positioning_percentile_waits_for_enough_history(tmp_path):
    path = tmp_path / "positioning.jsonl"
    for i in range(25):
        ph.append_snapshot({"recorded_at": f"2026-09-{1 + i // 2:02d}T{9 + 9 * (i % 2):02d}:00:00",
                            "retail": {"XAUUSD": {"source": "FXSSI", "long_pct": 40.0 + i}},
                            "cot_gold_mm": None}, path)
    rows = ph.load_history(path)
    snap = {"recorded_at": "2026-09-24T09:00:00", "retail": {"XAUUSD": {"source": "FXSSI", "long_pct": 63.0}},
            "cot_gold_mm": {"report_date": "2026-09-22", "mm_net_pct_oi": 32.8}}
    lines = "\n".join(ph.format_positioning_lines(snap, rows))
    assert "96.0パーセンタイル" in lines
    assert "判定保留（履歴0週" in lines
    other_source = {**snap, "retail": {"XAUUSD": {"source": "IG", "long_pct": 63.0}}}
    assert "判定保留（履歴0件" in "\n".join(ph.format_positioning_lines(other_source, rows))


def test_backfill_uses_scraped_timestamps_once(tmp_path):
    src = tmp_path / "scraped_data_2026-09-11.json"
    src.write_text(json.dumps({"timestamp": "2026-09-11T22:52:07",
                               "retail_sentiment": {"XAUUSD": {"source": "FXSSI", "long_pct": 62.4}},
                               "cot_disaggregated": {"data": {"date": "2026-09-08",
                                                              "managed_money": {"net_pct_oi": 32.8}}}}))
    path = tmp_path / "positioning.jsonl"
    assert ph.backfill_from_scraped([src], path) == 1
    assert ph.backfill_from_scraped([src], path) == 0
    row = ph.load_history(path)[0]
    assert row["retail"]["XAUUSD"]["long_pct"] == 62.4 and row["cot_gold_mm"]["mm_net_pct_oi"] == 32.8


def test_enrich_offchart_inputs_survives_feed_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "fetch_ff_week", lambda: {"events": [], "error": "ForexFactory 取得失敗: Timeout"})
    monkeypatch.setattr(ph, "HISTORY_PATH", tmp_path / "positioning.jsonl")
    monkeypatch.setattr(main, "append_snapshot", lambda snap: ph.append_snapshot(snap, tmp_path / "positioning.jsonl"))
    monkeypatch.setattr(main, "load_history", lambda: ph.load_history(tmp_path / "positioning.jsonl"))
    results = {"timestamp": "2026-09-24T09:00:51",
               "economic_calendar": {"events": [_ev("Initial Jobless Claims", "N/A", "201K")]},
               "retail_sentiment": {"XAUUSD": {"source": "FXSSI", "long_pct": 62.4}}}
    main.enrich_offchart_inputs(results)
    assert results["economic_calendar"]["forecast_fallback"]["error"].startswith("ForexFactory")
    assert results["macro_surprises"] == []
    assert results["positioning"]["snapshot"]["retail"]["XAUUSD"]["long_pct"] == 62.4
    assert (tmp_path / "positioning.jsonl").exists()


def test_backfill_cot_history_adds_each_week_once(tmp_path, monkeypatch):
    rows = [{"report_date_as_yyyy_mm_dd": f"2026-09-{d:02d}T00:00:00.000", "open_interest_all": "400000",
             "m_money_positions_long_all": str(140000 + d), "m_money_positions_short_all": "10000"} for d in (15, 8)]

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return rows

    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: Resp())
    path = tmp_path / "p.jsonl"
    assert ph.backfill_cot_history("GOLD - COMMODITY EXCHANGE INC.", 2, path) == 2
    assert ph.backfill_cot_history("GOLD - COMMODITY EXCHANGE INC.", 2, path) == 0
    assert [r["cot_gold_mm"]["report_date"] for r in ph.load_history(path)] == ["2026-09-08", "2026-09-15"]


def test_surprise_prefers_forecast_recorded_before_release(tmp_path):
    path = tmp_path / "f.jsonl"
    pending = [_ev("Initial Jobless Claims", "N/A", "201K")]
    before = datetime(2026, 9, 24, 18, 0, tzinfo=ms.JST)
    assert ms.archive_forecasts(pending, before, path) == 1
    assert ms.archive_forecasts(pending, datetime(2026, 9, 24, 22, 0, tzinfo=ms.JST), path) == 0  # 発表後は記録しない
    released = [_ev("Initial Jobless Claims", "215K", "205K")]  # 発表後に予想表示が変わった場合
    out = ms.released_surprises(released, datetime(2026, 9, 25, 9, 0, tzinfo=ms.JST),
                                forecast_history=ms.load_forecast_history(path))
    assert out[0]["forecast"] == "201K" and out[0]["surprise"]["diff"] == 14
    assert out[0]["forecast_provenance"].startswith("発表前記録（2026-09-24 18:00")
    none = ms.released_surprises(released, datetime(2026, 9, 25, 9, 0, tzinfo=ms.JST))
    assert none[0]["forecast_provenance"].startswith("事前記録なし")


def test_cot_percentile_states_comparison_window():
    rows = [{"recorded_at": f"w{i}", "cot_gold_mm": {"report_date": f"2026-{1 + i // 4:02d}-{1 + i % 4 * 7:02d}",
                                                      "mm_net_pct_oi": float(i)}} for i in range(30)]
    snap = {"recorded_at": "x", "retail": {}, "cot_gold_mm": {"report_date": "2026-09-22", "mm_net_pct_oi": 15.0}}
    text = "\n".join(ph.format_positioning_lines(snap, rows))
    assert "比較窓 2026-01-01〜2026-08-08 の30週" in text


def test_report_anchor_falls_back_to_parent_passed_editions(tmp_path, monkeypatch):
    from scrapers import report_anchor as ra
    for mode, name, status, reviewed in (("weekly", "Weekly_Bias_Report_2026-09-26", "parent_passed", "2026-09-26T09:26"),
                                         ("daily", "Daily_Bias_Report_2026-09-25", "parent_passed", "2026-09-25T18:10"),
                                         ("daily", "Daily_Bias_Report_2026-09-26", "parent_passed", "2026-09-26T09:10"),
                                         ("daily", "Daily_Bias_Report_2026-09-24", "changes_requested", "2026-09-24T18:10")):
        ed = tmp_path / mode / "editions" / f"{name}-{status}"
        ed.mkdir(parents=True)
        (ed / f"{name}.md").write_text(f"# t\n\n## セクション0: エグゼクティブサマリー\n\n{name} 結論\n", encoding="utf-8")
        (ed / f"{name}.parent-review.json").write_text(json.dumps({"status": status, "reviewedAt": reviewed}))
    monkeypatch.setenv("BRAIN_PATH", str(tmp_path / "no-brain"))
    monkeypatch.setenv("REPORT_ANCHOR_FALLBACK_DIR", str(tmp_path))
    from datetime import date
    anchor = ra.load_report_anchor(today=date(2026, 9, 26))
    assert anchor["weekly"]["file"] == "Weekly_Bias_Report_2026-09-26.md"
    assert anchor["prev_daily"]["file"] == "Daily_Bias_Report_2026-09-25.md"  # 当日分・未通過版は使わない
    assert anchor["xau_tf"] is None
    assert "出所: chart-intel" in "\n".join(ra.format_anchor_lines(anchor))
