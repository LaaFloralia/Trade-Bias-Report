"""配信用サマリー（Hermes → Telegram に送る唯一の本文）の回帰テスト。

要件: Telegram にはレポート全文を送らず概要だけを送る。本文は Brain の MD と
Google Drive の PDF に出す。ここではその契約（概要に本文が混ざらない / 出力先が
必ず載る / 失敗時も通知になる）を固定する。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import intel  # noqa: E402


@pytest.fixture(autouse=True)
def _fixed_brain_path(monkeypatch):
    """フィクスチャのパスと brain_path() を一致させる（実行環境の BRAIN_PATH に依存しない）。"""
    monkeypatch.setenv("BRAIN_PATH", "/Users/laa/Brain")

INTEL_JSON = {
    "bias": 0.45,
    "no_trade": False,
    "no_trade_reason": None,
    "risk_events_next_24h": [
        "15:00 JST UK GDP (QoQ) (Q2)",
        "21:30 JST 米 PPI (MoM) (Jul)",
        "21:30 JST 米 新規失業保険申請",
        "02:00 JST 米 30年債入札",
    ],
    "positioning_summary": "リテールは Long 50.0% / Short 50.0% で" + "詳細" * 200,
    "confidence": 0.5,
    "data_as_of": "2026-08-13",
    "review": {"verdict": "hit"},
}


def _record(**overrides) -> dict:
    rec = {
        "ok": True,
        "data_as_of": "2026-08-13",
        "duration_s": 632.0,
        "intel_json": INTEL_JSON,
        "outputs": {
            "md_path": "/Users/laa/Brain/Calendar/Daily-Bias/Daily_Bias_Report_2026-08-13.md",
            "pdf_path": "/Users/laa/dev/fundamental-macro-analysis/output/Daily_Bias_Report_2026-08-13.pdf",
            "drive_path": "/Users/laa/Library/CloudStorage/GoogleDrive-x@gmail.com/マイドライブ/Trading/Bias-Reports/Daily_Bias_Report_2026-08-13.pdf",
            "brain_synced": True,
        },
    }
    rec.update(overrides)
    return rec


def test_summary_has_headline_numbers_and_outputs():
    out = intel.build_notify_summary(_record(), "daily", "2026-08-13")
    assert "チャート外分析 Daily — 2026-08-13" in out
    assert "+0.45" in out and "ややブル" in out
    assert "信頼度: 50%" in out
    assert "ノートレード: 該当なし" in out
    assert "前回バイアスの振り返り: hit" in out
    # パスは vault / Drive のルートからの相対で見せる（CloudStorage の実パスは出さない）
    assert "・Brain MD: Calendar/Daily-Bias/Daily_Bias_Report_2026-08-13.md" in out
    assert "・Drive PDF: Trading/Bias-Reports/Daily_Bias_Report_2026-08-13.pdf" in out
    assert "CloudStorage" not in out


def test_summary_excludes_report_body():
    """レポート本文・機械用 JSON の生データを通知に載せない。"""
    rec = _record()
    rec["claude_calls"] = [{"purpose": "report_md", "response": "# 本文\n" + "長文" * 2000}]
    out = intel.build_notify_summary(rec, "daily", "2026-08-13")
    assert "長文" not in out
    assert "positioning_summary" not in out
    assert INTEL_JSON["positioning_summary"] not in out
    # 概要は Telegram 1 通に収まる長さ（本文が混ざれば必ず超える）
    assert len(out) < 900


def test_summary_caps_risk_events():
    rec = _record()
    rec["intel_json"] = {**INTEL_JSON, "risk_events_next_24h": [f"event{i}" for i in range(20)]}
    out = intel.build_notify_summary(rec, "daily", "2026-08-13")
    assert out.count("・event") == intel.NOTIFY_MAX_EVENTS


def test_summary_states_no_risk_events_explicitly():
    """イベント 0 件でも行を残す（省略すると「落ちた」のか判別できない）。"""
    rec = _record()
    rec["intel_json"] = {**INTEL_JSON, "risk_events_next_24h": []}
    out = intel.build_notify_summary(rec, "daily", "2026-08-14")
    assert "24h のリスクイベント: なし" in out


def test_summary_flags_missing_pdf():
    rec = _record()
    rec["outputs"] = {**rec["outputs"], "pdf_path": None, "drive_path": None}
    out = intel.build_notify_summary(rec, "daily", "2026-08-13")
    assert "PDF: 発行に失敗" in out


def test_summary_flags_pdf_without_drive():
    rec = _record()
    rec["outputs"] = {**rec["outputs"], "drive_path": None}
    out = intel.build_notify_summary(rec, "daily", "2026-08-13")
    assert "Drive コピーはスキップ/失敗" in out


def test_summary_flags_stale_data():
    rec = _record(data_as_of="2026-08-11")
    out = intel.build_notify_summary(rec, "daily", "2026-08-13")
    assert "STALE" in out


def test_summary_on_failure():
    rec = {"ok": False, "error": "RuntimeError: claude timeout", "outputs": {}}
    out = intel.build_notify_summary(rec, "weekly", "2026-08-15")
    assert out.startswith("⚠️ チャート外分析 Weekly — 2026-08-15 生成失敗")
    assert "claude timeout" in out


def test_weekly_title_and_symbol_variant():
    weekly = intel.build_notify_summary(_record(), "weekly", "2026-08-15")
    assert "チャート外分析 Weekly" in weekly
    sym = intel.build_notify_summary(_record(), "daily", "2026-08-13", symbol="USDJPY")
    assert "（USDJPY）" in sym


def test_write_notify_summary_emits_path_contract(tmp_path, monkeypatch, capsys):
    """シェル側が拾う `SUMMARY_FILE: <path>` の契約を固定する。"""
    monkeypatch.setattr(intel, "INTEL_DIR", tmp_path / "intel")
    path = intel.write_notify_summary(_record(), "daily", "2026-08-13")
    assert path is not None and path.exists()
    assert path.name == "summary_daily_2026-08-13.txt"
    assert "SUMMARY_FILE: " in capsys.readouterr().out
    assert "チャート外分析 Daily" in path.read_text(encoding="utf-8")


def test_bias_labels():
    assert intel._bias_label(0.8) == "強ブル"
    assert intel._bias_label(0.3) == "ややブル"
    assert intel._bias_label(0.0) == "中立"
    assert intel._bias_label(-0.3) == "ややベア"
    assert intel._bias_label(-0.9) == "強ベア"
