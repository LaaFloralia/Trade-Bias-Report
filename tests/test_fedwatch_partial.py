from datetime import date

import main
import pytest
from scrapers.fedwatch import _parse_investing_body
from scrapers.fedwatch_history import record_snapshot
from scripts.human_report import ReportData, Section, build_macro_panel, extract_fedwatch


ACTUAL_PROVIDER_SHAPE = (
    "Meeting Time: Oct 28, 2026\n"
    "3.75 - 4.00\n"
    "4.00 - 4.25\n"
    "3.50 - 3.75\t—\t—\t5.2%\n"
    "3.75 - 4.00\t47.2%\t40.3%\t49.6%\n"
    "4.00 - 4.25\t52.8%\t59.7%\t45.2%\n"
    "S&P 500 VIX\t14.70\t-0.17\t-1.14%"
)


def test_actual_investing_shape_keeps_partial_rows_and_excludes_vix():
    result = _parse_investing_body(ACTUAL_PROVIDER_SHAPE, today=date(2026, 9, 22))

    assert result["error"] is None
    assert result["completeness"] == "partial"
    assert result["known_current_total"] == 100.0
    assert result["target_rates"] == [
        {"range": "3.75-4.00", "current": 47.2, "prev_day": 40.3, "prev_week": 49.6},
        {"range": "4.00-4.25", "current": 52.8, "prev_day": 59.7, "prev_week": 45.2},
    ]
    assert result["unavailable_target_rates"] == [
        {
            "range": "3.50-3.75",
            "current": None,
            "prev_day": None,
            "prev_week": 5.2,
            "availability": "unavailable",
        }
    ]
    assert len(result["raw_target_rate_lines"]) == 3
    assert all("VIX" not in line for line in result["raw_target_rate_lines"])
    assert all(result[field] is None for field in ("hold_pct", "cut_25bp_pct", "cut_50bp_pct", "hike_25bp_pct"))


def test_known_partial_probabilities_do_not_need_to_sum_to_100():
    body = (
        "Meeting Time: Oct 28, 2026\n"
        "3.50 - 3.75\t—\t—\t5.2%\n"
        "3.75 - 4.00\t40%\t—\t—\n"
        "4.00 - 4.25\t40%\t—\t—"
    )
    result = _parse_investing_body(body, today=date(2026, 9, 22))
    assert result["error"] is None
    assert result["completeness"] == "partial"
    assert result["known_current_total"] == 80.0
    assert len(result["target_rates"]) == 2


def test_empty_probability_cells_are_a_partial_row_not_a_dropped_row():
    body = (
        "Meeting Time: Oct 28, 2026\n"
        "3.50 - 3.75\t\t\t\n"
        "3.75 - 4.00\t47.2%\t40.3%\t49.6%\n"
        "4.00 - 4.25\t52.8%\t59.7%\t45.2%"
    )
    result = _parse_investing_body(body, today=date(2026, 9, 22))
    assert result["error"] is None
    assert result["completeness"] == "partial"
    assert result["unavailable_target_rates"][0]["range"] == "3.50-3.75"
    assert result["unavailable_target_rates"][0]["current"] is None


def test_all_current_probabilities_missing_remain_visible_in_formatted_input():
    body = (
        "Meeting Time: Oct 28, 2026\n"
        "3.50 - 3.75\t—\t—\t—\n"
        "3.75 - 4.00\t\t\t\n"
        "4.00 - 4.25\tN/A\tN/A\tN/A"
    )
    result = _parse_investing_body(body, today=date(2026, 9, 22))
    assert result["error"] is None
    assert result["completeness"] == "partial"
    assert result["target_rates"] == []
    assert len(result["unavailable_target_rates"]) == 3

    text = main.format_scraped_data(
        {"timestamp": "2026-09-22T12:00:00Z", "fedwatch": result}
    )
    assert "現在確率分布: 一部欠測" in text
    for rate in ("3.50-3.75", "3.75-4.00", "4.00-4.25"):
        assert f"{rate}: 現在 取得不可" in text
    assert "分類: 未確認" in text


