from __future__ import annotations

import sys
from datetime import datetime as RealDateTime
from datetime import timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scrapers import fred  # noqa: E402


def test_extended_fred_stale_thresholds(monkeypatch):
    fixed_now = RealDateTime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)

    class FixedDateTime(RealDateTime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_now.replace(tzinfo=None)
            return fixed_now.astimezone(tz)

    as_of_by_series = {}

    def _fake_fetch_series_observations(series_id, api_key):
        return {
            "value": 1.0,
            "as_of_date": as_of_by_series[series_id],
            "prev_value": 0.9,
            "prev_as_of_date": "2026-01-01",
        }

    monkeypatch.setattr(fred, "datetime", FixedDateTime)
    monkeypatch.setattr(fred, "_fetch_series_observations", _fake_fetch_series_observations)

    as_of_by_series["WALCL"] = (fixed_now - timedelta(days=20)).strftime("%Y-%m-%d")
    walcl = fred.fetch_fred_series("WALCL", api_key="fake_key")
    assert walcl["stale"] is True
    assert "series threshold 10 calendar days" in walcl["note"]

    as_of_by_series["IRLTLT01DEM156N"] = (fixed_now - timedelta(days=50)).strftime("%Y-%m-%d")
    oecd_fresh = fred.fetch_fred_series("IRLTLT01DEM156N", api_key="fake_key")
    assert oecd_fresh["stale"] is False
    assert "note" not in oecd_fresh

    as_of_by_series["IRLTLT01DEM156N"] = (fixed_now - timedelta(days=80)).strftime("%Y-%m-%d")
    oecd_stale = fred.fetch_fred_series("IRLTLT01DEM156N", api_key="fake_key")
    assert oecd_stale["stale"] is True
    assert "series threshold 75 calendar days" in oecd_stale["note"]

    assert fred.SERIES_IDS == ["DGS10", "DGS2", "DTWEXBGS"]
