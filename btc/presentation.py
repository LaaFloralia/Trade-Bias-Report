"""HTML presentation through the shared human-first renderer (not modified).

Same contract as the XAU adapter (chart-intel/report_presentation.py): the
summary/figure sidecars must quote the Markdown exactly, the HTML is static
(no script, no external resource), the Markdown source is embedded byte for
byte, and rendering is not acceptance. BTC adds ``report-symbol`` and
``report-session-slot`` metadata.
"""
from __future__ import annotations

from html import escape
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from btc import SYMBOL

RENDERER = Path(os.environ.get('BTC_RENDERER',
                               '/Users/laa/.agents/skills/human-first-docs/scripts/render_report.py'))
SUMMARY_FIELDS = ('conclusion', 'metrics', 'conditions', 'evidence', 'limitations')
KINDS = ('daily', 'weekly')
SESSION_SLOTS = ('am', 'pm', 'weekly')
NO_FIGURES = '図表は未作成です。本文の数値・出典を確認してください。図表を含む資料の完成確認は未実施です。'
STYLE = """<style>
:root{--ink:#273d36;--muted:#5c6761;--line:#d9ded5;--paper:#fffefb;--canvas:#f4f3ed;--accent:#486b56;--positive:#486b56;--negative:#705b61;--wash:#f5f6f0}
.reading-nav{position:static}
.reports-back{display:block;padding:14px max(24px,calc((100vw - 1080px)/2));background:#fffefb;font-size:14px}
.conclusion{background:#f0f3ec;color:var(--ink);border:1px solid var(--line);border-radius:0}
.conclusion .section-label,.conclusion p{color:var(--ink)}.conclusion p{white-space:pre-line;font-size:16px}
.data-figure{overflow-x:auto}.data-svg{min-width:0}.bar-label{font-size:15px}.plot-label{font-size:15px}
.markdown-body{overflow-wrap:anywhere}
.metrics{grid-template-columns:repeat(4,minmax(0,1fr))}
@media(max-width:600px){.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.metric:nth-child(odd){padding-left:0}.metric:nth-child(even){border-right:0}.reading-nav{gap:10px;padding:12px 14px}.reading-nav>div{gap:14px}.overview-grid>.decision-rail{grid-row:1}.figure-heading p,.data-figure figcaption,.figure-source,.brief-note,.asof-note{font-size:12px}.conclusion{padding:18px}.data-figure{padding:16px 12px}.plot-label,.bar-label,.axis-label{font-size:18px}}
@media print{.reports-back{display:none}.data-svg{min-width:0}.conclusion p{font-size:10pt}}
</style>"""


class PresentationError(ValueError):
    pass


def _load(path):
    raw = Path(path).read_bytes()
    return json.loads(raw.decode('utf-8')), hashlib.sha256(raw).hexdigest()


def validate_quotes(summary: dict, figures: list, source: str) -> None:
    """Every visible summary/figure item quotes a nonempty exact source passage."""
    if not isinstance(summary, dict):
        raise PresentationError('Summary must be a JSON object.')
    for field in SUMMARY_FIELDS:
        items = summary.get(field, [])
        if field == 'conclusion':
            items = [items] if items else []
        if not isinstance(items, list):
            raise PresentationError('Summary collections must be arrays.')
        for item in items:
            quote = item.get('source_quote') if isinstance(item, dict) else None
            if not isinstance(quote, str) or not quote.strip() or quote not in source:
                raise PresentationError('Summary source_quote must exactly match a nonempty source passage.')
    if not isinstance(figures, list):
        raise PresentationError('Figures must be a JSON array.')
    for figure in figures:
        if not isinstance(figure, dict) or not isinstance(figure.get('items'), list) or not figure['items']:
            raise PresentationError('Each figure must contain a nonempty items array.')
        for item in figure['items']:
            quote = item.get('source_quote') if isinstance(item, dict) else None
            if not isinstance(quote, str) or not quote.strip() or quote not in source:
                raise PresentationError('Figure source_quote must exactly match a nonempty source passage.')


