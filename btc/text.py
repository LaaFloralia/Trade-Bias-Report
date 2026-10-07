"""Neutralising external strings before they reach Markdown (A2).

Headlines, article excerpts, provider labels and parsed calendar names are
data, not markup. They are rewritten once, when they enter the facts, so the
briefing, the Markdown, the figures and machine.json all carry the same
harmless text: characters that could open HTML, links, images, tables or
code (``< > [ ] | ` \\``) become full-width look-alikes, line breaks collapse
to spaces, a leading ``#`` cannot start a heading, and ``javascript:`` /
``vbscript:`` / ``data:`` lose their colon. Parent-written text is not
rewritten; it is rejected when it contains these characters (btc.analysis).
"""
from __future__ import annotations

import re

_TABLE = str.maketrans({'<': '＜', '>': '＞', '[': '［', ']': '］', '|': '｜', '`': '｀', '\\': '＼', '#': '＃'})
_SCHEMES = re.compile(r'(?i)\b(javascript|vbscript|data)\s*:')


def md_safe(text) -> str:
    text = re.sub(r'[\x00-\x1f\x7f  ]+', ' ', str(text if text is not None else ''))
    text = ' '.join(text.translate(_TABLE).split())
    return _SCHEMES.sub(lambda m: m.group(1) + '：', text)
