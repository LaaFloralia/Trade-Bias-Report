"""publish (PDF → Drive) がタイムアウトした時の挙動を固定する。

2026-08-13 の cron 実行では PDF 生成まで成功していたのに publish 全体が
300 秒で切られ、Drive に PDF が届かなかった。タイムアウト時は
(1) 部分出力をログに残す (2) 生成済み PDF から Drive コピーだけを回復する。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import intel  # noqa: E402


class _Proc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_parses_pdf_and_drive_contract():
    out = "[publish] PDF 生成 1.0s\nPDF: /o/r.pdf\nDrive: /d/r.pdf\n"
    assert intel._parse_publish_stdout(out) == {"pdf_path": "/o/r.pdf", "drive_path": "/d/r.pdf"}


def test_timeout_recovers_drive_copy(tmp_path, monkeypatch, capsys):
    md = tmp_path / "Daily_Bias_Report_2026-08-13.md"
    md.write_text("# r", encoding="utf-8")
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "Daily_Bias_Report_2026-08-13.pdf").write_bytes(b"%PDF")
    monkeypatch.setattr(intel, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(intel, "LOGS_DIR", tmp_path / "logs")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "--drive-only" in cmd:
            return _Proc(stdout="PDF: /o/r.pdf\nDrive: /d/r.pdf\n")
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0),
                                        output="PDF: /o/r.pdf\n", stderr="")

    monkeypatch.setattr(intel.subprocess, "run", fake_run)

    result = intel.publish_report_pdf(md)
    assert result == {"pdf_path": "/o/r.pdf", "drive_path": "/d/r.pdf"}
    assert any("--drive-only" in c for c in calls), "リカバリ経路が呼ばれていない"
    # 部分出力がログに残る（cron での原因追跡用）
    log = (tmp_path / "logs" / "publish_debug_Daily_Bias_Report_2026-08-13.log")
    assert log.exists() and "PDF: /o/r.pdf" in log.read_text(encoding="utf-8")
    assert "回復" in capsys.readouterr().out


def test_timeout_without_existing_pdf_reraises(tmp_path, monkeypatch):
    md = tmp_path / "Daily_Bias_Report_2026-08-13.md"
    md.write_text("# r", encoding="utf-8")
    monkeypatch.setattr(intel, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(intel, "LOGS_DIR", tmp_path / "logs")

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0), output="", stderr="")

    monkeypatch.setattr(intel.subprocess, "run", fake_run)
    with pytest.raises(subprocess.TimeoutExpired):
        intel.publish_report_pdf(md)


def test_default_timeout_is_within_hermes_script_budget():
    """Hermes cron の script_timeout_seconds (1500) の内側であること。"""
    assert 600 <= intel.PUBLISH_TIMEOUT <= 1200
