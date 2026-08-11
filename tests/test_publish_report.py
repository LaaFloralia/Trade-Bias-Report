"""publish_report.py（MD → PDF → Google Drive 発行）のユニットテスト。

Playwright / 実 Drive には触れず、render と Drive パスをモックして
「ソフト障害はすべて exit 0」「stdout の PDF:/Drive: 契約」を検証する。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import publish_report  # noqa: E402


def test_drive_root_detects_cloudstorage_drive():
    p = Path(
        "/Users/laa/Library/CloudStorage/GoogleDrive-x@gmail.com/マイドライブ/Trading/Bias-Reports"
    )
    assert publish_report._drive_root(p) == Path(
        "/Users/laa/Library/CloudStorage/GoogleDrive-x@gmail.com"
    )
    # CloudStorage 配下でないパスは未マウント判定の対象外
    assert publish_report._drive_root(Path("/tmp/reports")) is None


def test_missing_input_md_is_soft_failure(capsys):
    rc = publish_report.publish(Path("/no/such/report.md"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "WARN" in out and "存在しない" in out


def test_publish_renders_in_output_and_copies_to_drive(tmp_path, monkeypatch, capsys):
    # 入力 MD は output/ の外 (Brain 相当)
    src_md = tmp_path / "brain" / "Daily_Bias_Report_2026-08-11.md"
    src_md.parent.mkdir(parents=True)
    src_md.write_text("# report", encoding="utf-8")

    output_dir = tmp_path / "output"
    monkeypatch.setattr(publish_report, "OUTPUT_DIR", output_dir)

    def fake_render(work_md, project_root=None, keep_html=False):
        assert work_md.parent == output_dir, "MD は output/ に一時コピーして描画する"
        pdf = work_md.with_suffix(".pdf")
        pdf.write_bytes(b"%PDF-1.4 fake")
        return None, pdf

    monkeypatch.setattr(publish_report, "render", fake_render)
    drive_dir = tmp_path / "drive" / "Bias-Reports"
    monkeypatch.setattr(
        publish_report, "get_output_setting",
        lambda key: str(drive_dir) if key == "gdrive_pdf_dir" else None,
    )

    rc = publish_report.publish(src_md)
    assert rc == 0
    out = capsys.readouterr().out
    assert "PDF: " in out and "Drive: " in out
    # 一時コピーの MD は削除され、PDF と Drive コピーが残る
    assert not (output_dir / src_md.name).exists()
    assert (output_dir / "Daily_Bias_Report_2026-08-11.pdf").exists()
    assert (drive_dir / "Daily_Bias_Report_2026-08-11.pdf").exists()
    # 元の Brain 側 MD は消さない
    assert src_md.exists()


def test_publish_no_drive_skips_copy(tmp_path, monkeypatch, capsys):
    src_md = tmp_path / "r.md"
    src_md.write_text("# report", encoding="utf-8")
    output_dir = tmp_path / "output"
    monkeypatch.setattr(publish_report, "OUTPUT_DIR", output_dir)

    def fake_render(work_md, project_root=None, keep_html=False):
        pdf = work_md.with_suffix(".pdf")
        pdf.write_bytes(b"%PDF")
        return None, pdf

    monkeypatch.setattr(publish_report, "render", fake_render)
    called = []
    monkeypatch.setattr(publish_report, "_copy_to_drive", lambda pdf: called.append(pdf))

    rc = publish_report.publish(src_md, no_drive=True)
    assert rc == 0
    assert called == []
    assert "PDF: " in capsys.readouterr().out


def test_unmounted_drive_warns_and_exits_zero(tmp_path, monkeypatch, capsys):
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF")
    # CloudStorage 配下だがドライブルートが存在しない → 未マウント扱い
    fake_gdrive = tmp_path / "CloudStorage" / "GoogleDrive-x@gmail.com" / "マイドライブ" / "Bias-Reports"
    monkeypatch.setattr(
        publish_report, "get_output_setting",
        lambda key: str(fake_gdrive) if key == "gdrive_pdf_dir" else None,
    )
    assert publish_report._copy_to_drive(pdf) is None
    out = capsys.readouterr().out
    assert "未マウント" in out
    # 偽のローカルディレクトリを作らない
    assert not fake_gdrive.exists()


def test_render_failure_is_soft_and_cleans_up(tmp_path, monkeypatch, capsys):
    src_md = tmp_path / "brain" / "r.md"
    src_md.parent.mkdir(parents=True)
    src_md.write_text("# report", encoding="utf-8")
    output_dir = tmp_path / "output"
    monkeypatch.setattr(publish_report, "OUTPUT_DIR", output_dir)

    def broken_render(work_md, project_root=None, keep_html=False):
        raise RuntimeError("playwright crashed")

    monkeypatch.setattr(publish_report, "render", broken_render)
    rc = publish_report.publish(src_md)
    assert rc == 0
    assert "PDF 生成に失敗" in capsys.readouterr().out
    assert not (output_dir / src_md.name).exists(), "一時 MD は失敗時も削除される"
