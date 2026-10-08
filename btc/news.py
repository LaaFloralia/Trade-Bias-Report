"""BTC news: public RSS headlines, Jev triage (optional, degrades to rules), body checks, price reaction.

- Feeds: five crypto media RSS + SEC / CFTC / Fed official feeds (design 6.1);
  the feed parser is reused from XAU ``scrapers.news_triage``.
- Three steps across the process boundary (btc.headline_triage):
  the sandboxed collect worker gathers headlines (``collect_headline_source``);
  the entry, outside the sandbox, runs the Jev triage on public headlines only
  (same constants and shared daily budget as XAU, P4); the sandboxed
  news-detail worker selects and checks them (``finish_news``). Failure,
  ``LAA_JEV_DISABLED=1`` or timeout fall back to a keyword rule. Method,
  model, latency and input size are always recorded.
- Headlines are unverified leads. Code fetches up to 8 article bodies (90 s in
  total) and records only a hash, length, a short excerpt and title-word
  overlap. A body is fetched only when the feed item's URL is on that feed's
  fixed host (``BODY_HOSTS``: https, default port, no userinfo, exact host,
  no redirects); anything else is ``not_attempted / url_not_allowed`` and is
  never requested. The fetcher's SSRF guard (btc.fetch) also refuses
  non-public addresses and pins the connection to the vetted address; the parent decides ``verification`` but cannot claim a body check
  that code did not perform.
- Price reaction (design 6.3): Binance BTCUSDT closed 1-minute bars around
  the publisher time; baseline is the bar closing before t0; no forward fill.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from html.parser import HTMLParser
import re
import time
from urllib.parse import urlsplit, urlunsplit

from btc import headline_triage as triage
from btc.fetch import FetchError, Fetcher, SourceResult, checked_url
from btc.sources.base import z

UTC = timezone.utc
GENERAL_FEEDS = (
    ('coindesk_rss', 'CoinDesk', 'https://www.coindesk.com/arc/outboundfeeds/rss/'),
    ('theblock_rss', 'The Block', 'https://www.theblock.co/rss.xml'),
    ('cointelegraph_rss', 'Cointelegraph', 'https://cointelegraph.com/rss'),
    ('decrypt_rss', 'Decrypt', 'https://decrypt.co/feed'),
    ('bitcoinmagazine_rss', 'Bitcoin Magazine', 'https://bitcoinmagazine.com/feed'),
)
OFFICIAL_FEEDS = (
    ('sec_rss', 'SEC', 'https://www.sec.gov/news/pressreleases.rss'),
    ('cftc_rss', 'CFTC', 'https://www.cftc.gov/RSS/RSSGP/rssgp.xml'),
    ('fed_rss', 'Federal Reserve', 'https://www.federalreserve.gov/feeds/press_all.xml'),
)
FALLBACK = re.compile(r'\bbitcoin\b|\bBTC\b|\bETF\b|crypto|stablecoin|tether|USDT|USDC|\bSEC\b|\bCFTC\b|'
                      r'\bFed\b|FOMC|Powell|inflation|CPI|payroll|jobs report|Treasur|yield|dollar|hack|exploit|'
                      r'MicroStrategy|\bStrategy\b|Saylor|BlackRock|IBIT|Binance|Coinbase|Kraken', re.I)
MAX_BODIES = 8
BODY_BUDGET_SECONDS = 90
MAX_REACTIONS = 8
OFFICIAL_IDS = {fid for fid, _, _ in OFFICIAL_FEEDS}
# Article bodies: only these hosts, per feed (exact match; add a subdomain here explicitly to allow it).
BODY_HOSTS = {
    'coindesk_rss': ('www.coindesk.com',),
    'theblock_rss': ('www.theblock.co',),
    'cointelegraph_rss': ('cointelegraph.com',),
    'decrypt_rss': ('decrypt.co',),
    'bitcoinmagazine_rss': ('bitcoinmagazine.com',),
    'sec_rss': ('www.sec.gov',),
    'cftc_rss': ('www.cftc.gov',),
    'fed_rss': ('www.federalreserve.gov',),
}


def _feed_parser():
    from scrapers import news_triage as xau  # read-only reuse of the XAU feed parser
    return xau


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip('/'), '', ''))


def title_key(title: str) -> str:
    return hashlib.sha256(re.sub(r'[^a-z0-9]+', ' ', title.lower()).strip().encode()).hexdigest()[:16]


def collect_headlines(f: Fetcher, start: datetime, now: datetime) -> tuple[list[dict], list[dict]]:
    xau = _feed_parser()
    seen_url, seen_title, candidates, feeds = set(), set(), [], []
    for official, group in ((False, GENERAL_FEEDS), (True, OFFICIAL_FEEDS)):
        for source_id, publisher, url in group:
            record = {'source_id': source_id, 'publisher': publisher, 'official': official, 'status': 'unavailable',
                      'items': 0, 'error_kind': None, 'retrieved_at': None}
            try:
                r = f.get(url, timeout=15)
                items = xau.parse_feed(r.body, publisher, start, now)
                record.update(status='ok', retrieved_at=z(datetime.fromisoformat(r.retrieved_at)), raw_sha256=r.raw_sha256)
            except FetchError as error:
                record['error_kind'] = error.kind
                feeds.append(record)
                continue
            except Exception:  # noqa: BLE001 - parser/XML failures become a fixed label
                record['error_kind'] = 'parse_error'
                feeds.append(record)
                continue
            fresh = 0
            for item in items:
                key_url, key_title = canonical_url(item['url']), title_key(item['title'])
                if key_url in seen_url or key_title in seen_title:
                    continue
                seen_url.add(key_url)
                seen_title.add(key_title)
                fresh += 1
                candidates.append({'source_id': source_id, 'publisher': publisher, 'official': official,
                                   'title': item['title'], 'url': canonical_url(item['url']),
                                   'published_at': z(datetime.fromisoformat(item['published'])),
                                   'first_seen_at': record['retrieved_at'], 'excerpt': item.get('text', '')[:300],
                                   'feed_raw_sha256': record.get('raw_sha256')})
            record['items'] = fresh
            feeds.append(record)
    candidates.sort(key=lambda c: c['published_at'], reverse=True)
    for index, item in enumerate(candidates[:200]):
        item['id'] = f'n{index + 1:03d}'
    return candidates[:200], feeds


def select(candidates: list[dict], scores: dict | None) -> list[dict]:
    if scores is None:
        pool = [dict(c, p=None) for c in candidates if FALLBACK.search(c['title'] + ' ' + c.get('excerpt', ''))]
        return sorted(pool, key=lambda c: c['published_at'], reverse=True)[:triage.KEEP_MAX]
    ranked = sorted((dict(c, p=scores.get(c['id'])) for c in candidates),
                    key=lambda c: (c['p'] or 0, c['published_at']), reverse=True)
    kept = [c for c in ranked if (c['p'] or 0) >= triage.THRESHOLD]
    if len(kept) < triage.KEEP_MIN:
        kept = ranked[:triage.KEEP_MIN]
    return kept[:triage.KEEP_MAX]


class _ArticleText(HTMLParser):
    SKIP = {'script', 'style', 'noscript', 'svg', 'nav', 'footer', 'header', 'form'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip, self.parts = 0, []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.parts.append(' '.join(data.split()))


def body_url_allowed(url, source_id: str) -> bool:
    """https, default port, no userinfo, DNS name equal to one of the feed's fixed hosts."""
    try:
        host = checked_url(str(url))
    except FetchError:
        return False
    return host in BODY_HOSTS.get(source_id, ())


