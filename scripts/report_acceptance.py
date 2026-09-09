"""Hash-bound factual and visual acceptance for one report edition.

A browser measurement is evidence, not semantic approval. A separate reviewer
must inspect the generated images and meaning and supply a hash-bound review.
"""
from __future__ import annotations

from html.parser import HTMLParser
import base64
import hashlib
import json
from pathlib import Path
import re

from scripts.report_bundle import BundleError, check_bindings, digest, require_fresh

CHECKS = ('facts_match_sources', 'numbers_and_units', 'summary_preserves_conditions',
          'no_unverified_trading_claims', 'desktop_readable', 'mobile_readable',
          'figures_meaning_and_labels', 'no_content_loss')


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def validate_bundle(html_path, bundle_path, now=None):
    html_path, bundle_path = Path(html_path), Path(bundle_path)
    b = json.loads(bundle_path.read_text())
    for key in ('data', 'source', 'summary', 'figures'):
        if digest(b[f'{key}Path']) != b[f'{key}Sha256']:
            raise BundleError(f'{key} input changed after bundle creation')
    data = json.loads(Path(b['dataPath']).read_text())
    if require_fresh(data, now).isoformat() != b['asOf']:
        raise BundleError('Bundle timestamp does not match source data')
    if b['missing'] or set(b['figureIds']) != set(b['requiredFigures']):
        raise BundleError('Required figures are missing')
    figures = json.loads(Path(b['figuresPath']).read_text())
    check_bindings(data, b, figures)
    audit = json.loads(html_path.with_suffix('.presentation.audit.json').read_text())
    if audit.get('source_sha256') != b['sourceSha256'] or audit.get('html_sha256') != digest(html_path):
        raise BundleError('HTML or source digest mismatch')
    if audit.get('summary_sha256') != b['summarySha256'] or audit.get('visuals_sha256') != b['figuresSha256']:
        raise BundleError('Presentation sidecars do not match bundle')
    if audit.get('figure_count') != len(figures) or audit.get('numeric_check') != 'pass':
        raise BundleError('Rendered figures failed numeric checks')
    html = html_path.read_text()
    if re.search(r'<\s*(script|iframe|object|embed|form)\b|\son\w+\s*=', html, re.I):
        raise BundleError('Active content is prohibited')
    for name, value in [('report-kind', b['kind']), ('report-as-of', b['asOf']), ('report-date', b['reportDate'])]:
        if f'<meta name="{name}" content="{value}">' not in html:
            raise BundleError('HTML edition metadata is missing')
    if 'href="/reports"' not in html:
        raise BundleError('Reports return link is missing')
    return b


class SourceBlocks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.level, self.current, self.blocks = 0, [], []
    def handle_starttag(self, tag, attrs):
        if tag in {'p', 'th', 'td', 'h1', 'h2', 'h3', 'h4', 'li'}:
            if not self.level:
                self.current = []
            self.level += 1
    def handle_endtag(self, tag):
        if tag in {'p', 'th', 'td', 'h1', 'h2', 'h3', 'h4', 'li'} and self.level:
            self.level -= 1
            if not self.level:
                self.blocks.append(''.join(self.current))
    def handle_data(self, data):
        if self.level:
            self.current.append(data)


def compact(text):
    return re.sub(r'\s+', '', text).replace('↩', '')


