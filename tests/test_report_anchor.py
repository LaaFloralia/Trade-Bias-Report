"""前回レポート アンカー (report_anchor) の抽出・鮮度判定・出力を検証する。
オンデマンド運用で Daily を自己完結させるための機構 (README § 7.5)。"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scrapers.report_anchor import (  # noqa: E402
    _extract_section,
    format_anchor_lines,
    load_report_anchor,
)
from main import format_scraped_data  # noqa: E402

TODAY = date(2026, 7, 9)

WEEKLY_MD = """# ICT Weekly Bias Report — 2026-05-16

## セクション0: エグゼクティブサマリー

- DXY ウィークリーバイアス: Bullish（推定）
- 最優先注目銘柄: USDJPY Long

---

## セクション1: 詳細

長い本文。
"""

DAILY_WITH_15_MD = """# ICT Daily Bias Report — 2026-07-08

## セクション0: エグゼクティブサマリー

- DXYバイアス: Bearish

## セクション1.5: ファンダメンタル大局バイアス（数週間〜数ヶ月）

| 銘柄 | 大局バイアス | 主ドライバー | 確度 |
|---|---|---|---|
| XAUUSD | Bearish | 実質金利上昇 | 中 |

## セクション2: 銘柄別

本文。
"""

DAILY_WITHOUT_15_MD = """# ICT Daily Bias Report — 2026-07-05

## セクション0: エグゼクティブサマリー

- DXYバイアス: Bearish（短期・実データ）

## セクション1: DXY

本文。
"""


def _make_brain(tmp_path, monkeypatch, files: dict[str, str]) -> Path:
    brain = tmp_path / "Brain"
    for rel, content in files.items():
        p = brain / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    monkeypatch.setenv("BRAIN_PATH", str(brain))
    return brain


def test_extract_section_stops_at_next_heading():
    s = _extract_section(WEEKLY_MD, ["セクション0"])
    assert "DXY ウィークリーバイアス" in s
    assert "セクション1" not in s
    assert "---" not in s


def test_anchor_stale_and_fresh(tmp_path, monkeypatch):
    _make_brain(tmp_path, monkeypatch, {
        "Calendar/Weekly-Bias/Weekly_Bias_Report_2026-05-16.md": WEEKLY_MD,
        "Calendar/Daily-Bias/Daily_Bias_Report_2026-07-08.md": DAILY_WITH_15_MD,
    })
    a = load_report_anchor(today=TODAY)
    w, d = a["weekly"], a["prev_daily"]
    assert w["age_days"] == 54 and w["stale"] is True
    assert d["age_days"] == 1 and d["stale"] is False
    # 1.5 が優先抽出される
    assert d["section_used"] == "セクション1.5"
    assert "実質金利上昇" in d["summary"]
    # additive: 同じ前回 Daily のセクション0 が prev_daily_exec に入る
    pe = a["prev_daily_exec"]
    assert pe is not None
    assert pe["file"] == d["file"]
    assert pe["section_used"] == "セクション0"
    assert "DXYバイアス: Bearish" in pe["summary"]


def test_prev_daily_excludes_today_and_falls_back_to_section0(tmp_path, monkeypatch):
    _make_brain(tmp_path, monkeypatch, {
        # 当日のレポートは「前回」対象外 (再生成時に自分を参照しない)
        "Calendar/Daily-Bias/Daily_Bias_Report_2026-07-09.md": DAILY_WITH_15_MD,
        "Calendar/Daily-Bias/Daily_Bias_Report_2026-07-05.md": DAILY_WITHOUT_15_MD,
    })
    a = load_report_anchor(today=TODAY)
    d = a["prev_daily"]
    assert d["file"] == "Daily_Bias_Report_2026-07-05.md"
    assert d["stale"] is False  # 4 日前 <= 7 日 (オンデマンド運用で 3→7 に緩和)
    # 1.5 セクションが無い旧レポート → セクション0 にフォールバック
    assert d["section_used"] == "セクション0"
    assert "Bearish（短期・実データ）" in d["summary"]


def test_prev_daily_stale_after_seven_days(tmp_path, monkeypatch):
    _make_brain(tmp_path, monkeypatch, {
        # 8 日前 (> 7 日) → stale
        "Calendar/Daily-Bias/Daily_Bias_Report_2026-07-01.md": DAILY_WITH_15_MD,
    })
    a = load_report_anchor(today=TODAY)
    assert a["prev_daily"]["age_days"] == 8
    assert a["prev_daily"]["stale"] is True
    assert a["prev_daily_exec"]["stale"] is True


def test_format_anchor_lines_emits_prev_daily_exec(tmp_path, monkeypatch):
    _make_brain(tmp_path, monkeypatch, {
        "Calendar/Daily-Bias/Daily_Bias_Report_2026-07-08.md": DAILY_WITH_15_MD,
    })
    a = load_report_anchor(today=TODAY)
    text = "\n".join(format_anchor_lines(a))
    assert "[前回 Daily] Daily_Bias_Report_2026-07-08.md (1日前)" in text
    assert "[前回 Daily 結論] Daily_Bias_Report_2026-07-08.md (1日前)" in text
    assert "DXYバイアス: Bearish" in text


def test_weekly_deep_preferred_on_same_date(tmp_path, monkeypatch):
    _make_brain(tmp_path, monkeypatch, {
        "Calendar/Weekly-Bias/Weekly_Bias_Report_2026-07-07.md": WEEKLY_MD,
        "Calendar/Weekly-Deep-Bias/Weekly_Deep_Bias_Report_2026-07-07.md": WEEKLY_MD,
    })
    a = load_report_anchor(today=TODAY)
    assert a["weekly"]["kind"] == "Weekly-Deep"


XAU_TF_MD = """# XAU Technical Report (ICT/SMC × Retail) — 2026-07-09
> データ: Dukascopy H1 bid (UTC)、最終バー 2026-07-06 23:00。

