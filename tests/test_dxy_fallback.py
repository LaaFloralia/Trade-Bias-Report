"""Smoke test: DXY 失敗時に format_scraped_data() が
Broad USD Index (FRED DTWEXBGS) proxy フォールバック表示を出すか検証する。

実行方法:
    .venv/bin/python3 -m unittest tests.test_dxy_fallback
    または
    .venv/bin/python3 tests/test_dxy_fallback.py

テスト用フィクスチャはテスト内に閉じており、本番コード（main.py / scrapers/）
には一切の改変を残さない（user 指示 2026-05-10）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from main import format_scraped_data  # noqa: E402


def _fixture_dxy_failed() -> dict:
    """DXY が完全失敗・FRED 全成功という状態の最小フィクスチャ。"""
    return {
        "timestamp": "2026-05-10T00:00:00",
        "price_data": "",
        "retail_sentiment": {},
        "coinglass": {},
        "cot": None,
        "dxy": {
            "source": "（テスト用、forced failure）",
            "current_price": None,
            "error": "forced test failure for fallback verification",
        },
        "fred": {
            "DGS10": {
                "source": "FRED", "series_id": "DGS10",
                "value": 4.41, "prev_value": 4.36, "change": 0.05,
                "as_of_date": "2026-05-07", "prev_as_of_date": "2026-05-06",
                "timestamp": "2026-05-10T00:00:00Z",
                "stale": False, "fallback_used": False,
            },
            "DGS2": {
                "source": "FRED", "series_id": "DGS2",
                "value": 3.92, "prev_value": 3.87, "change": 0.05,
                "as_of_date": "2026-05-07", "prev_as_of_date": "2026-05-06",
                "timestamp": "2026-05-10T00:00:00Z",
                "stale": False, "fallback_used": False,
            },
            "DTWEXBGS": {
                "source": "FRED", "series_id": "DTWEXBGS",
                "value": 118.3926, "prev_value": 118.671, "change": -0.278,
                "as_of_date": "2026-05-01", "prev_as_of_date": "2026-04-30",
                "timestamp": "2026-05-10T00:00:00Z",
                "stale": False, "fallback_used": False,
            },
        },
        "economic_calendar": None,
        "fedwatch": None,
        "btc_etf": None,
    }


class DxyFallbackTests(unittest.TestCase):
    def test_dxy_failure_emits_dtwexbgs_proxy_with_required_substrings(self):
        output = format_scraped_data(_fixture_dxy_failed())

        # 必須 substring（user 指示通り）
        for needle in (
            "[DXY] 取得不可",
            "DXY unavailable",
            "using Broad USD Index proxy",
            "FRED DTWEXBGS",
            "fallback_used=true",
        ):
            self.assertIn(needle, output, msg=f"missing substring: {needle!r}")

    def test_dtwexbgs_value_appears_in_fallback_line(self):
        output = format_scraped_data(_fixture_dxy_failed())
        # 数値そのもの (118.393) が proxy 行に含まれる
        self.assertIn("118.393", output)

    def test_fred_sections_still_render_when_dxy_failed(self):
        output = format_scraped_data(_fixture_dxy_failed())
        # DXY 失敗でも US10Y / US2Y / Broad USD Index セクションは独立に出る
        self.assertIn("[US10Y]", output)
        self.assertIn("[US2Y]", output)
        self.assertIn("[Broad USD Index]", output)
        self.assertIn("NOT DXY", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
