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
    assert ms.compute_surprise(_ev("CPI (MoM) (Aug)", "0.4%", "0.2%"))["unit"] == "pp"
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
    assert "4,350" in text and "4,300★（-12.2, -0.28%・参考変動額内）" in text and "QuikStrike" in text
    assert "±67.9ドル" in text and "±56.4ドル" in text  # 252営業日換算と365暦日換算
    assert "観測した注文集中（価格帯別の注文量）: 取得不可" in text
    no_gvz = "\n".join(format_liquidity_lines(build_liquidity(4312.21, {"error": "timeout"})))
    assert "参考変動額: 取得不可（GVZ timeout）" in no_gvz and "参考変動額内" not in no_gvz
    assert build_liquidity(float("nan"), {"value": 25.0})["levels"] == []
    assert build_liquidity(4000.0, {"value": float("nan")})["expected_move_1sd"] is None
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
    assert "94.0パーセンタイル" in lines  # 履歴 40〜64 の25件: 23件より大きく1件と同値 → (23+0.5)/25
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
    monkeypatch.setattr(main, "load_forecast_history", lambda: {})
    monkeypatch.setattr(main, "load_actual_history", lambda: {})
    monkeypatch.setattr(main, "archive_actuals", lambda events, now, known: 0)
    monkeypatch.setattr(main, "archive_forecasts", lambda events, now: 0)
    results = {"timestamp": "2026-09-24T09:00:51",
               "economic_calendar": {"events": [_ev("Initial Jobless Claims", "N/A", "201K")]},
               "retail_sentiment": {"XAUUSD": {"source": "FXSSI", "long_pct": 62.4}}}
    main.enrich_offchart_inputs(results, now=datetime(2026, 9, 24, 9, 1, tzinfo=ms.JST),
                                gvz_fetch=lambda sid: {"value": 24.0, "as_of_date": "2026-09-23"},
                                news_builder=lambda now, hours: {"candidate_count": 0, "kept": [], "sources": [],
                                                                 "selection": {"mode": "skipped"}},
                                save=False)
    assert results["economic_calendar"]["forecast_fallback"]["error"].startswith("ForexFactory")
    assert results["macro_surprises"] == []
    assert results["positioning"]["snapshot"]["retail"]["XAUUSD"]["long_pct"] == 62.4
    assert (tmp_path / "positioning.jsonl").exists()
    status = {row["item"]: row["available"] for row in results["input_status"]}
    assert status[6] is True and status[2] is False and status[3] is False
    assert "取得済み" in main.format_input_status_lines(results["input_status"])[1]


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


def test_forecast_fill_rejects_ambiguous_or_different_qualifiers():
    when = datetime(2026, 10, 13, 21, 30, tzinfo=ms.JST)
    ff = [{"country": "United States", "title": "Core CPI m/m", "forecast": "0.3%", "datetime_jst": when},
          {"country": "United States", "title": "CPI m/m", "forecast": "0.2%", "datetime_jst": when},
          {"country": "United States", "title": "CPI y/y", "forecast": "2.9%", "datetime_jst": when}]
    base = dict(date="Tuesday, October 13, 2026", time="21:30")
    headline = [_ev("CPI (MoM) (Sep)", "N/A", "N/A", **base)]
    core = [_ev("Core CPI (MoM) (Sep)", "N/A", "N/A", **base)]
    assert ms.fill_missing_forecasts(headline, ff) == 1          # ForexFactory の実表記 m/m のまま
    assert headline[0]["forecast"] == "0.2%"
    assert ms.fill_missing_forecasts(core, ff) == 1
    assert core[0]["forecast"] == "0.3%"
    twins = [_ev("CPI (MoM) (Sep)", "N/A", "N/A", **base)]
    dup = [{"country": "United States", "title": "CPI MoM", "forecast": x, "datetime_jst": when} for x in ("0.2%", "0.3%")]
    assert ms.fill_missing_forecasts(twins, dup) == 0 and twins[0]["forecast"] == "N/A"


def test_percentile_needs_variation_and_uses_mid_rank():
    assert ph.percentile_rank([50.0] * 30, 50.0) is None           # 変化のない履歴
    values = [float(v) for v in range(40, 70)]
    assert ph.percentile_rank(values, 55.0) == 51.7                 # (15 + 0.5) / 30
    assert ph.percentile_rank(values + [float("nan")], 55.0) == 51.7


def test_weekly_surprise_window_is_seven_days():
    now = datetime(2026, 9, 26, 7, 0, tzinfo=ms.JST)
    events = [_ev("PPI (MoM)", "0.1%", "0.2%", date="Monday, September 21, 2026")]
    assert ms.released_surprises(events, now) == []
    assert len(ms.released_surprises(events, now, lookback_hours=7 * 24)) == 1
    assert ms.format_surprise_lines([], 7 * 24)[0] == "### 指標サプライズ（発表済み・直近7日）"


def test_anchor_reads_iso_generation_time():
    from datetime import date
    from scrapers import report_anchor as ra
    text = "# t\nデータ基準日: 2026-09-26 ｜ 生成完了: 2026-09-26T20:42:18.484636+09:00 ｜ データ充足: 16/18\n"
    assert ra._extract_generated_at(text, date(2026, 9, 26)) == "2026-09-26T20:42+09:00"


