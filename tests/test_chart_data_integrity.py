from __future__ import annotations

from datetime import date
from datetime import datetime as RealDateTime
from datetime import timezone

import pytest

import main
from scrapers import cot, cot_disaggregated as cotd
from scrapers.fedwatch import _parse_investing_body
from scrapers.twelvedata import _format_instrument
from scrapers.validation import (
    apply_validation,
    validate_all,
    validate_price_data,
    validate_twelvedata_instrument,
)


def _cot_row(report_date: str, base: int = 100) -> dict:
    return {
        "report_date_as_yyyy_mm_dd": report_date,
        "open_interest_all": str(base * 10),
        "noncomm_positions_long_all": str(base + 20),
        "noncomm_positions_short_all": str(base),
        "comm_positions_long_all": str(base),
        "comm_positions_short_all": str(base + 10),
        "nonrept_positions_long_all": str(base // 2),
        "nonrept_positions_short_all": str(base // 3),
        "change_in_open_interest_all": "5",
    }


def _freeze_cot_now(monkeypatch, iso_value: str = "2026-09-22T12:00:00+00:00") -> None:
    fixed_now = RealDateTime.fromisoformat(iso_value)

    class FixedDateTime(RealDateTime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_now.replace(tzinfo=None)
            return fixed_now.astimezone(tz)

    monkeypatch.setattr(cot, "datetime", FixedDateTime)


def _freeze_cot_disaggregated_now(
    monkeypatch, iso_value: str = "2026-09-22T12:00:00+00:00"
) -> None:
    fixed_now = RealDateTime.fromisoformat(iso_value)

    class FixedDateTime(RealDateTime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_now.replace(tzinfo=None)
            return fixed_now.astimezone(tz)

    monkeypatch.setattr(cotd, "datetime", FixedDateTime)


def test_fedwatch_target_ranges_are_not_misclassified_as_hold_or_hike():
    body = (
        "Meeting Time: Oct 28, 2026\n"
        "3.25 - 3.50\t80%\t80%\t80%\n"
        "3.50 - 3.75\t20%\t20%\t20%"
    )

    result = _parse_investing_body(body, today=date(2026, 9, 22))

    assert result["target_rates"] == [
        {"range": "3.25-3.50", "current": 80.0, "prev_day": 80.0, "prev_week": 80.0},
        {"range": "3.50-3.75", "current": 20.0, "prev_day": 20.0, "prev_week": 20.0},
    ]
    assert result["hold_pct"] is None
    assert result["hike_25bp_pct"] is None
    assert result["cut_25bp_pct"] is None


@pytest.mark.parametrize(
    ("body", "expected_error"),
    [
        (
            "Meeting Time: Jan 1, 2020\n3.25 - 3.50\t80%\t80%\t80%\n"
            "3.50 - 3.75\t20%\t20%\t20%",
            "次回FOMC日が過去",
        ),
        (
            "Meeting Time: Feb 30, 2027\n3.25 - 3.50\t80%\t80%\t80%\n"
            "3.50 - 3.75\t20%\t20%\t20%",
            "次回FOMC日が不正",
        ),
        (
            "Meeting Time: Oct 28, 2026\n3.25 - 3.50\t150%\t80%\t80%\n"
            "3.50 - 3.75\t20%\t20%\t20%",
            "current が0〜100の範囲外",
        ),
        (
            "Meeting Time: Oct 28, 2026\n3.25 - 3.50\t70%\t80%\t80%\n"
            "3.50 - 3.75\t20%\t20%\t20%",
            "current確率合計が100%と不整合",
        ),
        (
            "Meeting Time: Oct 28, 2026\n3.25 - 3.50\t0%\tbad%\t0%\n"
            "3.50 - 3.75\t100%\t100%\t100%",
            "一部行を数値として解析できない",
        ),
        ("Meeting Time: Oct 28, 2026", "レートレンジ別確率が未取得"),
    ],
)
def test_fedwatch_rejects_unusable_meeting_and_probability_data(body, expected_error):
    result = _parse_investing_body(body, today=date(2026, 9, 22))

    assert result["target_rates"] == []
    assert expected_error in result["error"]
    assert "raw_target_rates" in result


def test_fedwatch_accepts_probability_boundaries_and_rounded_total():
    result = _parse_investing_body(
        "Meeting Time: Oct 28, 2026\n"
        "3.25 - 3.50\t0%\t0%\t0%\n"
        "3.50 - 3.75\t99.9%\t100%\t100%",
        today=date(2026, 9, 22),
    )

    assert result["error"] is None
    assert [row["current"] for row in result["target_rates"]] == [0.0, 99.9]


def test_fedwatch_target_ranges_are_rendered_without_derived_policy_label():
    text = main.format_scraped_data({
        "timestamp": "2026-09-22T12:00:00Z",
        "fedwatch": {
            "target_rates": [
                {"range": "3.25-3.50", "current": 80.0, "prev_day": 80.0, "prev_week": 80.0},
                {"range": "3.50-3.75", "current": 20.0, "prev_day": 20.0, "prev_week": 20.0},
            ],
            "hold_pct": None,
            "cut_25bp_pct": None,
            "cut_50bp_pct": None,
            "hike_25bp_pct": None,
            "next_fomc_date": "Oct 28, 2026",
            "source": "test",
            "error": None,
        },
    })

    assert "3.25-3.50: 現在 80.0%" in text
    assert "分類: 未確認" in text
    assert "据え置き確率:" not in text


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, "not-a-number"])
def test_price_validation_rejects_non_finite_bool_and_bad_strings(bad):
    issues = validate_price_data("XAUUSD", {
        "current_price": bad,
        "prev_close": "2300.0",
        "change_pct": "0",
    })
    assert any(issue.startswith("現在価格が数値不正") for issue in issues)


@pytest.mark.parametrize("change", [0, -1.25, "0", "-1.25"])
def test_price_validation_accepts_zero_and_negative_finite_change(change):
    issues = validate_price_data("XAUUSD", {
        "current_price": "2350.0",
        "prev_close": "2340.0",
        "change_pct": change,
    })
    assert not any(issue.startswith("前日比が数値不正") for issue in issues)


def test_invalid_current_price_removes_only_affected_model_section_and_keeps_raw():
    raw_quote = {"close": "-1", "previous_close": "2300", "percent_change": "0"}
    scraped = {
        "_raw_quote_XAUUSD": raw_quote,
        "_raw_series_XAUUSD": [],
    }
    validation = validate_all(scraped)
    formatted = "\n".join([
        "=== Price Data (Twelve Data API) ===",
        "",
        "[XAUUSD]",
        "現在値: -1.00 | 前日終値: 2,300.00 | 前日比: +0.00 (+0.000%)",
        "当日: O 2,300.00 / H 2,310.00 / L 2,290.00 / C -1.00",
        "PDH: 2,305.00 / PDL: 2,280.00",
        "",
        "[USDJPY]",
        "現在値: 150.00 | 前日終値: 149.00 | 前日比: +1.00 (+0.671%)",
        "当日: O 149.00 / H 151.00 / L 148.00 / C 150.00",
    ])

    cleaned = apply_validation(formatted, validation)

    assert "[XAUUSD]\n価格セクション除外" in cleaned
    assert "現在値: -1.00" not in cleaned
    assert "当日: O 2,300.00" not in cleaned
    assert "[USDJPY]" in cleaned
    assert "現在値: 150.00" in cleaned
    assert scraped["_raw_quote_XAUUSD"] is raw_quote
    assert raw_quote["close"] == "-1"


@pytest.mark.parametrize("bad_current", ["bad", True])
def test_dxy_invalid_current_is_excluded_before_numeric_formatting(bad_current):
    text = main.format_scraped_data({
        "timestamp": "2026-09-22T12:00:00Z",
        "dxy": {
            "current_price": bad_current,
            "prev_close": "104",
            "change": "0",
            "change_pct": "0",
            "source": "test",
        },
    })

    assert "[DXY (スクレイピング)]" in text
    assert "価格セクション除外" in text
    assert "現在値:" not in text


def test_dxy_numeric_strings_are_formatted_safely():
    text = main.format_scraped_data({
        "timestamp": "2026-09-22T12:00:00Z",
        "dxy": {
            "current_price": "104.25",
            "prev_close": "104.00",
            "change": "0.25",
            "change_pct": "0.24",
            "source": "test",
        },
    })

    assert "現在値: 104.250 | 前日終値: 104.000" in text


@pytest.mark.parametrize(("bad", "expected"), [(True, "PDHが数値不正"), ("bad", "PDLが数値不正")])
def test_twelvedata_series_invalid_values_reach_validation_without_mutating_raw(bad, expected):
    quote = {"close": "2350", "previous_close": "2340", "percent_change": "0"}
    previous = {"high": bad if bad is True else "2355", "low": bad if bad == "bad" else "2335"}
    series = [{"high": "2360", "low": "2340"}, previous]

    issues = validate_twelvedata_instrument("XAUUSD", quote, series)

    assert any(issue.startswith(expected) for issue in issues)
    assert series[1] is previous
    assert previous["high"] is bad if bad is True else previous["low"] == "bad"


def test_twelvedata_weekly_aggregate_rejects_bool_instead_of_coercing_to_one():
    quote = {"close": "2350", "previous_close": "2340", "percent_change": "0"}
    series = [
        {"high": "2360", "low": "2340"},
        {"high": "2355", "low": "2335"},
        {"high": "2354", "low": "2334"},
        {"high": True, "low": "2333"},
        {"high": "2352", "low": "2332"},
        {"high": "2351", "low": "2331"},
    ]

    issues = validate_twelvedata_instrument("XAUUSD", quote, series)

    assert any(issue.startswith("PWHが数値不正") for issue in issues)
    assert series[3]["high"] is True


@pytest.mark.parametrize(
    ("field", "bad_value", "issue_prefix"),
    [
        ("change", "nan", "変化額が数値不正"),
        ("open", "nan", "当日始値が数値不正"),
        ("open", "-1", "当日始値がゼロまたは負数"),
        ("high", "inf", "当日高値が数値不正"),
        ("high", "-1", "当日高値がゼロまたは負数"),
        ("low", "nan", "当日安値が数値不正"),
        ("low", "-1", "当日安値がゼロまたは負数"),
    ],
)
def test_invalid_twelvedata_change_or_ohlc_is_removed_from_formatted_input(
    field, bad_value, issue_prefix
):
    quote = {
        "close": "2350",
        "previous_close": "2340",
        "change": "10",
        "percent_change": "0.427",
        "open": "2345",
        "high": "2360",
        "low": "2330",
    }
    quote[field] = bad_value
    price_text = "\n".join(
        [
            "=== Price Data (Twelve Data API) ===",
            "",
            *_format_instrument("XAUUSD", quote, []),
            "[USDJPY]",
            "有効な後続セクション",
        ]
    )
    text = main.format_scraped_data(
        {
            "timestamp": "2026-09-22T12:00:00Z",
            "price_data": price_text,
            "_raw_quote_XAUUSD": quote,
            "_raw_series_XAUUSD": [],
        }
    )

    assert "[XAUUSD]\n価格セクション除外" in text
    assert issue_prefix in text
    assert repr(bad_value).lower() not in text.lower()
    if bad_value == "-1":
        assert "(-1.0)" not in text
    assert "[USDJPY]\n有効な後続セクション" in text


def test_negative_twelvedata_change_amount_is_valid_when_finite():
    quote = {
        "close": "2340",
        "previous_close": "2350",
        "change": "-10",
        "percent_change": "-0.426",
        "open": "2345",
        "high": "2355",
        "low": "2335",
    }
    issues = validate_twelvedata_instrument("XAUUSD", quote, [])
    assert not any(issue.startswith("変化額") for issue in issues)


def test_invalid_previous_close_also_removes_price_section():
    issues = {"BTCUSD": ["前日終値が数値不正 ('bad')"]}
    cleaned = apply_validation(
        "[BTCUSD]\n現在値: 100.00 | 前日終値: bad | 前日比: +0.00 (+0.000%)\n当日: O 100 / H 101 / L 99 / C 100",
        issues,
    )
    assert "価格セクション除外" in cleaned
    assert "現在値: 100.00" not in cleaned


def test_invalid_last_price_section_does_not_remove_following_report_sections():
    cleaned = apply_validation(
        "[BTCUSD]\n現在値: nan | 前日終値: 100\n当日: O 100 / H 101 / L 99 / C nan\n\n"
        "### リテールポジション (Retail Sentiment)\n- BTCUSD: Long 55%",
        {"BTCUSD": ["現在価格が数値不正 ('nan')"]},
    )
    assert "現在値: nan" not in cleaned
    assert "### リテールポジション (Retail Sentiment)" in cleaned
    assert "- BTCUSD: Long 55%" in cleaned


def test_cot_uses_oldest_adopted_date_and_preserves_instrument_dates(monkeypatch):
    _freeze_cot_now(monkeypatch)
    rows = {
        "gold": [_cot_row("2026-09-15T00:00:00.000"), _cot_row("2026-09-08T00:00:00.000", 90)],
        "yen": [_cot_row("2026-09-16T00:00:00.000"), _cot_row("2026-09-09T00:00:00.000", 90)],
    }
    monkeypatch.setattr(cot, "_fetch_instrument", lambda market: rows[market])

    result = cot.fetch_cot_data([("GOLD", "gold"), ("JPY", "yen")])

    assert result["timestamp"] == "2026-09-22T12:00:00Z"
    assert result["as_of_date"] == "2026-09-15"
    assert result["report_date"] == "2026-09-15"
    assert result["instrument_dates"] == {"GOLD": "2026-09-15", "JPY": "2026-09-16"}
    assert "[GOLD]\nReport Date: 2026-09-15" in result["text"]
    assert "[JPY]\nReport Date: 2026-09-16" in result["text"]
    assert "(前週比:" in result["text"]
    assert result["source_url"] == cot.BASE_URL
    assert result["stale"] is False


@pytest.mark.parametrize(
    ("previous_date", "expected_label"),
    [
        ("2026-09-10T00:00:00.000", "前回比（2026-09-10）"),
        ("2026-09-02T00:00:00.000", "前回比（2026-09-02）"),
    ],
)
def test_cot_non_seven_day_comparison_is_not_labeled_previous_week(
    monkeypatch, previous_date, expected_label
):
    _freeze_cot_now(monkeypatch)
    monkeypatch.setattr(
        cot,
        "_fetch_instrument",
        lambda _market: [
            _cot_row("2026-09-16T00:00:00.000", 100),
            _cot_row(previous_date, 90),
        ],
    )

    result = cot.fetch_cot_data([("GOLD", "gold")])

    assert f"({expected_label}:" in result["text"]
    assert "(前週比:" not in result["text"]


@pytest.mark.parametrize(
    ("report_date", "expected"),
    [
        ("2020-01-07T00:00:00.000", "レポートが古いため現在判断から除外"),
        ("2026-09-23T00:00:00.000", "未来のレポート日付"),
        ("not-a-date", "レポート日付不正"),
        ("", "レポート日付欠損"),
    ],
)
def test_cot_excludes_old_future_and_invalid_dates(monkeypatch, report_date, expected):
    _freeze_cot_now(monkeypatch)
    monkeypatch.setattr(cot, "_fetch_instrument", lambda _market: [_cot_row(report_date)])

    result = cot.fetch_cot_data([("GOLD", "gold")])

    assert expected in result["text"]
    assert "Large Speculators:" not in result["text"]
    assert result["as_of_date"] is None
    assert result["stale"] is ("古い" in expected)


def test_cot_partial_failure_keeps_fresh_instrument_and_marks_error(monkeypatch):
    _freeze_cot_now(monkeypatch)

    def fake_fetch(market):
        if market == "gold":
            return [_cot_row("2026-09-16T00:00:00.000")]
        raise RuntimeError("temporary source failure")

    monkeypatch.setattr(cot, "_fetch_instrument", fake_fetch)
    result = cot.fetch_cot_data([("GOLD", "gold"), ("JPY", "yen")])

    assert "[GOLD]\nReport Date: 2026-09-16" in result["text"]
    assert "Large Speculators:" in result["text"]
    assert "[JPY]\nCOT取得不可（temporary source failure）" in result["text"]
    assert "JPY: temporary source failure" in result["error"]
    assert result["instrument_dates"]["JPY"] is None


@pytest.mark.parametrize(
    ("report_date", "accepted"),
    [("2026-09-15T00:00:00.000", True), ("2020-01-07T00:00:00.000", False)],
)
def test_cot_disaggregated_applies_same_date_freshness_gate(monkeypatch, report_date, accepted):
    _freeze_cot_disaggregated_now(monkeypatch)

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return [{"report_date_as_yyyy_mm_dd": report_date}]

    monkeypatch.setattr(cotd.requests, "get", lambda *args, **kwargs: FakeResponse())

    result = cotd.fetch_cot_disaggregated("GOLD")

    if accepted:
        assert result["data"]["date"] == "2026-09-15"
        assert result["as_of_date"] == "2026-09-15"
        assert result["error"] is None
    else:
        assert result["data"] is None
        assert result["as_of_date"] is None
        assert result["stale"] is True
        assert "現在判断から除外" in result["error"]