def test_missing_prior_columns_remain_none_without_making_current_distribution_partial():
    body = (
        "Meeting Time: Oct 28, 2026\n"
        "3.50 - 3.75\t50%\t—\t\n"
        "3.75 - 4.00\t50%\t\tN/A"
    )
    result = _parse_investing_body(body, today=date(2026, 9, 22))
    assert result["error"] is None
    assert result["completeness"] == "complete"
    assert result["target_rates"] == [
        {"range": "3.50-3.75", "current": 50.0, "prev_day": None, "prev_week": None},
        {"range": "3.75-4.00", "current": 50.0, "prev_day": None, "prev_week": None},
    ]


@pytest.mark.parametrize("bad_value", ["101%", "NaN%", "Infinity%"])
def test_bad_probability_cells_fail_closed(bad_value):
    body = (
        "Meeting Time: Oct 28, 2026\n"
        f"3.50 - 3.75\t{bad_value}\t50%\t50%\n"
        "3.75 - 4.00\t50%\t50%\t50%"
    )
    result = _parse_investing_body(body, today=date(2026, 9, 22))
    assert result["error"]
    assert result["completeness"] == "invalid"
    assert result["target_rates"] == []
    assert result["unavailable_target_rates"] == []


def test_malformed_rate_row_fails_instead_of_silently_disappearing():
    result = _parse_investing_body(
        "Meeting Time: Oct 28, 2026\n3.50 - 3.75\t50%\t50%",
        today=date(2026, 9, 22),
    )
    assert result["completeness"] == "invalid"
    assert "4列形式が不正" in result["error"]
    assert result["raw_target_rate_lines"] == ["3.50 - 3.75\t50%\t50%"]


def test_extra_probability_column_fails_instead_of_becoming_a_blank_cell():
    result = _parse_investing_body(
        "Meeting Time: Oct 28, 2026\n"
        "3.50 - 3.75\t50%\t50%\t50%\t\n"
        "3.75 - 4.00\t50%\t50%\t50%",
        today=date(2026, 9, 22),
    )
    assert result["completeness"] == "invalid"
    assert "4列形式が不正" in result["error"]


@pytest.mark.parametrize(
    ("meeting", "valid"),
    [(None, False), ("Feb 30, 2027", False), ("Jan 1, 2020", False), ("Oct 28, 2026", True)],
)
def test_meeting_date_validation_fails_closed_except_for_upcoming_meeting(meeting, valid):
    body = "3.50 - 3.75\t50%\t50%\t50%\n3.75 - 4.00\t50%\t50%\t50%"
    if meeting is not None:
        body = f"Meeting Time: {meeting}\n" + body
    result = _parse_investing_body(body, today=date(2026, 9, 22))
    assert (result["error"] is None) is valid
    if not valid:
        assert result["target_rates"] == []


def test_formatted_analysis_shows_unavailable_range_and_does_not_leak_diagnostics():
    result = _parse_investing_body(ACTUAL_PROVIDER_SHAPE, today=date(2026, 9, 22))
    text = main.format_scraped_data(
        {"timestamp": "2026-09-22T12:00:00Z", "fedwatch": result}
    )
    assert "現在確率分布: 一部欠測" in text
    assert "3.50-3.75: 現在 取得不可" in text
    assert "提供表前週 5.2%" in text
    assert "分類: 未確認" in text
    assert "raw_target_rate_lines" not in text
    assert "S&P 500 VIX" not in text


def test_partial_distribution_does_not_render_as_complete_probability_bar():
    data = ReportData()
    extract_fedwatch(
        [
            Section(
                3,
                "FedWatch（常時取得）",
                "現在確率分布: 一部欠測\n"
                "* 3.50-3.75: 現在 取得不可\n"
                "* 3.75-4.00: 現在 47.2%\n"
                "* 4.00-4.25: 現在 52.8%",
            )
        ],
        data,
    )
    panel = build_macro_panel(data)
    assert data.fedwatch["partial"] is True
    assert data.fedwatch["probs"] == [("3.75-4.00", 47.2), ("4.00-4.25", 52.8)]
    assert "確率分布は一部欠測" in panel
    assert "<svg" not in panel


def test_record_snapshot_skips_partial_distribution_even_when_known_sum_is_100(tmp_path):
    result = _parse_investing_body(ACTUAL_PROVIDER_SHAPE, today=date(2026, 9, 22))
    path = tmp_path / "fedwatch.json"
    assert record_snapshot(result, date(2026, 9, 22), path) is False
    assert not path.exists()