def body_check(f: Fetcher, item: dict) -> dict:
    """Fetch the article on the feed's own host; keep hash/length/excerpt only."""
    if not body_url_allowed(item.get('url'), item.get('source_id', '')):
        return {'status': 'not_attempted', 'error_kind': 'url_not_allowed'}
    host = urlsplit(item['url']).hostname or ''
    try:
        r = f.get(item['url'], timeout=15, allow_redirects=False)
        parser = _ArticleText()
        parser.feed(r.text())
        text = ' '.join(parser.parts)
    except FetchError as error:
        return {'status': 'unavailable', 'error_kind': error.kind}
    except Exception:  # noqa: BLE001
        return {'status': 'unavailable', 'error_kind': 'parse_error'}
    words = [w for w in re.findall(r'[a-z0-9]{4,}', item['title'].lower())]
    found = sum(1 for w in words if w in text.lower())
    overlap = round(found / len(words), 2) if words else 0.0
    if len(text) < 400 or overlap < 0.5:
        return {'status': 'unavailable', 'error_kind': 'invalid_payload', 'host': host, 'text_chars': len(text),
                'title_overlap': overlap}
    start = max(0, text.lower().find(words[0]) if words else 0)
    return {'status': 'retrieved', 'host': host, 'text_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'text_chars': len(text), 'title_overlap': overlap, 'excerpt': text[start:start + 280],
            'retrieved_at': z(datetime.fromisoformat(r.retrieved_at))}


