"""Static-edition checks and real-browser evidence for one BTCUSD edition.

Equivalent to XAU scripts/report_acceptance.py (validate_bundle +
browser_check): desktop 1365 / wide 1800 / mobile 390, no script/iframe/
object/embed/form or inline handlers, no external request, every Markdown
block present in the rendered body, embedded source bytes identical, no
horizontal page overflow, no element escaping the viewport outside scroll
containers, no overlapping SVG labels; images (overview, every figure,
full-report tiles, mobile table right edges, full page) with sha256 go to the
render record. A browser measurement is evidence, not semantic approval.
"""
from __future__ import annotations

import base64
from html import unescape
from html.parser import HTMLParser
import os
from pathlib import Path
import re

from btc import SYMBOL
from btc import bundle as bundle_mod
from btc.common import digest, read_json, write_json

# Same criterion set as XAU scripts.report_acceptance.CHECKS (tests assert equality).
CHECKS = ('facts_match_sources', 'numbers_and_units', 'summary_preserves_conditions',
          'no_unverified_trading_claims', 'desktop_readable', 'mobile_readable',
          'figures_meaning_and_labels', 'no_content_loss')
VIEWPORTS = ((1365, 'desktop'), (1800, 'wide'), (390, 'mobile'))
HEIGHT = 950
TILE_STEP = 850
# Allow-list of what the shared renderer (render_report.py + visual_blocks.py),
# Python-Markdown (tables/fenced_code/sane_lists/nl2br/footnotes) and
# btc.presentation actually emit. Anything else fails closed.
ALLOWED_ELEMENTS = frozenset({
    'html', 'head', 'meta', 'title', 'style', 'body', 'a', 'nav', 'div', 'main', 'section', 'header', 'footer',
    'span', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'ol', 'ul', 'li', 'aside', 'article', 'details', 'summary',
    'table', 'thead', 'tbody', 'tr', 'th', 'td', 'code', 'pre', 'strong', 'em', 'b', 'i', 'br', 'hr',
    'blockquote', 'sup', 'small', 'figure', 'figcaption',
    'svg', 'g', 'desc', 'line', 'text', 'rect', 'circle', 'polyline',
})
GLOBAL_ATTRIBUTES = frozenset({'class', 'id', 'title', 'role', 'tabindex', 'aria-label', 'aria-labelledby',
                               'aria-hidden', 'lang', 'style'})
ELEMENT_ATTRIBUTES = {
    'meta': {'charset', 'name', 'content'},
    'a': {'href', 'download', 'target', 'rel'},
    'ol': {'start'},
    'th': {'align', 'colspan', 'rowspan'},
    'td': {'align', 'colspan', 'rowspan'},
    'svg': {'viewbox', 'xmlns'},
    'line': {'x1', 'y1', 'x2', 'y2'},
    'text': {'x', 'y', 'text-anchor'},
    'rect': {'x', 'y', 'width', 'height', 'rx'},
    'circle': {'cx', 'cy', 'r'},
    'polyline': {'points', 'fill'},
}
URL_ATTRIBUTES = frozenset({'href', 'src', 'xlink:href', 'action', 'formaction', 'poster', 'data', 'background',
                            'srcset', 'ping', 'cite', 'longdesc', 'manifest', 'codebase'})
SAFE_URL = re.compile(r'^(?:#[^\s]*|https?://\S+|data:image/png[;,]\S*|data:image/svg\+xml[;,]\S*|'
                      r'data:text/markdown[;,]\S*)$')
FIXED_LINKS = frozenset({'/reports'})  # btc.presentation back link
CSS_DANGER = re.compile(r'url\s*\(|@import|expression\s*\(|javascript:|(?<![\w-])behavior\s*:|-moz-binding', re.I)
RAW_TAG = re.compile(r'<[a-zA-Z][^>]*>')
RAW_HANDLER = re.compile(r'(?:^|[\s/"\'])on[a-z]+\s*=', re.I)
CONTROL_AND_SPACE = re.compile(r'[\x00-\x20\x7f-\x9f]+')


class AcceptanceError(ValueError):
    pass


def normalized_url(value: str) -> str:
    """Decoded attribute value without control characters or whitespace, lower case."""
    return CONTROL_AND_SPACE.sub('', value or '').lower()


def url_allowed(tag: str, value: str) -> bool:
    url = normalized_url(value)
    if url in FIXED_LINKS:
        return tag == 'a'
    if url.startswith('mailto:'):
        return tag == 'a'
    if url.startswith('data:text/markdown'):
        return tag == 'a'
    return bool(SAFE_URL.match(url))