def render(md_path, *, summary_path, visuals_path, as_of: str, kind: str, session_slot: str,
           generated_at: str, renderer: Path | None = None, timeout: int = 120) -> dict:
    """Render sibling .html and .presentation.audit.json; never overwrite inputs."""
    renderer = Path(renderer or RENDERER)
    md_path = Path(md_path).absolute()
    if md_path.suffix != '.md':
        raise PresentationError('Input must be a Markdown .md file.')
    if kind not in KINDS or session_slot not in SESSION_SLOTS or not isinstance(as_of, str) or not as_of:
        raise PresentationError('Report edition metadata is required')
    if not renderer.is_file():
        raise PresentationError('Shared renderer is unavailable')
    html_path = md_path.with_suffix('.html')
    audit_path = md_path.with_suffix('.presentation.audit.json')
    raw = md_path.read_bytes()
    source = raw.decode('utf-8')
    if not source.strip():
        raise PresentationError('Markdown source is empty.')
    summary, summary_hash = _load(summary_path)
    figures, figures_hash = _load(visuals_path)
    validate_quotes(summary, figures, source)
    if not figures:
        summary['note'] = '\n'.join(filter(None, [summary.get('note', ''), NO_FIGURES]))
    with tempfile.TemporaryDirectory(prefix='.presentation-', dir=md_path.parent) as temporary:
        stage = Path(temporary)
        staged_md = stage / md_path.name
        staged_md.write_bytes(raw)
        staged_summary, staged_visuals = stage / 'summary.json', stage / 'visuals.json'
        staged_summary.write_text(json.dumps(summary, ensure_ascii=False), encoding='utf-8')
        staged_visuals.write_text(json.dumps(figures, ensure_ascii=False), encoding='utf-8')
        staged_html, staged_audit = stage / 'report.html', stage / 'audit.json'
        command = [sys.executable, '-B', str(renderer), str(staged_md), '--output', str(staged_html),
                   '--summary', str(staged_summary), '--visuals', str(staged_visuals),
                   '--audit', str(staged_audit), '--as-of', as_of, '--generated-at', generated_at]
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        if result.returncode:
            # The renderer's own validation message is local text about our input.
            raise PresentationError('Shared renderer rejected the input: ' + result.stderr[-1500:])
        audit = json.loads(staged_audit.read_text(encoding='utf-8'))
        if audit.get('source_sha256') != hashlib.sha256(raw).hexdigest():
            raise PresentationError('Rendered source digest does not match the input bytes.')
        document = staged_html.read_text(encoding='utf-8')
        document = document.replace('<body>', '<body><a class="reports-back" href="/reports">レポート一覧へ戻る</a>', 1)
        document = document.replace('PDFには全文を収録しています。', '各章に原稿の全文を収録しています。')
        meta = ''.join(f'<meta name="{key}" content="{escape(value, quote=True)}">' for key, value in (
            ('report-kind', kind), ('report-symbol', SYMBOL), ('report-session-slot', session_slot),
            ('report-as-of', as_of), ('report-date', as_of[:10])))
        document = document.replace('</head>', STYLE + meta + '</head>', 1)
        staged_html.write_text(document, encoding='utf-8')
        audit.update({
            'adapter': 'chart-intel-btcusd/btc.presentation', 'renderer': str(renderer),
            'render_status': 'rendered', 'summary_mode': 'supplied',
            'visual_status': 'provided_data_checked' if figures else 'not_provided',
            'presentation_complete': False, 'pdf_status': 'not_generated',
            'publication_status': 'not_attempted', 'symbol': SYMBOL, 'session_slot': session_slot,
            'summary_sha256': summary_hash, 'visuals_sha256': figures_hash, 'as_of': as_of,
            'semantic_review': 'pending', 'html_sha256': hashlib.sha256(staged_html.read_bytes()).hexdigest(),
        })
        staged_audit.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.chmod(staged_html, 0o600)
        os.chmod(staged_audit, 0o600)
        os.replace(staged_html, html_path)
        os.replace(staged_audit, audit_path)
    return {'html_path': str(html_path), 'presentation_audit_path': str(audit_path)}
