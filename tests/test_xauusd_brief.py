"""XAUUSD Brief 生成モジュールのユニットテスト。

API 呼び出しを伴わない純粋関数と、モッククライアントによる generate_xauusd_brief
の動作を検証する。

実行:
    .venv/bin/python3 -m pytest tests/test_xauusd_brief.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.archive.generate_xauusd_brief import (  # noqa: E402
    _count_body_chars,
    _extract_acquired_time,
    _resolve_report_date,
    _strip_h1,
    _toggle_enabled,
    generate_xauusd_brief,
)


SAMPLE_REPORT = """# ICT Daily Bias Report 2026-05-20

データ取得日時: 2026-05-20T08:33:12

## セクション0: エグゼクティブサマリー
- DXYバイアス: Bearish
- XAUUSD Long 注目

## セクション7
信頼度スコア合計: 6点（標準確度）
"""


# ------------------------------------------------------------
# トグル
# ------------------------------------------------------------
def test_toggle_default_enabled(monkeypatch):
    monkeypatch.delenv("GENERATE_XAUUSD_BRIEF", raising=False)
    assert _toggle_enabled() is True


@pytest.mark.parametrize("val", ["false", "0", "no", "off", "FALSE", ""])
def test_toggle_env_disables(monkeypatch, val):
    monkeypatch.setenv("GENERATE_XAUUSD_BRIEF", val)
    assert _toggle_enabled() is False


@pytest.mark.parametrize("val", ["true", "1", "yes", "on"])
def test_toggle_env_enables(monkeypatch, val):
    monkeypatch.setenv("GENERATE_XAUUSD_BRIEF", val)
    assert _toggle_enabled() is True


# ------------------------------------------------------------
# 字数カウント（テーブル除外）
# ------------------------------------------------------------
def test_count_body_chars_excludes_tables():
    md = (
        "本文あいうえお\n"
        "| 時刻 | 指標 |\n"
        "|---|---|\n"
        "| 21:30 | CPI |\n"
        "かきくけこ\n"
    )
    # 地の文は「本文あいうえお」(7) +「かきくけこ」(5) = 12 字。テーブル行は除外。
    assert _count_body_chars(md) == 12


def test_count_body_chars_ignores_whitespace():
    assert _count_body_chars("あ い う\n\n  え お  ") == 5


# ------------------------------------------------------------
# 取得時刻・日付の抽出
# ------------------------------------------------------------
def test_extract_acquired_time_from_timestamp():
    assert _extract_acquired_time(SAMPLE_REPORT) == "08:33"


def test_extract_acquired_time_from_jst_phrase():
    assert _extract_acquired_time("本日 21:05 JST 時点の状況") == "21:05"


def test_extract_acquired_time_fallback_is_hhmm():
    out = _extract_acquired_time("時刻情報なしのテキスト")
    assert len(out) == 5 and out[2] == ":"


def test_resolve_report_date_prefers_explicit():
    assert _resolve_report_date(SAMPLE_REPORT, "2026-01-01") == "2026-01-01"


def test_resolve_report_date_from_body():
    assert _resolve_report_date(SAMPLE_REPORT, None) == "2026-05-20"


# ------------------------------------------------------------
# H1 除去
# ------------------------------------------------------------
def test_strip_h1_removes_leading_title():
    assert _strip_h1("# タイトル\n\n## 1. 本文") == "## 1. 本文"


def test_strip_h1_keeps_body_without_h1():
    assert _strip_h1("## 1. 本文\n内容") == "## 1. 本文\n内容"


# ------------------------------------------------------------
# generate_xauusd_brief: スキップ系（API 呼び出しなし）
# ------------------------------------------------------------
def test_skip_when_toggle_disabled(monkeypatch):
    monkeypatch.setenv("GENERATE_XAUUSD_BRIEF", "false")
    assert generate_xauusd_brief(SAMPLE_REPORT, output_dir="/tmp") is None


def test_skip_when_report_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("GENERATE_XAUUSD_BRIEF", "true")
    missing = tmp_path / "Daily_Bias_Report_2026-05-20.md"
    assert generate_xauusd_brief(missing) is None


def test_skip_when_report_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("GENERATE_XAUUSD_BRIEF", "true")
    assert generate_xauusd_brief("   ", output_dir=str(tmp_path)) is None


def test_skip_when_text_input_without_output_dir(monkeypatch):
    monkeypatch.setenv("GENERATE_XAUUSD_BRIEF", "true")
    # 本文渡し（複数行）で output_dir 未指定 → 出力先不明でスキップ
    assert generate_xauusd_brief(SAMPLE_REPORT) is None


# ------------------------------------------------------------
# generate_xauusd_brief: モッククライアントによる正常系
# ------------------------------------------------------------
class _FakeBlock:
    type = "text"

    def __init__(self, text: str):
        self.text = text


class _FakeResponse:
    def __init__(self, text: str):
        self.content = [_FakeBlock(text)]


class _FakeMessages:
    def __init__(self, text: str):
        self._text = text
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self._text)


class _FakeClient:
    def __init__(self, text: str):
        self.messages = _FakeMessages(text)


def test_generate_writes_file_with_mock_client(monkeypatch, tmp_path):
    monkeypatch.setenv("GENERATE_XAUUSD_BRIEF", "true")
    report = tmp_path / "Daily_Bias_Report_2026-05-20.md"
    report.write_text(SAMPLE_REPORT, encoding="utf-8")

    body = "## 1. 本日のゲート判定\n- **トレード可否**: 可\n"
    client = _FakeClient(body)

    out = generate_xauusd_brief(report, client=client)

    assert out is not None
    assert out.name == "Daily_XAUUSD_Brief_2026-05-20.md"
    assert out.parent == tmp_path
    content = out.read_text(encoding="utf-8")
    # H1 は Python 側で付与され、日付と取得時刻を含む
    assert content.startswith("# XAUUSD Brief 2026-05-20 (08:33 JST取得)")
    assert "## 1. 本日のゲート判定" in content
    # LLM 呼び出しは 1 回のみ（字数が上限内のためリトライ無し）
    assert len(client.messages.calls) == 1


def test_generate_retries_once_when_over_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("GENERATE_XAUUSD_BRIEF", "true")
    report = tmp_path / "Daily_Bias_Report_2026-05-20.md"
    report.write_text(SAMPLE_REPORT, encoding="utf-8")

    # 1500 字超の本文を返すモック → 短縮リトライが 1 回走る
    over_limit = "## 1. 判定\n" + ("あ" * 1600)
    client = _FakeClient(over_limit)

    out = generate_xauusd_brief(report, client=client)

    assert out is not None
    # 初回 + 短縮リトライ = 計 2 回
    assert len(client.messages.calls) == 2