## 1. マルチタイムフレーム構造 (Market Structure)

| TF | 構造 | 直近の構造ブレイク |
|---|---|---|
| D1 | 下降 (LH+LL) | 下抜け ↓ 4121 (2026-06-24) |

## 2. 流動性マップ (Liquidity Pools = ストップの在処)

- 4545.69 (+9.35%) — PMH (前月高値)
- 3941.53 (-5.18%) — PWL (前週安値)

## 3. プレミアム / ディスカウント

- 現在位置 **77%** → **プレミアム (売り側優位圏)**

## 4. 未充填インバランス (FVG)

- D1 下 (サポート帯): **4115–4123** (未充填)

## 5. ボラティリティ / セッション

- ATR20d: 80.6
"""


def test_xau_tf_anchor_extracts_level_sections(tmp_path, monkeypatch):
    _make_brain(tmp_path, monkeypatch, {
        "Calendar/XAU-TF/XAU_Technical_Report_2026-07-09.md": XAU_TF_MD,
    })
    a = load_report_anchor(today=TODAY)
    xt = a["xau_tf"]
    assert xt["age_days"] == 0 and xt["stale"] is False
    assert xt["section_used"] == "levels"
    # データ基準行 + 構造・流動性マップ・Premium/Discount・FVG が連結抽出される
    for expected in ("[データ基準] データ: Dukascopy H1 bid (UTC)、最終バー 2026-07-06 23:00",
                     "[構造]", "[流動性マップ]", "PMH (前月高値)", "[Premium/Discount]",
                     "[未充填FVG]", "4115–4123"):
        assert expected in xt["summary"]
    # 対象外セクション (ボラティリティ) は含めない
    assert "ATR20d" not in xt["summary"]


def test_xau_tf_anchor_stale_after_one_day(tmp_path, monkeypatch):
    _make_brain(tmp_path, monkeypatch, {
        "Calendar/XAU-TF/XAU_Technical_Report_2026-07-07.md": XAU_TF_MD,
    })
    a = load_report_anchor(today=TODAY)
    assert a["xau_tf"]["age_days"] == 2
    assert a["xau_tf"]["stale"] is True
    text = "\n".join(format_anchor_lines(a))
    assert "[XAU テクニカル] XAU_Technical_Report_2026-07-07.md (2日前) [STALE" in text
    assert "SSoT" in text


def test_xau_tf_anchor_absent_message(tmp_path, monkeypatch):
    _make_brain(tmp_path, monkeypatch, {
        "Calendar/Daily-Bias/Daily_Bias_Report_2026-07-08.md": DAILY_WITH_15_MD,
    })
    a = load_report_anchor(today=TODAY)
    assert a["xau_tf"] is None
    text = "\n".join(format_anchor_lines(a))
    assert "[XAU テクニカル] なし" in text


def test_missing_brain_is_graceful(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_PATH", str(tmp_path / "no-such-dir"))
    a = load_report_anchor(today=TODAY)
    assert a["weekly"] is None and a["prev_daily"] is None
    assert a["error"] is None
    assert "存在しない" in a["note"]
    lines = format_anchor_lines(a)
    assert any("アンカーなしで続行" in ln for ln in lines)


def test_format_scraped_data_emits_anchor_first(tmp_path, monkeypatch):
    _make_brain(tmp_path, monkeypatch, {
        "Calendar/Weekly-Bias/Weekly_Bias_Report_2026-05-16.md": WEEKLY_MD,
        "Calendar/Daily-Bias/Daily_Bias_Report_2026-07-08.md": DAILY_WITH_15_MD,
    })
    anchor = load_report_anchor(today=TODAY)
    text = format_scraped_data({"timestamp": "2026-07-09T09:00:00", "report_anchor": anchor})
    assert "### 前回レポート アンカー (Brain 自動読み込み)" in text
    assert "[Weekly 大局] Weekly_Bias_Report_2026-05-16.md (54日前) [STALE >9日: 参考扱い]" in text
    assert "[前回 Daily] Daily_Bias_Report_2026-07-08.md (1日前)" in text
    # アンカーは価格データより前 (冒頭) に出る
    assert text.index("前回レポート アンカー") < 200