VOID_ELEMENTS = frozenset({'meta', 'br', 'hr', 'img', 'link', 'base', 'input', 'col', 'area', 'source', 'wbr'})
RAW_TEXT = frozenset({'style', 'title', 'script', 'textarea', 'xmp', 'noscript', 'noembed', 'noframes', 'iframe'})


class _AllowList(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.problems: set[str] = set()
        self.stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == 'style' and self.stack[-1:] != ['head']:
            # The renderer puts <style> only directly under <head>. Inside <svg> a
            # browser parses style content as markup, unlike html.parser.
            self.problems.add('style_outside_head')
        self._check(tag, attrs)
        if tag not in VOID_ELEMENTS:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self._check(tag.lower(), attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.stack:
            while self.stack and self.stack.pop() != tag:
                pass

    def handle_data(self, data):
        current = self.stack[-1] if self.stack else ''
        if current in RAW_TEXT and '<' in data:
            self.problems.add('markup_in_raw_text')
        if current == 'style' and CSS_DANGER.search(data):
            self.problems.add('css_external_or_script')

    def handle_pi(self, data):
        self.problems.add('processing_instruction')

    def unknown_decl(self, data):
        self.problems.add('unknown_declaration')

    def _check(self, tag, attrs):
        tag = tag.lower()
        if tag not in ALLOWED_ELEMENTS:
            self.problems.add(f'element:{tag}' if re.fullmatch(r'[a-z0-9:-]{1,24}', tag) else 'element:invalid')
            return
        allowed = GLOBAL_ATTRIBUTES | ELEMENT_ATTRIBUTES.get(tag, set())
        names = [name.lower() for name, _ in attrs]
        if tag == 'meta' and 'http-equiv' in names:
            self.problems.add('meta_http_equiv')
        for name, value in attrs:
            name = (name or '').lower()
            value = value or ''
            if name.startswith('on'):
                self.problems.add('event_handler_attribute')
            elif name in URL_ATTRIBUTES:
                if name not in allowed or not url_allowed(tag, value):
                    self.problems.add('unsafe_url')
            elif name not in allowed:
                self.problems.add('attribute_not_allowed')
            elif name == 'style' and CSS_DANGER.search(value):
                self.problems.add('css_external_or_script')
            if 'javascript:' in normalized_url(value) or 'vbscript:' in normalized_url(value):
                self.problems.add('script_url')


def static_html_problems(html: str) -> list[str]:
    """Allow-list scan of every element and attribute (character references decoded).

    A second, raw pass over tag text catches handler attributes that a lenient
    parser could fold into another attribute value.
    """
    parser = _AllowList()
    parser.feed(html)
    parser.close()
    problems = set(parser.problems)
    for tag in RAW_TAG.findall(html):
        if RAW_HANDLER.search(tag):
            problems.add('event_handler_attribute')
        if 'javascript:' in normalized_url(unescape(tag)):
            problems.add('script_url')
    return sorted(problems)


def validate_edition(html_path, bundle_path, now=None) -> dict:
    html_path, bundle_path = Path(html_path), Path(bundle_path)
    try:
        b = bundle_mod.validate(bundle_path, now)
    except bundle_mod.BundleError as error:
        raise AcceptanceError(str(error)) from None
    figures = read_json(b['figuresPath'])
    audit = read_json(html_path.with_suffix('.presentation.audit.json'))
    if audit.get('source_sha256') != b['sourceSha256'] or audit.get('html_sha256') != digest(html_path):
        raise AcceptanceError('HTML or source digest mismatch')
    if audit.get('summary_sha256') != b['summarySha256'] or audit.get('visuals_sha256') != b['figuresSha256']:
        raise AcceptanceError('Presentation sidecars do not match bundle')
    if audit.get('figure_count') != len(figures) or audit.get('numeric_check') != 'pass':
        raise AcceptanceError('Rendered figures failed numeric checks')
    html = html_path.read_text(encoding='utf-8')
    if static_html_problems(html):
        raise AcceptanceError('Active content or external resource is prohibited')
    for name, value in (('report-kind', b['kind']), ('report-symbol', SYMBOL),
                        ('report-session-slot', b['sessionSlot']), ('report-as-of', b['asOf']),
                        ('report-date', b['reportDate'])):
        if f'<meta name="{name}" content="{value}">' not in html:
            raise AcceptanceError('HTML edition metadata is missing')
    return b


class SourceBlocks(HTMLParser):
    TAGS = {'p', 'th', 'td', 'h1', 'h2', 'h3', 'h4', 'li'}

    def __init__(self):
        super().__init__()
        self.level, self.current, self.blocks = 0, [], []

    def handle_starttag(self, tag, attrs):
        if tag in self.TAGS:
            if not self.level:
                self.current = []
            self.level += 1

    def handle_endtag(self, tag):
        if tag in self.TAGS and self.level:
            self.level -= 1
            if not self.level:
                self.blocks.append(''.join(self.current))

    def handle_data(self, data):
        if self.level:
            self.current.append(data)


def compact(text: str) -> str:
    return re.sub(r'\s+', '', text).replace('↩', '')


DOM_ATTRIBUTES_JS = r'''() => {
  // Every element and attribute of the live DOM: handlers, script URLs, schemes outside the allow-list.
  const urlAttrs = new Set(['href','src','xlink:href','action','formaction','poster','data','background','srcset','ping','cite']);
  const ok = (tag, v) => {
    const u = v.replace(/[\u0000-\u0020\u007f-\u009f]+/g, '').toLowerCase();
    if (u === '/reports' || u.startsWith('mailto:') || u.startsWith('data:text/markdown')) return tag === 'a';
    return /^(#|https?:\/\/|data:image\/png[;,]|data:image\/svg\+xml[;,])/.test(u);
  };
  let bad = 0;
  for (const el of document.querySelectorAll('*')) {
    const tag = el.localName.toLowerCase();
    if (['base','link','iframe','frame','object','embed','form','script','img','video','audio','source','input','button'].includes(tag)) bad += 1;
    if (tag === 'meta' && el.hasAttribute('http-equiv')) bad += 1;
    for (const a of el.attributes) {
      const n = a.name.toLowerCase();
      const v = a.value.replace(/[\u0000-\u0020\u007f-\u009f]+/g, '').toLowerCase();
      if (n.startsWith('on') || n === 'srcdoc' || v.includes('javascript:') || v.includes('vbscript:')) bad += 1;
      else if (urlAttrs.has(n) && !ok(tag, a.value)) bad += 1;
      else if (n === 'style' && /url\s*\(|@import|expression\s*\(/i.test(a.value)) bad += 1;
    }
  }
  return bad;
}'''

ESCAPING_ELEMENTS_JS = '''() => {
  const limit = window.innerWidth + 1;
  const scrollers = '.table-wrap, .data-figure, pre, #source-markdown, .source-markdown';
  let count = 0;
  for (const el of document.querySelectorAll('main *, nav *, body > a')) {
    if (el.closest(scrollers)) continue;
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.right > limit) count += 1;
  }
  return count;
}'''

SVG_OVERLAP_JS = '''() => {
  let overlaps = 0;
  for (const svg of document.querySelectorAll('svg.data-svg')) {
    const boxes = [...svg.querySelectorAll('text')].map(t => t.getBoundingClientRect())
      .filter(r => r.width > 0 && r.height > 0);
    for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
      const a = boxes[i], b = boxes[j];
      const w = Math.min(a.right, b.right) - Math.max(a.left, b.left);
      const h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
      if (w > 1 && h > 2) overlaps += 1;
    }
  }
  return overlaps;
}'''


def _launch(playwright):
    channel = os.environ.get('BTC_PLAYWRIGHT_CHANNEL')  # dev verification only, e.g. "chrome"
    if channel:
        return playwright.chromium.launch(headless=True, channel=channel), f'channel:{channel}'
    return playwright.chromium.launch(headless=True), 'playwright-chromium'


def browser_check(html_path, bundle_path, *, now=None) -> str:
    import markdown
    from playwright.sync_api import sync_playwright
    b = validate_edition(html_path, bundle_path, now)
    html_path = Path(html_path).absolute()
    out = html_path.with_suffix('.render-evidence')
    out.mkdir(mode=0o700, exist_ok=True)
    source_bytes = Path(b['sourcePath']).read_bytes()
    source = source_bytes.decode('utf-8')
    expected = SourceBlocks()
    expected.feed(markdown.markdown(source, extensions=['tables', 'footnotes', 'fenced_code']))
    record = {'schemaVersion': 1, 'symbol': SYMBOL, 'htmlSha256': digest(html_path),
              'bundleSha256': digest(bundle_path), 'status': 'failed', 'viewports': [], 'images': [],
              'sourceBlocks': len(expected.blocks)}

    def shot(path, viewport, **extra):
        os.chmod(path, 0o600)
        record['images'].append({'path': str(path), 'sha256': digest(path), 'viewport': viewport, **extra})

    with sync_playwright() as p:
        browser, record['browser'] = _launch(p)
        try:
            for width, label in VIEWPORTS:
                # Page scripts never run (Playwright's own evaluate still works).
                page = browser.new_page(viewport={'width': width, 'height': HEIGHT}, device_scale_factor=1,
                                        java_script_enabled=False)
                blocked = []

                def route(route_obj, blocked=blocked):
                    if route_obj.request.url.startswith(('file:', 'data:')):
                        return route_obj.continue_()
                    blocked.append(1)
                    return route_obj.abort()
                page.route('**/*', route)
                page.goto(html_path.as_uri(), wait_until='load')
                page.evaluate('document.fonts.ready')
                if page.locator('script,iframe,object,embed,form').count():
                    raise AcceptanceError('Active document nodes are prohibited')
                unsafe = page.evaluate(DOM_ATTRIBUTES_JS)
                if unsafe:
                    raise AcceptanceError(f'Unsafe DOM attributes: {unsafe}')
                if blocked:
                    raise AcceptanceError('External resource request is prohibited')
                page.locator('details.chapter').evaluate_all('(nodes) => nodes.forEach(n => n.open = true)')
                body = compact(page.locator('.markdown-body').inner_text())
                blocks = expected.blocks
                if source.lstrip().startswith('# ') and blocks:
                    if compact(blocks[0]) not in compact(page.locator('footer').inner_text()):
                        raise AcceptanceError('Original report title is missing')
                    blocks = blocks[1:]
                missing = [x for x in blocks if compact(x) and compact(x) not in body]
                if missing:
                    raise AcceptanceError(f'Rendered source has missing blocks: {len(missing)}')
                encoded = page.locator('#source-markdown a[download]').get_attribute('href').split(',', 1)[1]
                if base64.b64decode(encoded) != source_bytes:
                    raise AcceptanceError('Embedded original source bytes differ')
                if page.evaluate('document.documentElement.scrollWidth > innerWidth + 1'):
                    raise AcceptanceError(f'{label} body overflows viewport')
                escaping = page.evaluate(ESCAPING_ELEMENTS_JS)
                if escaping:
                    raise AcceptanceError(f'{label} has elements beyond the viewport: {escaping}')
                overlaps = page.evaluate(SVG_OVERLAP_JS)
                if overlaps:
                    raise AcceptanceError(f'{label} figure labels overlap: {overlaps}')
                figure_count = page.locator('.data-figure').count()
                top = out / f'{label}-overview.png'
                page.screenshot(path=str(top), full_page=False)
                shot(top, label)
                if label != 'wide':
                    for i, figure in enumerate(page.locator('.data-figure').all()):
                        path = out / f'{label}-figure-{i + 1}.png'
                        figure.screenshot(path=str(path))
                        shot(path, label)
                    rect = page.locator('.full-report').evaluate('''(element) => {
                        const r = element.getBoundingClientRect();
                        return {x: r.left + window.scrollX, y: r.top + window.scrollY, width: r.width, height: r.height};
                    }''')
                    top_y, bottom_y, index = rect['y'], rect['y'] + rect['height'], 0
                    while top_y < bottom_y:
                        index += 1
                        tile = out / f'{label}-source-{index:02}.png'
                        clip = {'x': rect['x'], 'y': top_y, 'width': rect['width'],
                                'height': min(HEIGHT, bottom_y - top_y)}
                        page.screenshot(path=str(tile), full_page=True, clip=clip)
                        shot(tile, label, section='full-report', clip=clip)
                        top_y += TILE_STEP
                if label == 'mobile':
                    for index, table in enumerate(page.locator('.table-wrap').all()):
                        if table.evaluate('(e)=>e.scrollWidth > e.clientWidth + 1'):
                            table.evaluate('(e)=>e.scrollLeft=e.scrollWidth')
                            tile = out / f'mobile-table-right-{index + 1}.png'
                            table.screenshot(path=str(tile))
                            shot(tile, label)
                            table.evaluate('(e)=>e.scrollLeft=0')
                full = out / f'{label}-full.png'
                page.evaluate('document.documentElement.style.scrollBehavior = "auto"; window.scrollTo({top:0,left:0,behavior:"instant"})')
                page.wait_for_function('window.scrollY === 0')
                page.screenshot(path=str(full), full_page=True)
                shot(full, label)
                record['viewports'].append({'width': width, 'height': HEIGHT, 'label': label,
                                            'bodyOverflow': False, 'elementsBeyondViewport': 0,
                                            'svgLabelOverlaps': 0, 'externalRequests': 0,
                                            'figureCount': figure_count, 'sourceBlocksMissing': 0})
                page.close()
        finally:
            browser.close()
    record['status'] = 'passed'
    result = html_path.with_suffix('.render.json')
    write_json(result, record)
    return str(result)