def test_correlation_reports_periods_and_rejects_nan():
    from scrapers import correlation as cr
    assert cr._pearson([1.0, float("nan"), 3.0, 4.0], [1.0, 2.0, 3.0, 4.0]) is None
    days = [f"2026-{m:02d}-{d:02d}" for m in (6, 7, 8) for d in range(1, 29)][:70]
    xau = {d: 4000 + i * (1 if i % 2 else -1.5) for i, d in enumerate(days)}
    fred = {"DFII10": {"observations": [(d, 2.0 + i * (0.01 if i % 2 else -0.012)) for i, d in enumerate(days)]}}
    res = cr.build_correlations(xau, fred)
    pair = res["pairs"][0]
    assert pair["period_20d"].endswith(days[-1]) and pair["period_60d"]
    assert "（2026-" in "\n".join(cr.format_correlation_lines(res))


def test_liquidity_format_accepts_saved_json_without_calendar_move():
    saved = {"price": 4286.1, "levels": round_levels(4286.1), "gvz": 23.59, "gvz_as_of": "2026-09-22",
             "gvz_stale": False, "gvz_error": None, "expected_move_1sd": 63.7}   # 9/26 以前の保存形式
    text = "\n".join(format_liquidity_lines(saved))
    assert "±63.7ドル" in text and "暦日365日換算" not in text


def test_weekly_input_status_uses_prev_week():
    results = {"fedwatch": {"target_rates": [{"range": "3.75-4.00", "current": 33.4, "prev_day": None, "prev_week": 40.3}]}}
    daily = {r["item"]: r["available"] for r in main.build_input_status(results)}
    weekly = {r["item"]: r["available"] for r in main.build_input_status(results, weekly=True)}
    assert daily[3] is False and weekly[3] is True


def test_dxy_change_is_recomputed_from_previous_close():
    from scrapers.dxy import _reconcile_change
    r = {"current_price": 100.97, "prev_close": 101.24, "change": -0.31, "change_pct": -0.31}
    _reconcile_change(r)
    assert r["change"] == -0.27 and r["change_reported"] == -0.31 and "不一致" in r["change_note"]
    ok = {"current_price": 101.034, "prev_close": 101.249, "change": -0.215, "change_pct": None}
    _reconcile_change(ok)
    assert ok["change"] == -0.215 and "change_note" not in ok


