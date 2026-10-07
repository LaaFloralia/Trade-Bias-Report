"""Static and DOM allow-list checks (A2) and parity with the XAU acceptance criteria."""
import pytest

from btc.acceptance import CHECKS, DOM_ATTRIBUTES_JS, static_html_problems

ATTACKS = [
    '<img/src=x/onerror=alert(1)>',
    '<svg/onload=alert(1)>',
    '<a href="jav&#x61;script:alert(1)">x</a>',
    '<meta charset=utf-8 http-equiv=refresh content="0;url=https://example.test/">',
    # case, character references, whitespace and control characters
    '<SVG/OnLoad=alert(1)>',
    '<p ONCLICK = "x()">x</p>',
    '<a href="JaVaScRiPt:alert(1)">x</a>',
    '<a href="&#106;avascript:alert(1)">x</a>',
    '<a href="&#x6A;&#x61;vascript:alert(1)">x</a>',
    '<a href=" java\tscript:alert(1)">x</a>',
    '<a href="java&#10;script:alert(1)">x</a>',
    '<a href="java&Tab;script:alert(1)">x</a>',
    '<a href="  javascript:alert(1)">x</a>',
    '<META HTTP-EQUIV="refresh" content="0">',
    '<a href="vbscript:x">x</a>',
    '<a href="data:text/html,<script>alert(1)</script>">x</a>',
    '<script>alert(1)</script>',
    '<iframe srcdoc="<p>x</p>"></iframe>',
    '<object data="x"></object>',
    '<embed src="x">',
    '<form action="https://x.test/"><button formaction="javascript:x">b</button></form>',
    '<base href="https://x.test/">',
    '<link rel="stylesheet" href="https://x.test/a.css">',
    '<img src="https://x.test/a.png">',
    '<div style="background:url(//x.test/a.png)">x</div>',
    '<div style="background:URL ( https://x.test/a.png )">x</div>',
    '<style>@import "https://x.test/a.css";</style>',
    '<svg><a xlink:href="javascript:alert(1)"><text>x</text></a></svg>',
    '<p title="x" data-x="1">x</p>',
    '<svg><style><embed src=//x.invalid></style></svg>',
    '<body><style>p{color:red}</style></body>',
]


def test_checks_match_xau_criteria():
    from scripts.report_acceptance import CHECKS as XAU_CHECKS
    assert tuple(CHECKS) == tuple(XAU_CHECKS)


@pytest.mark.parametrize('html', ATTACKS)
def test_static_allow_list_rejects(html):
    assert static_html_problems(f'<html><body>{html}</body></html>')


def test_static_allow_list_accepts_renderer_output():
    assert static_html_problems('<html><body><p>ok</p><a href="https://example.com/">l</a>'
                                '<a href="#source-markdown">s</a><a class="reports-back" href="/reports">r</a>'
                                '<a download="r.md" href="data:text/markdown;charset=utf-8;base64,AA==">m</a>'
                                '<span class="split-segment" style="flex:40.0"></span>'
                                '<svg class="data-svg" viewBox="0 0 420 300" role="img"><line x1="1" y1="1" x2="2" y2="2"/>'
                                '<text x="1" y="2" text-anchor="end">a</text></svg></body></html>') == []
    # Escaped text that merely looks like markup is data, not markup.
    assert static_html_problems('<p>&lt;img src=x onerror=alert(1)&gt; javascript&#58;</p>') == []
    assert static_html_problems('<html><head><style>a > b {color: red}</style></head><body></body></html>') == []
    assert 'markup_in_raw_text' in static_html_problems('<svg><style><embed src=//x.invalid></style></svg>')


def test_dom_scan_counts_unsafe_attributes(browser_channel):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, **({'channel': browser_channel} if browser_channel else {}))
        try:
            page = browser.new_page(java_script_enabled=False)
            page.set_content('<p>ok</p><a href="#x">a</a><a href="/reports">b</a>')
            assert page.evaluate(DOM_ATTRIBUTES_JS) == 0
            for html in ATTACKS[:3] + ['<meta http-equiv="refresh" content="999">', '<a href="java&#10;script:x">x</a>',
                         '<p onclick="x">x</p>']:
                page.set_content(html)
                assert page.evaluate(DOM_ATTRIBUTES_JS) > 0, html
            # Parser differential: inside <svg> a browser parses <style> content as markup.
            page.set_content('<svg><style><embed src=//x.invalid></style></svg>')
            assert page.evaluate(DOM_ATTRIBUTES_JS) > 0
            assert page.locator('script,iframe,object,embed,form').count() > 0
            page.set_content('<script>document.title = "ran"</script>')
            assert page.evaluate('document.title') == ''  # page scripts are disabled
        finally:
            browser.close()
