"""Bias Report の MD を PDF 化して Google Drive 同期フォルダへ発行する。

入力: 任意の場所にある Bias Report Markdown（Brain 内の MD でも可）
出力:
  1. PDF: output/<同ベース名>.pdf（render_report.render を再利用）
  2. Drive コピー: config.yaml `output.gdrive_pdf_dir` へ（--no-drive で省略）

設計方針（レポート生成本体を止めない）:
  - すべてのソフト障害（入力欠落 / PDF 生成失敗 / Drive 未マウント / コピー失敗）は
    WARN を stderr ではなく stdout に出して exit 0 で終える。
  - Drive 未マウント判定: パスが CloudStorage 配下の場合、ドライブルート
    （.../CloudStorage/GoogleDrive-xxx）が存在しなければ未マウントとみなし、
    偽のローカルディレクトリを mkdir で作らずスキップする。
  - 成功時の stdout 契約（intel.py がパースする）:
        PDF: <生成した PDF の絶対パス>
        Drive: <Drive へコピーした PDF の絶対パス>

使い方:
    python scripts/publish_report.py ~/Brain/Calendar/Daily-Bias/Daily_Bias_Report_2026-08-11.md
    python scripts/publish_report.py <md_path> --no-drive
    python scripts/publish_report.py <md_path> --drive-only  # 既存 PDF の Drive 再コピー
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import get_output_setting  # noqa: E402
from scripts.render_report import render  # noqa: E402

OUTPUT_DIR = PROJECT_ROOT / "output"


def _drive_root(target: Path) -> Optional[Path]:
    """CloudStorage 配下のパスならドライブルート（GoogleDrive-xxx）を返す。

    例: /Users/laa/Library/CloudStorage/GoogleDrive-x@gmail.com/マイドライブ/...
        → /Users/laa/Library/CloudStorage/GoogleDrive-x@gmail.com
    CloudStorage 配下でなければ None（未マウント判定の対象外）。
    """
    parts = target.parts
    if "CloudStorage" not in parts:
        return None
    idx = parts.index("CloudStorage")
    if len(parts) <= idx + 1:
        return None
    return Path(*parts[: idx + 2])


def _render_pdf_in_output(md_path: Path) -> Optional[Path]:
    """MD を output/ で PDF 化して PDF パスを返す。失敗時は WARN を出して None。

    render_report.render は入力 MD の隣に出力する設計のため、MD が output/ の
    外にある場合は output/ に一時コピーして生成し、一時 MD は削除する。
    """
    OUTPUT_DIR.mkdir(exist_ok=True)
    work_md = OUTPUT_DIR / md_path.name
    temp_copy = work_md.resolve() != md_path.resolve()
    if temp_copy:
        shutil.copy2(md_path, work_md)
    t0 = time.time()
    try:
        _, pdf_path = render(work_md, project_root=PROJECT_ROOT)
        print(f"[publish] PDF 生成 {time.time() - t0:.1f}s")
        return pdf_path
    except Exception as exc:  # noqa: BLE001 — ソフト障害は exit 0
        print(f"[publish] WARN: PDF 生成に失敗: {type(exc).__name__}: {exc}")
        # render_html 成功後に render_pdf で失敗した場合の中間 HTML を掃除
        html_leftover = work_md.with_suffix(".html")
        if html_leftover.exists():
            html_leftover.unlink()
        return None
    finally:
        if temp_copy and work_md.exists():
            work_md.unlink()


def _copy_to_drive(pdf_path: Path) -> Optional[Path]:
    """PDF を config.yaml output.gdrive_pdf_dir へコピーする。スキップ/失敗は None。"""
    gdrive_dir = get_output_setting("gdrive_pdf_dir")
    if not gdrive_dir:
        print("[publish] WARN: config.yaml output.gdrive_pdf_dir が未設定のため Drive コピーをスキップ")
        return None
    target_dir = Path(str(gdrive_dir)).expanduser()
    root = _drive_root(target_dir)
    if root is not None and not root.exists():
        print(f"[publish] WARN: Google Drive が未マウント ({root}) のため Drive コピーをスキップ")
        return None
    t0 = time.time()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        drive_path = target_dir / pdf_path.name
        shutil.copy2(pdf_path, drive_path)
        print(f"[publish] Drive コピー {time.time() - t0:.1f}s")
        return drive_path
    except Exception as exc:  # noqa: BLE001 — ソフト障害は exit 0
        print(f"[publish] WARN: Drive コピーに失敗: {type(exc).__name__}: {exc}")
        return None


def publish(md_path: Path, no_drive: bool = False, drive_only: bool = False) -> int:
    """MD → PDF → Drive コピー。常に 0 を返す（ソフト障害はスキップして続行）。

    drive_only=True は PDF を生成し直さず、output/ の既存 PDF を Drive へ
    コピーするだけのリカバリモード（Playwright を起動しない）。
    """
    if not md_path.exists():
        print(f"[publish] WARN: 入力 MD が存在しないためスキップ: {md_path}")
        return 0

    if drive_only:
        pdf_path = OUTPUT_DIR / f"{md_path.stem}.pdf"
        if not pdf_path.exists():
            print(f"[publish] WARN: 既存 PDF が無いため Drive コピーをスキップ: {pdf_path}")
            return 0
    else:
        pdf_path = _render_pdf_in_output(md_path)
        if pdf_path is None:
            return 0
    print(f"PDF: {pdf_path.resolve()}")

    if no_drive:
        return 0
    drive_path = _copy_to_drive(pdf_path)
    if drive_path is not None:
        print(f"Drive: {drive_path.resolve()}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bias Report MD を PDF 化して Google Drive 同期フォルダへ発行する"
    )
    parser.add_argument("md_path", help="Bias Report Markdown のパス（Brain 内でも可）")
    parser.add_argument(
        "--no-drive", action="store_true",
        help="Google Drive へのコピーを省略（PDF 生成のみ）",
    )
    parser.add_argument(
        "--drive-only", action="store_true",
        help="PDF を作り直さず output/ の既存 PDF を Drive へコピーするだけ（リカバリ用）",
    )
    args = parser.parse_args(argv)
    if args.no_drive and args.drive_only:
        parser.error("--no-drive と --drive-only は同時に指定できない")
    return publish(
        Path(args.md_path).expanduser(),
        no_drive=args.no_drive,
        drive_only=args.drive_only,
    )


if __name__ == "__main__":
    raise SystemExit(main())