def browser_check(html_path, bundle_path, *, now=None):
    import markdown
    from playwright.sync_api import sync_playwright
    b = validate_bundle(html_path, bundle_path, now)
    html_path = Path(html_path).absolute()
    out = html_path.with_suffix('.render-evidence')
    out.mkdir(exist_ok=True)
    source = Path(b['sourcePath']).read_text()
    expected = SourceBlocks()
    expected.feed(markdown.markdown(source, extensions=['tables', 'footnotes', 'fenced_code']))
    record = {'schemaVersion': 1, 'htmlSha256': digest(html_path), 'bundleSha256': digest(bundle_path),
              'status': 'failed', 'viewports': [], 'images': [], 'sourceBlocks': len(expected.blocks)}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            for width, label in [(1365, 'desktop'), (390, 'mobile')]:
                page = browser.new_page(viewport={'width': width, 'height': 950}, device_scale_factor=1)
                page.route('**/*', lambda route: route.continue_() if route.request.url.startswith('file:') else route.abort())
                page.goto(html_path.as_uri(), wait_until='load')
                page.evaluate('document.fonts.ready')
                if page.locator('script,iframe,object,embed,form').count():
                    raise BundleError('Active document nodes are prohibited')
                page.locator('details.chapter').evaluate_all('(nodes) => nodes.forEach(n => n.open = true)')
                body = compact(page.locator('.markdown-body').inner_text())
                blocks = expected.blocks
                if source.lstrip().startswith('# ') and blocks:
                    if compact(blocks[0]) not in compact(page.locator('footer').inner_text()):
                        raise BundleError('Original report title is missing')
                    blocks = blocks[1:]
                missing = [x for x in blocks if compact(x) and compact(x) not in body]
                if missing:
                    raise BundleError(f'Rendered source has missing blocks: {len(missing)}')
                encoded = page.locator('#source-markdown a[download]').get_attribute('href').split(',', 1)[1]
                if base64.b64decode(encoded) != Path(b['sourcePath']).read_bytes():
                    raise BundleError('Embedded original source bytes differ')
                overflow = page.evaluate('document.documentElement.scrollWidth > innerWidth + 1')
                if overflow:
                    raise BundleError(f'{label} body overflows viewport')
                top = out / f'{label}-overview.png'
                page.screenshot(path=str(top), full_page=False)
                record['images'].append({'path': str(top), 'sha256': digest(top), 'viewport': label})
                for i, figure in enumerate(page.locator('.data-figure').all()):
                    path = out / f'{label}-figure-{i+1}.png'
                    figure.screenshot(path=str(path))
                    record['images'].append({'path': str(path), 'sha256': digest(path), 'viewport': label})
                # Readable source tiles keep the full tables legible to an image reviewer.
                # Figure screenshots scroll the viewport. Clips for full-page
                # screenshots require document coordinates, not bounding_box's
                # viewport-relative coordinates.
                rect = page.locator('.full-report').evaluate('''(element) => {
                    const r = element.getBoundingClientRect();
                    return {x: r.left + window.scrollX, y: r.top + window.scrollY,
                            width: r.width, height: r.height};
                }''')
                top_y, bottom_y = rect['y'], rect['y'] + rect['height']
                tile_index = 0
                while top_y < bottom_y:
                    tile_index += 1
                    tile = out / f'{label}-source-{tile_index:02}.png'
                    clip = {'x': rect['x'], 'y': top_y, 'width': rect['width'],
                            'height': min(950, bottom_y-top_y)}
                    page.screenshot(path=str(tile), full_page=True, clip=clip)
                    record['images'].append({'path': str(tile), 'sha256': digest(tile),
                                             'viewport': label, 'section': 'full-report', 'clip': clip})
                    top_y += 850
                if label == 'mobile':
                    for index, table in enumerate(page.locator('.table-wrap').all()):
                        if table.evaluate('(e)=>e.scrollWidth > e.clientWidth + 1'):
                            table.evaluate('(e)=>e.scrollLeft=e.scrollWidth')
                            tile = out / f'mobile-table-right-{index+1}.png'
                            table.screenshot(path=str(tile))
                            record['images'].append({'path': str(tile), 'sha256': digest(tile), 'viewport': label})
                            table.evaluate('(e)=>e.scrollLeft=0')
                # Full text evidence is separate from the compact first screen.
                path = out / f'{label}-full.png'
                page.evaluate('document.documentElement.style.scrollBehavior = \"auto\"; window.scrollTo({top:0,left:0,behavior:\"instant\"})')
                page.wait_for_function('window.scrollY === 0')
                page.screenshot(path=str(path), full_page=True)
                record['images'].append({'path': str(path), 'sha256': digest(path), 'viewport': label})
                record['viewports'].append({'width': width, 'height': 950, 'bodyOverflow': False,
                                            'figureCount': page.locator('.data-figure').count(), 'sourceBlocksMissing': 0})
                page.close()
        finally:
            browser.close()
    record['status'] = 'passed'
    result = html_path.with_suffix('.render.json')
    write(result, record)
    return str(result)


def validate_acceptance(html_path, bundle_path, acceptance_path, now=None):
    validate_bundle(html_path, bundle_path, now)
    review_bytes = Path(acceptance_path).read_bytes()
    review = json.loads(review_bytes)
    if review.get('status') != 'passed' or not review.get('reviewer') or not review.get('reviewedAt'):
        raise BundleError('Independent semantic and visual review is pending')
    if review.get('htmlSha256') != digest(html_path) or review.get('bundleSha256') != digest(bundle_path):
        raise BundleError('Review applies to different report bytes')
    if any(review.get('checks', {}).get(check) is not True for check in CHECKS):
        raise BundleError('Semantic or visual review is incomplete')
    render_path = Path(html_path).with_suffix('.render.json')
    if review.get('renderSha256') != digest(render_path):
        raise BundleError('Review does not reference current browser evidence')
    render = json.loads(render_path.read_text())
    if render.get('status') != 'passed' or render.get('htmlSha256') != digest(html_path) or render.get('bundleSha256') != digest(bundle_path):
        raise BundleError('Real browser render is not verified')
    for image in render.get('images', []):
        if digest(image['path']) != image['sha256']:
            raise BundleError('Reviewed render evidence changed')
    if not render.get('images') or {v['width'] for v in render.get('viewports', [])} != {1365, 390}:
        raise BundleError('Desktop/mobile render evidence is missing')
    return {'acceptanceSha256': hashlib.sha256(review_bytes).hexdigest(), 'publicationReady': True,
            'htmlSha256': review['htmlSha256'], 'bundleSha256': review['bundleSha256']}
