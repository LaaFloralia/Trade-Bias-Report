"""統一スコアの機械検証テスト（2026-08-14 Daily の実例を回帰ケースにする）。"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scrapers.score_consistency import (  # noqa: E402
    enforce_score_consistency,
    extract_score_table_total,
    label_for_score,
)

TABLE = """| # | 項目 | 判定 | 点 |
|---|---|---|---|
| 1 | DXYバイアス整合 | DXY Neutral かつ無相関化 → 0 固定 | 0 |
| 2 | リテール / Open Orders 逆張り整合 | Order Book BSL が Draw 方向に集中 | +2 |
| 3 | XAU-TF構造整合 | D1 上昇 / H4 下抜けで混在 | 0 |
| 4 | ファンダ大局整合 | セクション7 Bullish と整合 | +1 |
| 5 | 週次アンカー整合 | Weekly Bullish と整合 | +1 |
| 6 | イベントリスク | KZ 重複ハイインパクトなし | 0 |
| 7 | 相関レジーム乖離 | 3ペアすべて無相関化 | -1 |
| 8 | ETF・機関フロー整合 | GLD 流入・中銀 net_buying と整合 | +1 |
"""

# 実際に 2026-08-14 の Daily が出力した形（セクション0 未訂正 + 末尾に読み替え注記）
BROKEN_MD = f"""# ICT Daily Bias Report — 2026-08-14

## セクション0: エグゼクティブサマリー

信頼度: Med ｜ スコア 5 ｜ NO-TRADE: なし

- XAUUSD: Long。

## セクション1: 今夜の執行プラン

### 1-3. 統一信頼度スコア内訳

{TABLE}
合計 4+... 再集計: 0+2+0+1+1+0-1+1 = **4** → 判定 **Med-cautious**

## セクション8: 前回照合 & 自己検証

*(注: セクション0 の1行目は自己検証の再計算に合わせ「信頼度: Med-cautious ｜ スコア 4 ｜ NO-TRADE: なし」と読み替えること。)*
"""


def test_label_thresholds():
    assert label_for_score(8) == "High"
    assert label_for_score(7) == "High"
    assert label_for_score(6) == "Med"
    assert label_for_score(5) == "Med"
    assert label_for_score(4) == "Med-cautious"
    assert label_for_score(3) == "Med-cautious"
    assert label_for_score(2) == "Low"
    assert label_for_score(-3) == "Low"


def test_extracts_table_total():
    assert extract_score_table_total(BROKEN_MD) == 4


def test_corrects_section0_and_drops_read_as_note():
    out, rep = enforce_score_consistency(BROKEN_MD)
    assert rep["status"] == "corrected"
    assert rep["table_total"] == 4
    assert rep["declared_score"] == 5
    assert rep["declared_label"] == "Med"
    assert rep["expected_label"] == "Med-cautious"
    # セクション0 が訂正されている
    assert "信頼度: Med-cautious ｜ スコア 4 ｜ NO-TRADE: なし" in out
    assert "信頼度: Med ｜ スコア 5" not in out
    # 読み替え注記は消える（訂正後は虚偽になるため）
    assert "読み替え" not in out
    # 生成残骸が正規形に置き換わる
    assert "合計 4+..." not in out
    assert "合計: **4** → 判定 **Med-cautious**" in out
    assert rep["residue_cleaned"] == 2


def test_consistent_report_is_untouched_except_residue():
    md = BROKEN_MD.replace("信頼度: Med ｜ スコア 5", "信頼度: Med-cautious ｜ スコア 4")
    out, rep = enforce_score_consistency(md)
    assert rep["status"] == "ok"
    assert "信頼度: Med-cautious ｜ スコア 4" in out
    assert "読み替え" not in out


def test_weekly_format_is_supported():
    md = BROKEN_MD.replace(
        "信頼度: Med ｜ スコア 5 ｜ NO-TRADE: なし",
        "信頼度: Med（プラン1 のスコア合計 5 点。内訳はセクション8）",
    )
    out, rep = enforce_score_consistency(md)
    assert rep["status"] == "corrected"
    assert "信頼度: Med-cautious（プラン1 のスコア合計 4 点。内訳はセクション8）" in out


def test_low_score_is_not_auto_corrected():
    """Low は本文構成（プラン非提示）まで変わるため機械訂正しない。"""
    md = BROKEN_MD.replace("| 2 | リテール / Open Orders 逆張り整合 | Order Book BSL が Draw 方向に集中 | +2 |",
                           "| 2 | リテール / Open Orders 逆張り整合 | 逆行 | -2 |")
    md = md.replace("| 4 | ファンダ大局整合 | セクション7 Bullish と整合 | +1 |",
                    "| 4 | ファンダ大局整合 | 逆行 | -1 |")
    out, rep = enforce_score_consistency(md)
    assert rep["status"] == "needs_regeneration"
    assert rep["expected_label"] == "Low"
    assert out == md, "自動訂正せず原文のまま返す"


def test_missing_table_is_skipped():
    md = "## セクション0: エグゼクティブサマリー\n\n信頼度: Med ｜ スコア 5 ｜ NO-TRADE: なし\n"
    out, rep = enforce_score_consistency(md)
    assert rep["status"] == "skipped"
    assert out == md


def test_incomplete_table_is_ignored():
    """7 項目しかない表を誤って採用しない。"""
    partial = "\n".join(TABLE.splitlines()[:-1])
    md = f"## セクション0: エグゼクティブサマリー\n\n信頼度: Med ｜ スコア 5 ｜ NO-TRADE: なし\n\n{partial}\n"
    _, rep = enforce_score_consistency(md)
    assert rep["table_total"] is None
    assert rep["status"] == "skipped"