def reaction(f: Fetcher, published_at: str, now: datetime, others: list[str]) -> dict:
    """Design 6.3 on Binance BTCUSDT 1 m closed bars (USDT, not USD)."""
    t_event = datetime.fromisoformat(published_at)
    t0 = t_event.replace(second=0, microsecond=0)
    base_open = t0 - timedelta(minutes=1)
    end1 = t0 + timedelta(minutes=1) if t_event == t0 else t0 + timedelta(minutes=2)  # bar closing at ceil(t+60s)
    end5 = t0 + timedelta(minutes=5) if t_event == t0 else t0 + timedelta(minutes=6)
    before_open = base_open - timedelta(minutes=5)
    out = {'status': 'missing', 'market_id': 'binance:BTCUSDT:1m', 'reaction_basis': 'publisher_time',
           'event_at': z(t_event), 'baseline_at': z(t0), 'return_before_5m_bps': None, 'return_after_1m_bps': None,
           'return_after_5m_bps': None, 'timing_error_bound_seconds': 60, 'confounded': False,
           'confound_event_ids': [], 'fact_ids': [], 'note': 'BTCUSDT基準（USDT建て）。公表時刻をまたぐ分足の近似で、因果を示さない。'}
    if end5 > now:
        out['note'] += ' 公表後5分の確定足が未形成。'
        return out
    try:
        r = f.get('https://api.binance.com/api/v3/klines', params={
            'symbol': 'BTCUSDT', 'interval': '1m', 'startTime': int(before_open.timestamp() * 1000), 'limit': 14})
        rows = r.json()
        out['raw_sha256'] = r.raw_sha256
        now_ms = int(now.timestamp() * 1000)
        bars: dict[int, float] = {}
        duplicated = set()
        for row in rows:
            open_ms, close, close_ms = int(row[0]), float(row[4]), int(row[6])
            if close_ms >= now_ms or close_ms != open_ms + 59_999:
                continue  # not a closed 1 m bar
            if open_ms in bars and bars[open_ms] != close:
                duplicated.add(open_ms)  # same minute twice with different closes: unusable
            bars[open_ms] = close
        for open_ms in duplicated:
            bars.pop(open_ms)
    except (FetchError, ValueError, TypeError, IndexError):
        out['note'] += ' 分足を取得できず。'
        return out

    def minutes(first: datetime, last: datetime) -> list[int]:
        """Open times (ms) of every 1 m bar from ``first`` to ``last`` inclusive."""
        count = int((last - first).total_seconds() // 60) + 1
        return [int((first + timedelta(minutes=k)).timestamp() * 1000) for k in range(count)]

    def segment(first: datetime, last: datetime):
        """Close-to-close return over contiguous confirmed bars only (design 6.3); no forward fill."""
        need = minutes(first, last)
        gaps = [m for m in need if m not in bars]
        if gaps:
            return None, gaps
        return round(10000 * (bars[need[-1]] / bars[need[0]] - 1), 1), []

    before, gap_b = segment(before_open, base_open)
    after1, gap_1 = segment(base_open, end1 - timedelta(minutes=1))
    after5, gap_5 = segment(base_open, end5 - timedelta(minutes=1))
    out.update(return_before_5m_bps=before, return_after_1m_bps=after1, return_after_5m_bps=after5)
    used = minutes(before_open, end5 - timedelta(minutes=1))
    # The bars used are kept with the raw hash so every bp value can be recomputed from the saved sources.
    out['bars'] = [[z(datetime.fromtimestamp(m / 1000, UTC)), bars[m]] for m in used if m in bars]
    out['bars_required'] = len(used)
    out['bars_missing'] = [z(datetime.fromtimestamp(m / 1000, UTC)) for m in sorted(set(gap_b + gap_1 + gap_5))]
    out['bars_duplicated'] = [z(datetime.fromtimestamp(m / 1000, UTC)) for m in sorted(duplicated)]
    values = [before, after1, after5]
    out['status'] = 'ok' if None not in values else 'partial' if any(v is not None for v in values) else 'missing'
    if out['bars_missing']:
        out['note'] += f' 必要な1分足{len(used)}本のうち{len(out["bars_missing"])}本が欠落・未確定（欠けた区間は算出しない）。'
    out['confounded'] = any(abs((datetime.fromisoformat(o) - t_event).total_seconds()) <= 900 for o in others)
    return out


def collect_headline_source(f: Fetcher, *, now: datetime, lookback_hours: float) -> SourceResult:
    """Context phase (collect worker): feeds and de-duplicated candidates only; no triage, bodies or reactions."""
    started = time.monotonic()
    start = now - timedelta(hours=lookback_hours)
    candidates, feeds = collect_headlines(f, start, now)
    ok_general = sum(1 for x in feeds if x['status'] == 'ok' and not x['official'])
    ok_official = sum(1 for x in feeds if x['status'] == 'ok' and x['official'])
    status = 'ok' if ok_general >= 2 and ok_official >= 2 else ('partial' if ok_general + ok_official else 'unavailable')
    if status == 'unavailable':
        raise FetchError('connection')
    values = {'window_start': z(start), 'window_end': z(now), 'lookback_hours': lookback_hours, 'feeds': feeds,
              'candidate_count': len(candidates), 'candidates': candidates,
              'feeds_ok': {'general': ok_general, 'official': ok_official},
              'headline_elapsed_ms': round((time.monotonic() - started) * 1000)}
    retrieved = [x['retrieved_at'] for x in feeds if x.get('retrieved_at')]
    return SourceResult('news', status=status, retrieved_at=max(retrieved), source_url=GENERAL_FEEDS[0][2],
                        values=values, raw_sha256=[x['raw_sha256'] for x in feeds if x.get('raw_sha256')])


def headline_candidates(record: dict | None) -> list[dict]:
    """Candidates of a context-phase news record (empty when the headline source failed)."""
    values = (record or {}).get('values') or {}
    rows = values.get('candidates')
    return rows if record and record.get('status') in ('ok', 'partial') and isinstance(rows, list) else []


def finish_news(f: Fetcher, record: dict, scores: dict | None, selection: dict, *, now: datetime) -> SourceResult:
    """News-detail phase (sandboxed, no secrets): select -> bodies -> reactions -> clusters -> code verification."""
    started = time.monotonic()
    values = dict(record['values'])
    candidates = values.pop('candidates')
    headline_ms = values.pop('headline_elapsed_ms', 0)
    kept = select(candidates, scores)
    # Body checks: official sources first, then most recent; bounded count and time.
    order = sorted(sorted(kept, key=lambda c: c['published_at'], reverse=True), key=lambda c: not c['official'])
    checked = 0
    body_started = time.monotonic()
    for item in order:
        if checked >= MAX_BODIES or time.monotonic() - body_started > BODY_BUDGET_SECONDS or f.deadline.remaining() < 20:
            item['body'] = {'status': 'not_attempted', 'error_kind': 'budget_exhausted'}
            continue
        checked += 1
        item['body'] = body_check(f, item)
    times = [c['published_at'] for c in kept]
    for index, item in enumerate(sorted(kept, key=lambda c: c['published_at'], reverse=True)):
        if index < MAX_REACTIONS and f.deadline.remaining() > 15:
            item['reaction'] = reaction(f, item['published_at'], now, [t for t in times if t != item['published_at']])
        else:
            item['reaction'] = None
    for item in kept:
        item['event_cluster_id'] = 'c-' + title_key(item['title'])
        item['code_verification'] = ('primary_body_retrieved' if item['official'] and item['body'].get('status') == 'retrieved'
                                     else 'secondary_body_retrieved' if item['body'].get('status') == 'retrieved'
                                     else 'headline_only')
    values.update(selection=selection, kept=kept,
                  selection_rule={'threshold': triage.THRESHOLD, 'keep_min': triage.KEEP_MIN,
                                  'keep_max': triage.KEEP_MAX},
                  elapsed_ms=headline_ms + round((time.monotonic() - started) * 1000))
    return SourceResult('news', status=record['status'], retrieved_at=record['retrieved_at'],
                        source_url=record.get('source_url'), values=values, raw_sha256=list(record.get('raw_sha256') or []))
