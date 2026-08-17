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


def _fake_cloudstorage_dir(tmp_path):
    """マウント済みの CloudStorage レイアウトを作る（ドライブルートまで存在させる）。"""
    d = (tmp_path / "CloudStorage" / "GoogleDrive-x@gmail.com"
         / "マイドライブ" / "Trading" / "Bias-Reports")
    d.mkdir(parents=True)
    return d


def test_has_drive_item_id_parses_xattr_output(monkeypatch, tmp_path):
    import subprocess as sp

    f = tmp_path / "r.pdf"
    f.write_bytes(b"%PDF")

    def fake_run(cmd, **kwargs):
        return sp.CompletedProcess(
            cmd, 0,
            stdout="com.apple.provenance\ncom.google.drivefs.item-id#S\n", stderr="",
        )

    monkeypatch.setattr(publish_report.subprocess, "run", fake_run)
    assert publish_report._has_drive_item_id(f) is True

    def fake_run_local_only(cmd, **kwargs):
        return sp.CompletedProcess(cmd, 0, stdout="com.apple.provenance\n", stderr="")

    monkeypatch.setattr(publish_report.subprocess, "run", fake_run_local_only)
    assert publish_report._has_drive_item_id(f) is False


def test_has_drive_item_id_is_true_when_xattr_unavailable(monkeypatch, tmp_path):
    """xattr が使えない環境では判定不能 → 誤警告を出さないため True。"""
    f = tmp_path / "r.pdf"
    f.write_bytes(b"%PDF")

    def boom(cmd, **kwargs):
        raise OSError("xattr not found")

    monkeypatch.setattr(publish_report.subprocess, "run", boom)
    assert publish_report._has_drive_item_id(f) is True


def test_copy_warns_when_drive_app_not_syncing(tmp_path, monkeypatch, capsys):
    """コピーは通るが item-id が付かない = Drive アプリ未起動。成功扱いにしない。"""
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF")
    drive_dir = _fake_cloudstorage_dir(tmp_path)
    monkeypatch.setattr(
        publish_report, "get_output_setting",
        lambda key: str(drive_dir) if key == "gdrive_pdf_dir" else None,
    )
    monkeypatch.setattr(publish_report, "_has_drive_item_id", lambda p: False)
    monkeypatch.setattr(publish_report, "DRIVE_SYNC_TIMEOUT", 0.0)

    assert publish_report._copy_to_drive(pdf) is None
    out = capsys.readouterr().out
    assert "反映されていない" in out and "Drive アプリが起動していない" in out
    # ローカルへのコピー自体は残す（復旧時に再アップロードされる）
    assert (drive_dir / "r.pdf").exists()


def test_copy_succeeds_when_item_id_present(tmp_path, monkeypatch, capsys):
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF")
    drive_dir = _fake_cloudstorage_dir(tmp_path)
    monkeypatch.setattr(
        publish_report, "get_output_setting",
        lambda key: str(drive_dir) if key == "gdrive_pdf_dir" else None,
    )
    monkeypatch.setattr(publish_report, "_has_drive_item_id", lambda p: True)

    assert publish_report._copy_to_drive(pdf) == drive_dir / "r.pdf"
    assert "反映されていない" not in capsys.readouterr().out


def test_drive_only_copies_existing_pdf_without_rendering(tmp_path, monkeypatch, capsys):
    """--drive-only は Playwright を起動せず、既存 PDF を Drive へコピーする。"""
    src_md = tmp_path / "brain" / "Daily_Bias_Report_2026-08-13.md"
    src_md.parent.mkdir(parents=True)
    src_md.write_text("# report", encoding="utf-8")
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "Daily_Bias_Report_2026-08-13.pdf").write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(publish_report, "OUTPUT_DIR", output_dir)

    def fail_render(*args, **kwargs):
        raise AssertionError("drive-only では render を呼ばない")

    monkeypatch.setattr(publish_report, "render", fail_render)
    drive_dir = tmp_path / "drive"
    monkeypatch.setattr(
        publish_report, "get_output_setting",
        lambda key: str(drive_dir) if key == "gdrive_pdf_dir" else None,
    )

    assert publish_report.publish(src_md, drive_only=True) == 0
    out = capsys.readouterr().out
    assert "PDF: " in out and "Drive: " in out
    assert (drive_dir / "Daily_Bias_Report_2026-08-13.pdf").exists()


def test_drive_only_without_existing_pdf_is_soft(tmp_path, monkeypatch, capsys):
    src_md = tmp_path / "r.md"
    src_md.write_text("# report", encoding="utf-8")
    monkeypatch.setattr(publish_report, "OUTPUT_DIR", tmp_path / "output")
    assert publish_report.publish(src_md, drive_only=True) == 0
    assert "既存 PDF が無い" in capsys.readouterr().out


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
