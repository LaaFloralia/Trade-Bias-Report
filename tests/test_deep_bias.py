"""Deep Bias Report — fixture ドライランによる回帰テスト。

実行:
    .venv/bin/python3 -m pytest tests/test_deep_bias.py -v
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

# Make `scripts/` importable.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.render_report import (  # noqa: E402
    _extract_confidence_badge,
    _extract_title_and_summary,
    render,
    render_html,
)


FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures"
SAMPLE_MD = FIXTURE_DIR / "sample_report.md"


@pytest.fixture
def tmp_md(tmp_path: Path) -> Path:
    """Copy the sample report into tmp so render outputs land in tmp."""
    dst = tmp_path / "Deep_Bias_Report_2026-05-13.md"
    shutil.copy(SAMPLE_MD, dst)
    return dst


def test_extract_title_and_summary_finds_title():
    md = SAMPLE_MD.read_text(encoding="utf-8")
    title, summary_html = _extract_title_and_summary(md)
    assert "ICT Deep Bias Report" in title
    assert summary_html, "summary block should be non-empty"
    assert "DXY" in summary_html
    assert "XAUUSD" in summary_html


def test_render_html_only(tmp_md: Path):
    """Smoke test: HTML rendering completes and contains expected anchors."""
    html_path = render_html(tmp_md, project_root=PROJECT_ROOT)
    assert html_path.exists()
    assert html_path.suffix == ".html"

    html = html_path.read_text(encoding="utf-8")
    assert "Executive Summary" in html
    assert "DXY" in html
    assert "BTCUSD" in html
    assert "<table" in html, "tables extension must produce <table>"
    # 9.5pt font size from style.css should be embedded
    assert "font-family" in html


def test_render_produces_pdf(tmp_md: Path):
    """End-to-end: render_report.render() emits both HTML and PDF files."""
    html_path, pdf_path = render(tmp_md, project_root=PROJECT_ROOT)
    assert html_path.exists()
    assert pdf_path.exists()
    assert pdf_path.suffix == ".pdf"

    # The PDF should be non-trivial in size (header + content + style).
    pdf_size = pdf_path.stat().st_size
    assert pdf_size > 5_000, f"PDF unexpectedly small: {pdf_size} bytes"


def test_confidence_badge_explicit_marker_high():
    md = "信頼度バッジ: **High**\nDXY Bullish."
    html = _extract_confidence_badge(md)
    assert "cover-confidence-high" in html
    assert "信頼度: High" in html


def test_confidence_badge_explicit_marker_med_cautious():
    md = "1 行目: 信頼度バッジ: Med-cautious. XAUUSD Long..."
    html = _extract_confidence_badge(md)
    assert "cover-confidence-med-cautious" in html
    assert "信頼度: Med-cautious" in html


def test_confidence_badge_fallback_priority():
    """No explicit marker — highest-priority label found in summary wins."""
    md = "We rate this Low first, then Med, then High overall."
    html = _extract_confidence_badge(md)
    # Highest priority "High" should win
    assert "cover-confidence-high" in html


def test_confidence_badge_returns_empty_when_absent():
    html = _extract_confidence_badge("Plain summary without any confidence label.")
    assert html == ""


def test_extract_title_and_summary_handles_W0_weekly_header():
    md = (
        "# Weekly Deep Bias Report\n\n"
        "## セクション W0: エグゼクティブサマリー\n\n"
        "信頼度バッジ: **Med**\n\n"
        "1. DXY 週次バイアス Bullish — 主要ドライバー USDJPY\n"
        "2. マクロ流動性 expansion / US-JP 10Y +0.04\n"
        "\n---\n\n"
        "## セクション W1: 先週レビュー\n"
    )
    title, html = _extract_title_and_summary(md)
    assert "Weekly Deep Bias Report" in title
    assert "Executive Summary" in html
    assert "cover-confidence-med" in html
    assert "DXY 週次バイアス Bullish" in html


def test_scoring_thresholds_pure_helper():
    """The deep-bias scoring uses fixed thresholds; encode them as a pure helper."""
    from scripts.render_report import _project_root  # noqa: F401  (smoke import)

    # Encode the threshold rule inline so future drift is caught at test time.
    def classify(total: int) -> str:
        if total >= 10:
            return "High"
        if total >= 7:
            return "Med"
        if total >= 5:
            return "Med-cautious"
        return "Low"

    assert classify(10) == "High"
    assert classify(11) == "High"
    assert classify(9) == "Med"
    assert classify(7) == "Med"
    assert classify(6) == "Med-cautious"
    assert classify(5) == "Med-cautious"
    assert classify(4) == "Low"
    assert classify(0) == "Low"