def test_news_selection_uses_jev_scores_and_falls_back_to_keywords():
    from scrapers import news_triage as nt
    now = datetime(2026, 9, 26, 12, 0, tzinfo=ms.JST)
    body = b"""<rss><channel>
      <item><title>Gold rises as Fed cut bets grow</title><link>https://www.fxstreet.com/a</link><pubDate>Sat, 26 Sep 2026 01:00:00 GMT</pubDate></item>
      <item><title>South Korean Won supported by exports vs US Dollar</title><link>https://www.fxstreet.com/b</link><pubDate>Sat, 26 Sep 2026 01:30:00 GMT</pubDate></item>
      <item><title>Old story</title><link>https://www.fxstreet.com/c</link><pubDate>Tue, 22 Sep 2026 01:00:00 GMT</pubDate></item>
    </channel></rss>"""

    class Resp:
        status_code, content = 200, body
    cands, sources = nt.collect_headlines(now, 36, fetch=lambda url: Resp())
    assert len(cands) == 2 and sources[0]["items"] == 2      # 期間外を除外、同一見出しは重複除去
    kept = nt.select_headlines(cands, {"n1": 0.97, "n2": 0.2})
    assert [c["title"][:4] for c in kept[:1]] == ["Gold"] and kept[0]["p"] == 0.97
    fallback = nt.select_headlines(cands, None)
    assert {c["id"] for c in fallback} == {"n1", "n2"}      # キーワード規則は「dollar」も拾う（Jevが必要な理由）
    news = nt.build_news(now, 36, fetch=lambda url: Resp(),
                         runner=lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    assert news["selection"]["mode"] == "fallback" and "キーワード規則" in "\n".join(nt.format_news_lines(news))
    assert nt.parse_feed.__doc__ and nt.FEEDS
    try:
        nt.parse_feed(b"<!DOCTYPE x [<!ENTITY a 'b'>]><rss/>", "x", now, now)
        raise AssertionError("DTD must be rejected")
    except ValueError:
        pass


def test_offchart_features_are_machine_readable(tmp_path):
    from scrapers import offchart_features as of
    now = datetime(2026, 9, 29, 18, 0, tzinfo=ms.JST)
    results = {"timestamp": "2026-09-29T18:00:00",
               "economic_calendar": {"events": [
                   _ev("JOLTS Job Openings (Aug)", "N/A", "7.1M", date="Tuesday, September 29, 2026", time="23:00"),
                   _ev("Fed Interest Rate Decision", "N/A", "4.00%", date="Wednesday, October 28, 2026", time="03:00")]},
               "liquidity_levels": build_liquidity(4300.0, {"value": 23.0, "as_of_date": "2026-09-28"}),
               "fred": {"DFII10": {"value": 2.85, "change": 0.09, "change_20obs": 0.51, "as_of_date": "2026-09-24"}},
               "gold_etf": {"tonnes": 1054.55, "change_5d_t": 1.71, "change_20d_t": 9.05, "as_of_date": "2026-09-24"},
               "input_status": [{"item": 1, "available": True}]}
    feats = of.build_features(results, now)
    assert [b["event"] for b in feats["event_blackouts"]] == ["JOLTS Job Openings (Aug)"]   # 36時間内のみ
    assert feats["event_blackouts"][0]["window_start"].startswith("2026-09-29T22:30")
    assert feats["expected_move"]["expected_move_1sd"] == 62.3 and 4300.0 in feats["round_levels"]
    assert feats["rates"]["DFII10"]["change_20obs"] == 0.51 and feats["rates"]["DGS2"] is None
    of.save_features(feats, tmp_path)
    assert json.loads((tmp_path / "offchart_features_latest.json").read_text())["schema_version"] == 1
    assert len((tmp_path / "history" / "offchart_features.jsonl").read_text().splitlines()) == 1


def test_actual_first_seen_is_kept_and_revision_flagged(tmp_path):
    path = tmp_path / "f.jsonl"
    first = [_ev("Initial Jobless Claims", "215K", "201K")]
    t1 = datetime(2026, 9, 24, 22, 0, tzinfo=ms.JST)
    assert ms.archive_actuals(first, t1, set(), path) == 1
    assert ms.archive_actuals(first, t1, set(ms.load_actual_history(path)), path) == 0
    revised = [_ev("Initial Jobless Claims", "218K", "201K")]
    out = ms.released_surprises(revised, datetime(2026, 9, 25, 9, 0, tzinfo=ms.JST),
                                forecast_history=ms.load_forecast_history(path),
                                actual_history=ms.load_actual_history(path))
    assert out[0]["actual_first_seen"] == "215K" and "改定の可能性" in out[0]["actual_note"]
    assert ms.load_forecast_history(path) == {}          # 結果の記録は予想の履歴に混ざらない


def test_myfxbook_api_parses_outlook_and_always_logs_out():
    from scrapers.myfxbook import fetch_outlook_api
    calls = []

    class R:
        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data

    def get(url, params):
        calls.append(url.rsplit("/", 1)[-1])
        if url.endswith("login.json"):
            return R({"error": False, "session": "s"})
        if url.endswith("get-community-outlook.json"):
            return R({"error": False, "symbols": [{"name": "XAUUSD", "longPercentage": 67, "shortPercentage": 33,
                                                   "avgLongPrice": 4458.8, "avgShortPrice": 4088.7,
                                                   "longVolume": 1253.2, "shortVolume": 631.2,
                                                   "longPositions": 9478, "shortPositions": 4627}]})
        return R({"error": False})
    out = fetch_outlook_api("XAUUSD", "e", "p", get=get)
    assert out["long_pct"] == 67.0 and out["avg_long_entry"] == 4458.8 and out["long_positions"] == 9478
    assert calls == ["login.json", "get-community-outlook.json", "logout.json"]
    calls.clear()
    assert fetch_outlook_api("EURUSD", "e", "p", get=get) is None and calls[-1] == "logout.json"
    assert fetch_outlook_api("XAUUSD", "e", "p", get=lambda u, p: R({"error": True})) is None


def test_tv_gvz_preferred_when_newer_and_fresh(tmp_path):
    from scrapers import liquidity_levels as ll
    hist = tmp_path / "tv.jsonl"
    hist.write_text(json.dumps({"retrieved_at": "2026-09-26T14:15:36Z",
                                "gvz_latest": {"date": "2026-09-25", "close": 22.44}}) + "\n")
    now = datetime(2026, 9, 26, 23, 30, tzinfo=ms.JST)
    tv = ll.latest_tv_gvz(now, hist)
    assert tv["value"] == 22.44 and tv["as_of_date"] == "2026-09-25"
    fred = {"value": 23.59, "as_of_date": "2026-09-22"}
    chosen = ll.choose_gvz(fred, tv)
    assert chosen["source"].startswith("TradingView") and chosen["fred_value"] == 23.59
    liq = build_liquidity(4286.21, chosen)
    assert liq["gvz"] == 22.44 and liq["gvz_fred_as_of"] == "2026-09-22"
    text = "\n".join(format_liquidity_lines(liq))
    assert "TradingView CBOE:GVZ" in text and "FRED GVZCLS は 23.59（2026-09-22時点）" in text
    # 古い取得記録・FREDの方が新しい場合は FRED のまま
    assert ll.latest_tv_gvz(datetime(2026, 9, 27, 9, 0, tzinfo=ms.JST), hist) is None
    assert ll.choose_gvz({"value": 22.0, "as_of_date": "2026-09-25"}, tv)["source"] == "FRED GVZCLS"
    assert ll.choose_gvz({"error": "HTTPError"}, tv)["value"] == 22.44
    hist.write_text("not json\n")
    assert ll.latest_tv_gvz(now, hist) is None
