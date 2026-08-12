"""bias_review（振り返りナレッジベース）と weekly_review（共通照合入力）のテスト。

実行:
    .venv/bin/python3 -m pytest tests/test_bias_review.py -v
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scrapers import bias_review  # noqa: E402


def _entry(entry_date="2026-08-12", mode="Daily", verdict="hit", learn="実質金利の重みが不足"):
    return f"""## {entry_date} {mode}
- 判定: 当たり
- 前回想定: Bullish / High (7点) / 注目ゾーン 4,390-4,410
- 実際: +0.6%。BSL 4,438 sweep→reversal（リテール分析検出）
- 外し要因: -
- 学び: {learn}
<!-- review-json: {{"date": "{entry_date}", "mode": "{mode.lower()}", "verdict": "{verdict}"}} -->
"""


def test_validate_entry_accepts_standard_format():
    assert bias_review.validate_entry(_entry(), "2026-08-12", "daily") == []


def test_validate_entry_rejects_broken_format():
    errors = bias_review.validate_entry("これはエントリではない", "2026-08-12", "daily")
    assert errors
    errors = bias_review.validate_entry(
        _entry().replace("- 学び:", "- メモ:"), "2026-08-12", "daily"
    )
    assert any("学び" in e for e in errors)


def test_extract_verdict():
    assert bias_review.extract_verdict(_entry(verdict="miss")) == "miss"
    assert bias_review.extract_verdict("no json here") is None


def test_append_creates_file_with_header(tmp_path):
    log = tmp_path / "Bias-Review-Log.md"
    bias_review.append_entry(_entry(), "2026-08-12", "daily", path=log)
    text = log.read_text(encoding="utf-8")
    assert text.startswith("# Bias Review Log")
    assert "## 2026-08-12 Daily" in text


def test_append_same_day_same_mode_replaces(tmp_path):
    log = tmp_path / "Bias-Review-Log.md"
    bias_review.append_entry(_entry(learn="旧エントリ"), "2026-08-12", "daily", path=log)
    bias_review.append_entry(_entry(learn="新エントリ"), "2026-08-12", "daily", path=log)
    text = log.read_text(encoding="utf-8")
    assert text.count("## 2026-08-12 Daily") == 1
    assert "新エントリ" in text and "旧エントリ" not in text


def test_append_different_mode_coexists(tmp_path):
    log = tmp_path / "Bias-Review-Log.md"
    bias_review.append_entry(_entry(mode="Daily"), "2026-08-12", "daily", path=log)
    bias_review.append_entry(_entry(mode="Weekly"), "2026-08-12", "weekly", path=log)
    text = log.read_text(encoding="utf-8")
    assert "## 2026-08-12 Daily" in text and "## 2026-08-12 Weekly" in text


def test_load_recent_entries_returns_last_n(tmp_path):
    log = tmp_path / "Bias-Review-Log.md"
    for i in range(1, 8):
        d = f"2026-08-{i:02d}"
        bias_review.append_entry(_entry(entry_date=d, learn=f"学び{i}"), d, "daily", path=log)
    recent = bias_review.load_recent_entries(5, path=log)
    assert "学び7" in recent and "学び3" in recent
    assert "学び2" not in recent  # 直近 5 件のみ


def test_load_recent_entries_missing_file(tmp_path):
    assert bias_review.load_recent_entries(5, path=tmp_path / "nothing.md") is None


# ---------------------------------------------------------------- weekly_review

WEEKLY_MD = """# ICT Weekly Bias Report — 2026-08-11
## セクション0: エグゼクティブサマリー
信頼度: Med ｜ XAUUSD Bullish
## セクション7: 銘柄別週次バイアス & Weekly PO3
XAUUSD Bullish / Draw BSL 4,450
## セクション8: 来週の注目シナリオ Top 2
プラン1: XAUUSD Long
"""

DAILY_MD = """# ICT Daily Bias Report — 2026-08-12
## セクション0: エグゼクティブサマリー
信頼度: High ｜ スコア 7 ｜ NO-TRADE: なし
## セクション8: 前回照合 & 自己検証
当たり
"""


def test_build_weekly_review_block(tmp_path, monkeypatch):
    from scrapers import weekly_review

    brain = tmp_path / "Brain"
    (brain / "Calendar" / "Weekly-Bias").mkdir(parents=True)
    (brain / "Calendar" / "Daily-Bias").mkdir(parents=True)
    (brain / "Calendar" / "Weekly-Bias" / "Weekly_Bias_Report_2026-08-11.md").write_text(
        WEEKLY_MD, encoding="utf-8")
    (brain / "Calendar" / "Daily-Bias" / "Daily_Bias_Report_2026-08-12.md").write_text(
        DAILY_MD, encoding="utf-8")
    # 銘柄別サブディレクトリは対象外であることの検証用
    (brain / "Calendar" / "Daily-Bias" / "USDJPY").mkdir()
    (brain / "Calendar" / "Daily-Bias" / "USDJPY" / "Daily_Bias_Report_USDJPY_2026-08-13.md").write_text(
        DAILY_MD.replace("Daily Bias Report", "Daily Bias Report (USDJPY)"), encoding="utf-8")

    intel_dir = tmp_path / "intel"
    intel_dir.mkdir()
    (intel_dir / "intel_daily_2026-08-12.json").write_text(
        '{"bias": 0.5, "no_trade": false, "confidence": 0.7}', encoding="utf-8")

    block = weekly_review.build_weekly_review_block(
        brain=brain, intel_dir=intel_dir, today=date(2026, 8, 15),
    )
    assert block is not None
    assert block.startswith("### 前回レビュー入力")
    assert "[前回 Weekly] Weekly_Bias_Report_2026-08-11.md" in block
    assert "XAUUSD Bullish" in block
    assert "[直近 Daily] Daily_Bias_Report_2026-08-12.md" in block
    assert "intel_daily_2026-08-12.json" in block
    assert "USDJPY" not in block  # 銘柄別レポートは混入しない


def test_build_weekly_review_block_empty_brain(tmp_path):
    from scrapers import weekly_review

    block = weekly_review.build_weekly_review_block(
        brain=tmp_path / "no-brain", intel_dir=tmp_path / "no-intel",
        today=date(2026, 8, 15),
    )
    assert block is None
