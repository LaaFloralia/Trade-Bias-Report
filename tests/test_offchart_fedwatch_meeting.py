"""Preserve the meeting belonging to the collected FedWatch probability table."""

from copy import deepcopy
from datetime import datetime
import json

import pytest

from scrapers.fedwatch import _parse_investing_body
from scrapers.liquidity_levels import build_liquidity
from scrapers.macro_surprise import JST
from scrapers.offchart_features import build_features, save_features


NOW = datetime(2026, 9, 29, 18, 0, tzinfo=JST)


def fedwatch(meeting="Oct 28, 2026", first=80):
    # Real parser, synthetic public-shaped rows; no network or history access.
    return _parse_investing_body(
        f"Meeting Time: {meeting}\n"
        f"3.25 - 3.50\t{first}%\t70%\t60%\n"
        f"3.50 - 3.75\t{100 - first}%\t30%\t40%",
        today=NOW.date(),
    )


def inputs(fed):
    return {
        "timestamp": NOW.isoformat(),
        "fedwatch": fed,
        "economic_calendar": {"events": [{
            "date": "Tuesday, September 29, 2026",
            "time_jst": "23:00", "country": "United States",
            "indicator": "JOLTS Job Openings (Aug)", "forecast": "7.1M",
        }]},
        "liquidity_levels": build_liquidity(4300.0, {"value": 23.0, "as_of_date": "2026-09-28"}),
        "fred": {"DFII10": {"value": 2.85, "change": 0.09, "as_of_date": "2026-09-28"}},
        "input_status": [{"item": 3, "available": True}],
    }


@pytest.mark.parametrize("weekly", [False, True])
def test_parser_meeting_reaches_features_and_saved_json(tmp_path, weekly):
    raw = fedwatch()
    assert raw["error"] is None
    original = inputs(raw)
    before = deepcopy(original)
    features = build_features(original, NOW, weekly=weekly)
    assert features["fedwatch"] == {
        "next_meeting": "Oct 28, 2026", "target_rates": raw["target_rates"],
    }
    assert features["schema_version"] == 1
    assert features["mode"] == ("weekly" if weekly else "daily")
    assert original == before
    save_features(features, tmp_path)
    saved = json.loads((tmp_path / "offchart_features_latest.json").read_text())
    assert saved["fedwatch"] == features["fedwatch"]


@pytest.mark.parametrize("raw", [
    None,
    {},
    {"target_rates": []},
    {"meeting_date": "Dec 9, 2026", "target_rates": []},
    {"next_fomc_date": None, "meeting_date": "Dec 9, 2026", "target_rates": []},
])
def test_missing_canonical_meeting_stays_unknown_even_with_legacy_alias(raw):
    features = build_features(inputs(raw), NOW)
    assert features["fedwatch"]["next_meeting"] is None
    assert features["fedwatch"]["target_rates"] == (raw or {}).get("target_rates")


def test_conflicting_alias_cannot_replace_the_probability_table_meeting():
    raw = fedwatch()
    raw["meeting_date"] = "Dec 9, 2026"
    features = build_features(inputs(raw), NOW)
    assert features["fedwatch"]["next_meeting"] == "Oct 28, 2026"
    assert features["fedwatch"]["target_rates"] == raw["target_rates"]


def test_meeting_rollover_keeps_each_probability_table_and_other_features(tmp_path):
    old = build_features(inputs(fedwatch("Oct 28, 2026", first=80)), NOW)
    new = build_features(inputs(fedwatch("Dec 9, 2026", first=55)), NOW)
    assert old["fedwatch"]["next_meeting"] == "Oct 28, 2026"
    assert new["fedwatch"]["next_meeting"] == "Dec 9, 2026"
    assert old["fedwatch"]["target_rates"][0]["current"] == 80
    assert new["fedwatch"]["target_rates"][0]["current"] == 55
    assert old["event_blackouts"] and old["expected_move"]["expected_move_1sd"]
    assert {k: v for k, v in old.items() if k != "fedwatch"} == {
        k: v for k, v in new.items() if k != "fedwatch"
    }
    save_features(old, tmp_path)
    save_features(new, tmp_path)
    history = [json.loads(line) for line in (tmp_path / "history" / "offchart_features.jsonl").read_text().splitlines()]
    assert [row["fedwatch"] for row in history] == [old["fedwatch"], new["fedwatch"]]
