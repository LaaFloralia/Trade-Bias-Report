"""Deep Bias Report — Markdown を HTML / PDF にレンダリングする。

入力: output/Deep_Bias_Report_<日付>.md
出力: 同ベース名の .html と .pdf

実装:
- MD → HTML: markdown ライブラリ（extensions: tables, fenced_code, toc）
- テンプレ: templates/report.html を読み {{TITLE}} / {{DATE_JST}} / {{CONTENT}} / {{STYLE}} / {{SUMMARY_HTML}} を置換
- PDF: playwright sync_api で headless Chromium 起動 → page.set_content(html) → page.pdf(format='A4', ...)

使い方:
    python scripts/render_report.py output/Deep_Bias_Report_2026-05-13.md
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Tuple

import markdown


def _project_root() -> Path:
    """Return the project root directory (parent of scripts/)."""
    return Path(__file__).resolve().parent.parent


def _extract_confidence_badge(summary_md: str) -> str:
    """Extract the highest confidence label mentioned in the executive summary.

    Looks for "信頼度バッジ: X" first. If not found, falls back to scanning the
    summary text for the labels in priority order (High > Med > Med-cautious > Low).
    Returns an HTML snippet for the cover, or an empty string if nothing is found.
    """
    if not summary_md:
        return ""

    # 1. Prefer the explicit "信頼度バッジ: X" / "信頼度: X" marker
    explicit = re.search(
        r"信頼度(?:バッジ)?[:：]?\s*\**\s*(High|Med-cautious|Med|Low)\b",
        summary_md, re.IGNORECASE,
    )
    if explicit:
        label = explicit.group(1)
    else:
        # 2. Fallback: highest-priority label present anywhere in S0
        priority = ["High", "Med-cautious", "Med", "Low"]
        label = None
        for cand in priority:
            if re.search(rf"\b{re.escape(cand)}\b", summary_md, re.IGNORECASE):
                label = cand
                break
        if label is None:
            return ""

    # Normalize to canonical CSS class
    label_lower = label.lower()
    if label_lower == "med-cautious":
        css = "cover-confidence-med-cautious"
        display = "Med-cautious"
    elif label_lower == "high":
        css = "cover-confidence-high"
        display = "High"
    elif label_lower == "med":
        css = "cover-confidence-med"
        display = "Med"
    elif label_lower == "low":
        css = "cover-confidence-low"
        display = "Low"
    else:
        return ""

    return f'<div class="cover-confidence {css}">信頼度: {display}</div>\n'


def _extract_title_and_summary(md_text: str) -> Tuple[str, str]:
    """Extract the document title (first H1) and the executive summary block (S0 / W0).

    Falls back gracefully if the markers are missing. Includes a confidence
    badge derived from the S0 / W0 content if one can be detected.
    """
    lines = md_text.splitlines()

    # Title: first H1
    title = "Deep Bias Report"
    for ln in lines:
        if ln.startswith("# "):
            title = ln[2:].strip()
            break

    # Summary: text between "S0" / "W0" header and the next section header or "---".
    summary_lines: list[str] = []
    in_summary = False
    for ln in lines:
        if re.search(
            r"(S0|W0)[:：]?\s*エグゼクティブサマリー|エグゼクティブサマリー",
            ln,
        ) and ln.lstrip().startswith("#"):
            in_summary = True
            continue
        if in_summary:
            # stop on next section header or "---" separator
            if re.match(r"^#{1,3}\s", ln) or ln.strip() == "---":
                if summary_lines and summary_lines[-1].strip() == "":
                    summary_lines.pop()
                break
            summary_lines.append(ln)

    summary_md = "\n".join(summary_lines).strip()
    if summary_md:
        badge_html = _extract_confidence_badge(summary_md)
        body_html = markdown.markdown(summary_md, extensions=["tables", "fenced_code"])
        summary_html = f"<h2>Executive Summary</h2>\n{badge_html}{body_html}"
    else:
        summary_html = ""

    return title, summary_html


def _md_to_html(md_text: str) -> str:
    """Render the body markdown to HTML."""
    return markdown.markdown(
        md_text,
        extensions=["tables", "fenced_code", "toc", "sane_lists", "nl2br"],
        output_format="html5",
    )


def render_html(md_path: Path, project_root: Path | None = None,
                renderer: str | None = None) -> Path:
    """Render Markdown to a self-contained HTML file alongside the source.

    renderer:
      - None（既定）: human-first レンダラ（scripts/human_report.py、認知負荷対策
        ダッシュボード + 全文詳細）。失敗時は legacy テンプレへ自動フォールバック。
      - "legacy": 旧テンプレ（templates/report.html + style.css）を強制。
      環境変数 REPORT_RENDERER=legacy でも旧テンプレを強制できる（cron 緊急退避用）。

    Returns the path to the generated HTML file.
    """
    if project_root is None:
        project_root = _project_root()

    md_text = md_path.read_text(encoding="utf-8")

    import os
    if renderer is None:
        renderer = os.environ.get("REPORT_RENDERER", "human")

    if renderer != "legacy":
        try:
            try:
                from scripts.human_report import build_html
            except ImportError:  # 直接実行時（sys.path[0] == scripts/）
                from human_report import build_html  # type: ignore[no-redef]

            html = build_html(md_text)
            html_path = md_path.with_suffix(".html")
            html_path.write_text(html, encoding="utf-8")
            return html_path
        except Exception as exc:  # noqa: BLE001 — レンダラ不具合でパイプラインを止めない
            print(
                f"[render] WARN: human renderer failed "
                f"({type(exc).__name__}: {exc}); falling back to legacy",
                file=sys.stderr,
            )

    title, summary_html = _extract_title_and_summary(md_text)

    template_path = project_root / "templates" / "report.html"
    style_path = project_root / "templates" / "style.css"

    template = template_path.read_text(encoding="utf-8")
    style = style_path.read_text(encoding="utf-8")

    body_html = _md_to_html(md_text)
    date_jst = datetime.now().strftime("%Y-%m-%d %H:%M JST")

    # naive but explicit token replacement — ordering matters for {{DATE_JST}} inside CSS too.
    html = template
    html = html.replace("{{STYLE}}", style.replace("{{DATE_JST}}", date_jst))
    html = html.replace("{{TITLE}}", title)
    html = html.replace("{{DATE_JST}}", date_jst)
    html = html.replace("{{SUMMARY_HTML}}", summary_html)
    html = html.replace("{{CONTENT}}", body_html)

    html_path = md_path.with_suffix(".html")
    html_path.write_text(html, encoding="utf-8")
    return html_path


def render_pdf(html_path: Path) -> Path:
    """Render the HTML file to a PDF via Playwright headless Chromium.

    Returns the path to the generated PDF file.

    最適化: emulate_media('print') を呼び出してから PDF 生成する。
    style.css の @media screen ブロック (mobile / dark mode) は print mode では
    解釈されないため、PDF に余分な font fallback (Osaka-Mono 等) が埋め込まれず、
    Weekly Deep Bias のような長文でも 3MB → 2MB 程度まで縮む。
    """
    # Lazy import: keep the module importable without Playwright (e.g., type-checking).
    from playwright.sync_api import sync_playwright

    pdf_path = html_path.with_suffix(".pdf")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(html_path.resolve().as_uri())
            page.wait_for_load_state("domcontentloaded")
            # screen 専用 CSS (モバイル / ダーク) を PDF レンダリングから除外する
            page.emulate_media(media="print")
            page.pdf(
                path=str(pdf_path),
                format="A4",
                margin={"top": "12mm", "bottom": "12mm", "left": "12mm", "right": "12mm"},
                print_background=True,
                prefer_css_page_size=False,
            )
        finally:
            browser.close()

    return pdf_path


def render(md_path: Path, project_root: Path | None = None, keep_html: bool = False) -> Tuple[Path | None, Path]:
    """Render Markdown to PDF (HTML は PDF 生成の中間ファイル).

    keep_html=False（デフォルト）: PDF 生成後に HTML を削除し (None, pdf_path) を返す。
    keep_html=True: HTML を保持し (html_path, pdf_path) を返す。
    """
    html_path = render_html(md_path, project_root=project_root)
    pdf_path = render_pdf(html_path)
    if not keep_html:
        html_path.unlink()
        html_path = None
    return html_path, pdf_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render Bias Report (MD → PDF; HTML は中間生成後デフォルトで削除)"
    )
    parser.add_argument("md_path", help="Path to the Bias Markdown file")
    parser.add_argument(
        "--keep-html",
        action="store_true",
        help="PDF 生成後も中間 HTML を残す（デフォルトは削除）",
    )
    parser.add_argument(
        "--html-only",
        action="store_true",
        help="HTML のみ生成（PDF をスキップ。--keep-html は暗黙的に有効）",
    )
    parser.add_argument(
        "--legacy",
        action="store_true",
        help="旧テンプレ（templates/report.html + style.css）でレンダリングする",
    )
    args = parser.parse_args(argv)

    md_path = Path(args.md_path).resolve()
    if not md_path.exists():
        print(f"ERROR: input not found: {md_path}", file=sys.stderr)
        return 1

    html_path = render_html(md_path, renderer="legacy" if args.legacy else None)

    if args.html_only:
        print(f"HTML: {html_path}")
        return 0

    pdf_path = render_pdf(html_path)
    print(f"PDF:  {pdf_path}")

    if args.keep_html:
        print(f"HTML: {html_path}")
    else:
        html_path.unlink()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
