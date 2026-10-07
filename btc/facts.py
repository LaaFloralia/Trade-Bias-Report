"""Deterministic BTCUSD facts from one frozen collection (design 5; P5, P8).

Every number the report shows is a fact here. Observed facts cite one source
response (``raw_sha256``); derived facts use ``source_id='computed'``,
``raw_sha256=None``, ``input_manifest_sha256`` and ``source_fact_ids``.
Missing values stay ``None`` with a reason; nothing is filled with zero.

``state`` carries code-owned intermediate results (reference price, groups'
inputs, walls, option clusters, events, news) that scoring and the report
read; it never contains parent text.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import math
import re
import statistics
from zoneinfo import ZoneInfo

from btc import SYMBOL
from btc.common import JST, digest, parse_time
from btc.history import History, comparison_stats, describe, describe_window
from btc.text import md_safe

UTC = timezone.utc
NY = ZoneInfo('America/New_York')
SCHEMA_VERSION = 'btc-facts-1.0'
METHOD_VERSION = '1.0'
SAFE_ID = re.compile(r'[^a-z0-9_.:-]+')
QUOTE_STALE = 90
BOOK_STALE = 90
DERIV_STALE = 600
RATIO_STALE = 2 * 3600
OPTIONS_STALE = 900
FGI_STALE = 26 * 3600
STABLE_STALE = 36 * 3600
DOMINANCE_STALE = 1800
MEMPOOL_STALE = 600
REFERENCE_MAX_SPREAD_BPS = 50
QUOTE_MAX_GAP_SECONDS = 10
USDT_DEPEG_BPS = 50
WALL_SHARE = 0.20
FUNDING_MIN_SETTLEMENTS = 42   # 14 days of 8 h settlements (design: 14 days)
RATIO_MIN_BUCKETS = 336        # design 5.2: 1 h, 14 days
DAILY_MIN = 60                 # FGI / stable / DVOL daily series
COT_MIN_WEEKS = 104
BIG_EXPIRY_SHARE = 20.0


# ----------------------------------------------------------------- helpers

def z(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = parse_time(value)
    return value.astimezone(UTC).isoformat().replace('+00:00', 'Z')


def stamp_key(value) -> str:
    if value is None:
        return 'na'
    if isinstance(value, (date,)) and not isinstance(value, datetime):
        return value.strftime('%Y%m%d')
    if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return value.replace('-', '')
    t = parse_time(value) if isinstance(value, str) else value
    return t.astimezone(UTC).strftime('%Y%m%dt%H%M%Sz')


def slug(text: str) -> str:
    return SAFE_ID.sub('_', str(text).lower()).strip('_') or 'na'


def fmt_num(value: float, decimals: int = 0, sign: bool = False) -> str:
    text = f'{value:{"+" if sign else ""},.{decimals}f}'
    return text


def pct(value: float | None, decimals: int = 2, sign: bool = True) -> str:
    return f'{value:{"+" if sign else ""}.{decimals}f}%'


def jst(value) -> str:
    return parse_time(value).astimezone(JST).strftime('%Y-%m-%d %H:%M JST')


def jdate(value: str) -> str:
    return value


def _usable(record: dict | None) -> bool:
    return bool(record) and record.get('status') in ('ok', 'partial', 'stale')


class Facts:
    """Builds the fact list for one edition with the shared provenance fields."""

    def __init__(self, as_of: datetime, manifest_sha: str):
        self.as_of = as_of
        self.manifest = manifest_sha
        self.items: list[dict] = []
        self.index: dict[str, dict] = {}

    def _add(self, fact: dict) -> dict:
        base = fact['fact_id']
        n = 1
        while fact['fact_id'] in self.index:
            n += 1
            fact['fact_id'] = f'{base}.r{n}'
        self.items.append(fact)
        self.index[fact['fact_id']] = fact
        return fact

    def _common(self, *, domain, provider, instrument, metric, window, key, category, value, unit, status,
                missing_reason, display, display_value, observed_at, observation_date, period_start, period_end,
                published_at, retrieved_at, retrieval_started_at, available_at, timestamp_quality,
                scheduled_release_at, stale_after, freshness_rule_id, valid_until, venue, population, coverage,
                method_id, source_id, evidence_kind, source_fact_ids, raw_sha256, first_seen_at=None):
        logical = '.'.join(['btc', slug(domain), slug(provider), slug(instrument), slug(metric), slug(window)])
        fact_id = f'{logical}.{slug(key)}'
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                value, status, missing_reason = None, 'invalid', 'non_finite_value'
        if value is None:
            display_value = None
            if status in ('ok', 'provisional', 'partial', 'stale'):
                status = 'missing'
            missing_reason = missing_reason or 'value_unavailable'
            display = display if display and display != '—' else missing_display(missing_reason)
        display = md_safe(display)  # provider labels (e.g. FGI classification) are external text
        if value is not None and display_value is not None:
            display_value = _display_number(display, display_value)
        reference = observed_at or period_end or retrieved_at
        if valid_until is None and stale_after is not None and reference is not None:
            valid_until = z(parse_time(reference) + timedelta(seconds=stale_after))
        stale = bool(valid_until is not None and parse_time(valid_until) < self.as_of)
        if stale and status in ('ok', 'provisional', 'partial'):
            status = 'stale'
        fact = {
            'fact_id': fact_id, 'logical_id': logical, 'revision': 0, 'asset': SYMBOL, 'category': category,
            'metric': metric, 'source_id': source_id, 'value': value, 'unit': unit, 'status': status,
            'missing_reason': missing_reason if value is None or status not in ('ok', 'provisional') else missing_reason,
            'observed_at': z(observed_at), 'observation_date': observation_date, 'period_start': z(period_start),
            'period_end': z(period_end), 'published_at': z(published_at),
            'first_seen_at': z(first_seen_at or retrieved_at or self.as_of), 'retrieved_at': z(retrieved_at or self.as_of),
            'retrieval_started_at': z(retrieval_started_at or retrieved_at or self.as_of),
            'available_at': z(available_at or retrieved_at or self.as_of), 'timestamp_quality': timestamp_quality,
            'scheduled_release_at': z(scheduled_release_at), 'stale_after_seconds': stale_after,
            'freshness_rule_id': freshness_rule_id, 'stale': stale, 'valid_until': valid_until, 'venue': venue,
            'instrument': instrument, 'population': population, 'coverage': coverage or _coverage(),
            'evidence_kind': evidence_kind, 'method_id': method_id, 'method_version': METHOD_VERSION,
            'source_fact_ids': list(dict.fromkeys(source_fact_ids or [])), 'raw_sha256': raw_sha256,
            'input_manifest_sha256': self.manifest if source_id == 'computed' else None,
            'display': display, 'display_value': display_value if value is not None else None,
        }
        return self._add(fact)

    def observed(self, *, record: dict | None, raw: str | None, domain, provider, instrument, metric, value, unit,
                 display, display_value=None, window='snapshot', key=None, category=None, observed_at=None,
                 observation_date=None, period_start=None, period_end=None, published_at=None, status='ok',
                 missing_reason=None, stale_after=None, freshness_rule_id=None, valid_until=None, venue=None,
                 population='', coverage=None, method_id='observed_value', timestamp_quality=None,
                 scheduled_release_at=None, first_seen_at=None, source_id=None) -> dict:
        record = record or {}
        values = record.get('values') or {}
        retrieved = record.get('retrieved_at')
        quality = timestamp_quality or values.get('timestamp_quality') or 'unknown'
        if quality == 'receipt_interval' and period_start is None and period_end is None:
            period_start, period_end = values.get('retrieval_started_at') or retrieved, retrieved
        if raw is None and status not in ('missing', 'terms_restricted'):
            status, value, missing_reason = 'missing', None, missing_reason or record.get('error_kind') or 'source_unavailable'
        if key is None:
            key = stamp_key(observed_at or observation_date or period_end or retrieved)
        return self._common(
            domain=domain, provider=provider, instrument=instrument, metric=metric, window=window, key=key,
            category=category or domain, value=value, unit=unit, status=status, missing_reason=missing_reason,
            display=display, display_value=value if display_value is None else display_value,
            observed_at=observed_at, observation_date=observation_date, period_start=period_start,
            period_end=period_end, published_at=published_at, retrieved_at=retrieved,
            retrieval_started_at=values.get('retrieval_started_at'), available_at=retrieved,
            timestamp_quality=quality, scheduled_release_at=scheduled_release_at, stale_after=stale_after,
            freshness_rule_id=freshness_rule_id, valid_until=valid_until, venue=venue, population=population,
            coverage=coverage, method_id=method_id, source_id=source_id or record.get('source_id') or 'unknown',
            evidence_kind='observed', source_fact_ids=[], raw_sha256=raw if status not in ('missing', 'terms_restricted') else raw,
            first_seen_at=first_seen_at)

    def derived(self, *, sources: list[dict], domain, provider, instrument, metric, value, unit, display,
                display_value=None, window='snapshot', key=None, category=None, status='ok', missing_reason=None,
                method_id, observed_at=None, observation_date=None, stale_after=None, valid_until=None,
                freshness_rule_id=None, venue=None, population='', coverage=None, evidence_kind='derived',
                timestamp_quality=None) -> dict:
        sources = [s for s in sources if s]
        ids = [s['fact_id'] for s in sources]
        if any(s['value'] is None for s in sources) and value is not None:
            value, missing_reason = None, 'input_missing'
        times = [parse_time(s['available_at']) for s in sources] + [self.as_of]
        upstream = [parse_time(s['valid_until']) for s in sources if s.get('valid_until')]
        if valid_until is None and stale_after is None and upstream:
            valid_until = z(min(upstream))
        if status == 'ok' and any(s['status'] in ('stale', 'partial', 'provisional', 'conflict') for s in sources):
            worst = next(x for x in ('conflict', 'stale', 'partial', 'provisional')
                         if any(s['status'] == x for s in sources))
            status = worst
        quality = timestamp_quality or (sources[0]['timestamp_quality'] if sources else 'unknown')
        if key is None:
            key = stamp_key(observed_at or observation_date or (sources[0]['observed_at'] if sources else None)
                            or (sources[0]['observation_date'] if sources else None)
                            or (sources[0]['retrieved_at'] if sources else self.as_of))
        return self._common(
            domain=domain, provider=provider, instrument=instrument, metric=metric, window=window, key=key,
            category=category or domain, value=value, unit=unit, status=status, missing_reason=missing_reason,
            display=display, display_value=value if display_value is None else display_value,
            observed_at=observed_at, observation_date=observation_date, period_start=None, period_end=None,
            published_at=None, retrieved_at=max(s['retrieved_at'] for s in sources) if sources else None,
            retrieval_started_at=min(s['retrieval_started_at'] for s in sources) if sources else None,
            available_at=max(times), timestamp_quality=quality, scheduled_release_at=None, stale_after=stale_after,
            freshness_rule_id=freshness_rule_id, valid_until=valid_until, venue=venue, population=population,
            coverage=coverage, method_id=method_id, source_id='computed', evidence_kind=evidence_kind,
            source_fact_ids=ids, raw_sha256=None)

    def missing(self, *, domain, provider, instrument, metric, unit, reason, source_id, window='snapshot',
                category=None, status='missing', display=None) -> dict:
        return self._common(
            domain=domain, provider=provider, instrument=instrument, metric=metric, window=window, key='na',
            category=category or domain, value=None, unit=unit, status=status, missing_reason=reason,
            display=display or missing_display(reason), display_value=None, observed_at=None, observation_date=None,
            period_start=None, period_end=None, published_at=None, retrieved_at=None, retrieval_started_at=None,
            available_at=None, timestamp_quality='unknown', scheduled_release_at=None, stale_after=None,
            freshness_rule_id=None, valid_until=None, venue=None, population='', coverage=None,
            method_id='not_observed' if source_id != 'computed' else 'not_computed', source_id=source_id,
            evidence_kind='derived' if source_id == 'computed' else 'observed', source_fact_ids=[], raw_sha256=None)


_NUMBER = re.compile(r'[-+−]?\d+(?:,\d{3})*(?:\.\d+)?')


MISSING_LABELS = {
    'five_day_window_incomplete': '5営業日がそろわない', 'incomplete_row': '全銘柄の値が未確定',
    'provider_total_mismatch': '提供元の合計と不一致', 'known_funds_only': '既知の銘柄のみ',
    'history_insufficient': '履歴不足', 'base_day_missing': '比較日の値がない', 'common_observation_stale': '共通日が古い',
    'no_common_dates': '共通日がない', 'dollar_proxy_missing': 'ドル指数がない', 'treasury_missing': '米国債利回りがない',
    'funding_history_missing': 'Funding履歴がない', 'single_sided_oi_missing': '片側の建玉がない',
    'input_missing': '入力がない', 'inputs_missing': '入力がない', 'bar_missing': '分足がない',
    'source_unavailable': '取得できず', 'credentials_missing': '認証情報なし', 'not_used_by_terms': '規約により未使用',
    'budget_exhausted': '時間切れ', 'timeout': '取得できず（時間切れ）', 'connection': '取得できず（接続）',
    'stale_observation': '観測が古い', 'future_observation': '未来時刻の観測', 'quote_gap': '気配がない',
}


def missing_display(reason) -> str:
    """Japanese label for the report; the reason code stays in missing_reason and the fact table (section 10)."""
    label = MISSING_LABELS.get(str(reason or ''))
    if label is None and str(reason or '').startswith(('http_', 'parse_', 'invalid_', 'empty_', 'missing_', 'tls')):
        label = '取得できず'
    return f'欠測（{label}）' if label else '欠測'


def _display_number(display: str, approx: float) -> float:
    """The number exactly as printed in ``display`` that ``approx`` was formatted into.

    Figures compare item values with the quoted Markdown text digit by digit,
    so the bound value is taken from the printed string, never re-rounded.
    """
    best = None
    for token in _NUMBER.findall(display or ''):
        number = float(token.replace(',', '').replace('−', '-'))
        decimals = len(token.split('.', 1)[1]) if '.' in token else 0
        tolerance = 0.5 * 10 ** -decimals + 1e-9 * max(1.0, abs(approx))
        if abs(number - approx) <= tolerance and (best is None or abs(number - approx) < abs(best - approx)):
            best = number
    return best if best is not None else approx


def fact_text(fact: dict) -> str:
    """The fact as written in prose: display plus the qualifier that must stay beside it.

    Percentiles carry their comparison window and count (P5), walls their 25 bp band and strikes their OI.
    """
    note = (fact.get('coverage') or {}).get('note') or ''
    if fact['value'] is not None and note and fact['method_id'] in (
            'midrank_percentile_excluding_current', 'book_wall_25bp_bucket_2of3', 'strike_put_plus_call_oi'):
        if fact['metric'].endswith('_share'):
            return fact['display']
        return f'{fact["display"]}（{note}）'
    return fact['display']


def _coverage(included=(), expected=(), observed=None, expected_count=None, note='') -> dict:
    return {'included_components': list(included), 'expected_components': list(expected),
            'observed_count': observed, 'expected_count': expected_count, 'note': note}


def _percentile_fact(F: Facts, base: dict, series: list[dict], *, min_count: int, window_days=None,
                     window_count=None, label: str, as_of=None) -> tuple[dict, dict]:
    stats = comparison_stats(series, base['value'], as_of=as_of or base['observed_at'] or base['retrieved_at'],
                             window_days=window_days, window_count=window_count, min_count=min_count,
                             metric=base['logical_id'])
    if stats['status'] in ('ok', 'zero_variance') and stats['percentile'] is not None:
        # Short display (figures draw it next to a label); the window and count stay in coverage.note and are
        # printed beside the value wherever the fact is written out (fact_text).
        value, status, reason = stats['percentile'], 'ok', None
        display = f'百分位 {stats["percentile"]:.1f}'
    else:
        value, status, reason = None, 'warming_up', 'insufficient_history'
        display = describe(stats)
    fact = F.derived(sources=[base], domain=base['category'], provider=base['logical_id'].split('.')[2],
                     instrument=base['instrument'] or 'na', metric=f'{base["metric"]}_percentile',
                     value=value, unit='percentile', display=display, status=status, missing_reason=reason,
                     method_id='midrank_percentile_excluding_current', venue=base['venue'],
                     population=f'{label}（{base["population"]}）', observed_at=base['observed_at'],
                     observation_date=base['observation_date'],
                     coverage=_coverage(observed=stats['count'], expected_count=min_count,
                                        note=describe_window(stats)))
    if value is None:
        fact['status'] = 'warming_up'
    return fact, stats


# ---------------------------------------------------------------- builders

def price_facts(F: Facts, S: dict, state: dict):
    quotes = {}
    spec = {'kraken_ticker': ('kraken', 'btcusd', 'USD'), 'bitstamp_ticker': ('bitstamp', 'btcusd', 'USD'),
            'binance_spot': ('binance', 'btcusdt', 'USDT'), 'kraken_usdt': ('kraken', 'usdtusd', 'USD')}
    for source_id, (venue, instrument, unit) in spec.items():
        rec = S.get(source_id)
        if not _usable(rec):
            quotes[source_id] = F.missing(domain='price', provider=venue, instrument=instrument, metric='mid', unit=unit,
                                          reason=(rec or {}).get('error_kind') or 'source_unavailable', source_id=source_id)
            continue
        v = rec['values']
        mid = v['mid']
        display = f'{mid:.4f} USD' if instrument == 'usdtusd' else f'{mid:,.1f} {unit}'
        quotes[source_id] = F.observed(record=rec, raw=v['raw_parts']['r0'], domain='price', provider=venue,
                                       instrument=instrument, metric='mid', value=mid, unit=unit, display=display,
                                       observed_at=rec.get('observed_at'), stale_after=QUOTE_STALE, venue=venue,
                                       population=f'{venue} {v.get("pair")} best bid/ask mid')
    usdt = quotes['kraken_usdt']
    binance = quotes['binance_spot']
    usd_candidates = [quotes['kraken_ticker'], quotes['bitstamp_ticker']]
    converted = None
    if binance['value'] is not None and usdt['value'] is not None:
        gap = abs((parse_time(binance['retrieved_at']) - parse_time(usdt['retrieved_at'])).total_seconds())
        value = binance['value'] * usdt['value'] if gap <= QUOTE_MAX_GAP_SECONDS else None
        converted = F.derived(sources=[binance, usdt], domain='price', provider='binance', instrument='btcusd_via_usdt',
                              metric='mid_usd', value=value, unit='USD',
                              display=f'{value:,.1f} USD' if value else missing_display('quote_gap'),
                              missing_reason=None if value else 'quote_gap_over_10s',
                              method_id='btcusdt_mid_times_kraken_usdtusd', venue='binance',
                              population='Binance BTCUSDT mid × Kraken USDTUSD mid')
        usd_candidates.append(converted)
    if usdt['value'] is not None:
        dev = round(10000 * (usdt['value'] - 1), 1)
        F.derived(sources=[usdt], domain='price', provider='kraken', instrument='usdtusd', metric='usdt_deviation_bps',
                  value=dev, unit='bp', display=f'{dev:+.1f}bp', method_id='usdt_peg_deviation', venue='kraken')
        state['usdt_depeg'] = abs(dev) > USDT_DEPEG_BPS
    good = [q for q in usd_candidates if q and q['value'] is not None and not q['stale']]
    times = [parse_time(q['retrieved_at']) for q in good]
    reason = None
    if len(good) >= 2 and (max(times) - min(times)).total_seconds() > QUOTE_MAX_GAP_SECONDS:
        good.sort(key=lambda q: q['retrieved_at'])
        reason = 'quote_gap_over_10s'
    if len(good) >= 2 and reason is None:
        values = [q['value'] for q in good]
        ref = statistics.median(values)
        spread = (max(values) - min(values)) / ref * 10000
        status = 'conflict' if spread > REFERENCE_MAX_SPREAD_BPS else 'ok'
        fact = F.derived(sources=good, domain='price', provider='composite', instrument='btcusd', metric='reference_price',
                         value=round(ref, 1), unit='USD', display=f'{ref:,.1f} USD', status=status,
                         method_id='median_of_usd_mids', population='Kraken・Bitstamp BTCUSD、Binance BTCUSDT×USDTUSD の中央値',
                         stale_after=QUOTE_STALE, observed_at=None,
                         coverage=_coverage([q['venue'] for q in good], ['kraken', 'bitstamp', 'binance'], len(good), 3,
                                            f'最大最小差 {spread:.1f}bp'))
        state['reference_price'] = {'fact_id': fact['fact_id'], 'venue_fact_ids': [q['fact_id'] for q in good],
                                    'status': fact['status'], 'spread_bps': round(spread, 1)}
    else:
        fact = F.missing(domain='price', provider='composite', instrument='btcusd', metric='reference_price', unit='USD',
                         reason=reason or 'single_venue', source_id='computed')
        state['reference_price'] = {'fact_id': None, 'venue_fact_ids': [q['fact_id'] for q in good],
                                    'status': 'missing', 'spread_bps': None}
    if converted is not None and quotes['kraken_ticker']['value'] is not None and converted['value']:
        prem = round(10000 * (quotes['kraken_ticker']['value'] / converted['value'] - 1), 1)
        F.derived(sources=[quotes['kraken_ticker'], converted], domain='price', provider='kraken', instrument='btcusd',
                  metric='usd_venue_premium_bps', value=prem, unit='bp', display=f'{prem:+.1f}bp',
                  method_id='usd_venue_vs_offshore_usd', venue='kraken')
    F.observed(record={'source_id': 'coinbase'}, raw=None, domain='price', provider='coinbase', instrument='btcusd',
               metric='coinbase_premium_bps', value=None, unit='bp', display='規約により未使用',
               status='terms_restricted', missing_reason='規約により未使用', key='na', source_id='coinbase')
    state['quotes'] = {k: v['fact_id'] for k, v in quotes.items()}


def book_facts(F: Facts, S: dict, state: dict):
    from btc.sources.market import VENUES, BUCKET_BPS
    walls, depth_ids = [], []
    for source_id, (_, _, _, quote, market) in VENUES.items():
        rec = S.get(source_id)
        venue = source_id.replace('_futures_book', '').replace('_book', '')
        instrument = 'btcusdt_perp' if market == 'linear_perp' else ('btcusd' if quote == 'USD' else 'btcusdt')
        if not _usable(rec):
            F.missing(domain='liquidity', provider=venue, instrument=instrument, metric='depth_usd_100bps_bid',
                      unit=quote, reason=(rec or {}).get('error_kind') or 'source_unavailable', source_id=source_id)
            continue
        snaps = rec['values']['snapshots']
        per_snapshot = []
        for i, snap in enumerate(snaps):
            ids = {}
            last = i == len(snaps) - 1
            bands = (25, 50, 100) if last else (100,)
            for band in bands:
                for side in ('bid', 'ask'):
                    value = round(snap['depth_usd'][f'{side}_{band}'], 0)
                    reach = snap['reach_bps'][side]
                    status = 'ok' if reach >= band else 'partial'
                    fact = F.observed(record=rec, raw=snap['raw_sha256'], domain='liquidity', provider=venue,
                                      instrument=instrument, metric=f'depth_{side}_{band}bps', value=value, unit=quote,
                                      display=(f'{value / 1e6:,.2f}百万{quote}' if status == 'ok' else
                                               f'{value / 1e6:,.2f}百万{quote}以上（板の返却範囲{reach:.0f}bpまで）'),
                                      display_value=round(value / 1e6, 2),
                                      period_start=snap['retrieval_started_at'], period_end=snap['retrieved_at'],
                                      timestamp_quality='receipt_interval', stale_after=BOOK_STALE, venue=venue,
                                      status=status, missing_reason=None if status == 'ok' else 'book_range_short',
                                      method_id='book_depth_within_bps', population=f'{venue} {market} 観測板',
                                      coverage=_coverage(note=f'返却範囲 {reach:.0f}bp'))
                    ids[(side, band)] = fact
            per_snapshot.append(ids)
            if last:
                bid, ask = snap['depth_usd']['bid_100'], snap['depth_usd']['ask_100']
                imb = round((bid - ask) / (bid + ask), 3) if bid + ask > 0 else None
                F.observed(record=rec, raw=snap['raw_sha256'], domain='liquidity', provider=venue, instrument=instrument,
                           metric='book_imbalance_100bps', value=imb, unit='ratio', display=f'{imb:+.3f}' if imb is not None else '—',
                           period_start=snap['retrieval_started_at'], period_end=snap['retrieved_at'],
                           timestamp_quality='receipt_interval', stale_after=BOOK_STALE, venue=venue,
                           method_id='book_imbalance', population=f'{venue} {market} 観測板')
                F.observed(record=rec, raw=snap['raw_sha256'], domain='liquidity', provider=venue,
                                 instrument=instrument, metric='book_mid', value=round(snap['mid'], 2), unit=quote,
                                 display=f'{snap["mid"]:,.1f} {quote}', period_start=snap['retrieval_started_at'],
                                 period_end=snap['retrieved_at'], timestamp_quality='receipt_interval',
                                 stale_after=BOOK_STALE, venue=venue, method_id='book_mid')
                depth_ids += [ids[('bid', 100)]['fact_id'], ids[('ask', 100)]['fact_id']]
        # Walls: same fixed 25 bp bucket, share >= 20 % of that side's 100 bp depth in >= 2 of 3 snapshots.
        anchor = snaps[0]['anchor_mid']
        seen: dict[str, list] = {}
        for i, snap in enumerate(snaps):
            for key, usd in snap['buckets_usd'].items():
                side, index = key.split(':')
                index = int(index)
                low = anchor * (1 + index * BUCKET_BPS / 10000)
                high = anchor * (1 + (index + 1) * BUCKET_BPS / 10000)
                distance = abs(((low + high) / 2) / snap['mid'] - 1) * 10000
                denominator = snap['depth_usd'][f'{side}_100']
                best = snap['best_bid'] if side == 'bid' else snap['best_ask']
                if low <= best < high:
                    continue  # the bucket holding the best quote is always thick; it is not a wall
                if snap['reach_bps'][side] < 100 or distance > 100 or denominator <= 0:
                    # A book that does not reach 100 bp cannot show a share of the 100 bp depth.
                    continue
                share = usd / denominator
                if share >= WALL_SHARE:
                    seen.setdefault(key, []).append((i, share, low, high))
        for key, hits in seen.items():
            if len(hits) < 2:
                continue
            side = key.split(':')[0]
            i, share, low, high = hits[-1]
            center = round((low + high) / 2, 0)
            sources = [per_snapshot[h[0]][(side, 100)] for h in hits]
            label = '買い板' if side == 'bid' else '売り板'
            wall_key = f'{stamp_key(snaps[hits[-1][0]]["retrieved_at"])}_{int(center)}'
            price = F.derived(sources=sources, domain='liquidity', provider=venue, instrument=instrument,
                              metric=f'wall_{side}_price', value=center, unit=quote, key=wall_key,
                              display=f'{center:,.0f} {quote}',
                              method_id='book_wall_25bp_bucket_2of3', venue=venue, evidence_kind='derived',
                              population=f'{venue} {market} 観測板（{label}）',
                              coverage=_coverage(observed=len(hits), expected_count=len(snaps),
                                                 note=f'25bp帯 {low:,.0f}〜{high:,.0f} {quote}・'
                                                      f'{len(hits)}/{len(snaps)}回の観測で片側100bp量の20%以上'))
            share_fact = F.derived(sources=sources, domain='liquidity', provider=venue, instrument=instrument,
                                   metric=f'wall_{side}_share', value=round(share * 100, 1), unit='percent', key=wall_key,
                                   display=f'{share * 100:.1f}%', method_id='book_wall_25bp_bucket_2of3', venue=venue,
                                   population=f'{venue} {market} 観測板（{label}）')
            walls.append({'venue': venue, 'market': market, 'side': side, 'price_fact_id': price['fact_id'],
                          'share_fact_id': share_fact['fact_id'], 'hits': len(hits), 'quote': quote,
                          'center': center, 'low': low, 'high': high, 'share_pct': round(share * 100, 1)})
    walls.sort(key=lambda w: -w['share_pct'])
    state['liquidity'] = {'walls': walls[:8], 'book_fact_ids': depth_ids}


def _interval_hours(values: dict) -> float | None:
    if values.get('funding_interval_hours'):
        return float(values['funding_interval_hours'])
    settled = values.get('funding_settled') or []
    if len(settled) >= 2:
        gap = (parse_time(settled[-1]['t']) - parse_time(settled[-2]['t'])).total_seconds() / 3600
        return gap if gap > 0 else None
    return None


def derivatives_facts(F: Facts, S: dict, state: dict, history: History, as_of: datetime):
    venues = []
    records = []
    for venue in ('binance', 'bybit', 'okx'):
        source_id = f'{venue}_derivatives'
        rec = S.get(source_id)
        instrument = 'btc-usdt-swap' if venue == 'okx' else 'btcusdt'
        if not _usable(rec):
            F.missing(domain='derivatives', provider=venue, instrument=instrument, metric='funding_settled', unit='rate',
                      reason=(rec or {}).get('error_kind') or 'source_unavailable', source_id=source_id)
            continue
        v = rec['values']
        parts = v['raw_parts']
        settled = v.get('funding_settled') or []
        interval = _interval_hours(v)
        entry = {'venue': venue}
        if settled and interval:
            last = settled[-1]
            valid = z(parse_time(v['next_funding_time']) + timedelta(minutes=10))
            fs = F.observed(record=rec, raw=parts['funding_history'], domain='derivatives', provider=venue,
                            instrument=instrument, metric='funding_settled', value=last['rate'], unit='rate',
                            display=f'{last["rate"] * 100:.4f}%', display_value=round(last['rate'] * 100, 4),
                            observed_at=last['t'], valid_until=valid, venue=venue,
                            freshness_rule_id='next_settlement_plus_10m',
                            method_id='settled_funding_history', population=f'{venue} {instrument} 確定Funding')
            eq = last['rate'] * 8 / interval
            f8 = F.derived(sources=[fs], domain='derivatives', provider=venue, instrument=instrument,
                           metric='funding_equiv_8h', value=eq, unit='rate', display=f'{eq * 100:.4f}%',
                           display_value=round(eq * 100, 4), method_id='funding_rate_x8_over_interval', venue=venue,
                           observed_at=last['t'], coverage=_coverage(note=f'決済間隔 {interval:g}時間'))
            t_end = parse_time(last['t'])
            window = [s for s in settled if t_end - timedelta(hours=24) < parse_time(s['t']) <= t_end]
            expected = round(24 / interval)
            total = sum(s['rate'] for s in window)
            F.derived(sources=[fs], domain='derivatives', provider=venue, instrument=instrument,
                      metric='funding_realized_24h', value=total, unit='rate', display=f'{total * 100:.4f}%',
                      display_value=round(total * 100, 4), window='24h', method_id='sum_settled_24h', venue=venue,
                      status='ok' if len(window) == expected else 'partial', observed_at=last['t'],
                      coverage=_coverage(observed=len(window), expected_count=expected))
            series = [{'observed_at': s['t'], 'value': s['rate'] * 8 / interval} for s in settled[:-1]]
            perc, _ = _percentile_fact(F, f8, series, min_count=FUNDING_MIN_SETTLEMENTS, window_days=30,
                                       label='同一venueの確定Funding(8h換算)・過去30日')
            entry.update(funding_fact=f8, funding_percentile=perc)
            records += [{'source': venue, 'instrument': instrument, 'metric': 'funding_rate', 'observed_at': s['t'],
                         'value': s['rate'], 'retrieved_at': rec['retrieved_at']} for s in settled]
        else:
            F.missing(domain='derivatives', provider=venue, instrument=instrument, metric='funding_settled', unit='rate',
                      reason='funding_history_missing', source_id=source_id)
        current_part = {'binance': 'premium', 'bybit': 'ticker', 'okx': 'funding_current'}[venue]
        F.observed(record=rec, raw=parts[current_part], domain='derivatives', provider=venue, instrument=instrument,
                   metric='funding_current_predicted', value=v.get('current_funding_rate'), unit='rate',
                   display=f'{v["current_funding_rate"] * 100:.4f}%' if v.get('current_funding_rate') is not None else '—',
                   display_value=round(v['current_funding_rate'] * 100, 4) if v.get('current_funding_rate') is not None else None,
                   observed_at=v.get('mark_time'), stale_after=DERIV_STALE, venue=venue,
                   method_id='current_or_predicted_funding', population=f'{venue} 現在/予定Funding（確定値ではない）')
        oi_part = {'binance': 'oi', 'bybit': 'ticker', 'okx': 'oi'}[venue]
        oi = F.observed(record=rec, raw=parts[oi_part], domain='derivatives', provider=venue, instrument=instrument,
                        metric='oi_btc', value=v.get('oi_btc'), unit='BTC',
                        display=f'{v["oi_btc"]:,.0f} BTC' if v.get('oi_btc') is not None else '—',
                        observed_at=v.get('oi_time'), stale_after=DERIV_STALE, venue=venue,
                        missing_reason=None if v.get('oi_btc') is not None else 'single_sided_oi_missing',
                        method_id='single_sided_open_interest', population=f'{venue} {instrument} 片側OI')
        entry['oi_fact'] = oi
        if oi['value'] is not None:
            oi_usd = oi['value'] * v['mark_price']
            entry['oi_usd_fact'] = F.derived(sources=[oi], domain='derivatives', provider=venue, instrument=instrument,
                                             metric='oi_usd', value=round(oi_usd, 0), unit='USD',
                                             display=f'{oi_usd / 1e9:,.2f}十億USD', display_value=round(oi_usd / 1e9, 2),
                                             method_id='oi_btc_times_mark', venue=venue, observed_at=v.get('oi_time'))
            records.append({'source': venue, 'instrument': instrument, 'metric': 'oi_btc', 'observed_at': v['oi_time'],
                            'value': oi['value'], 'retrieved_at': rec['retrieved_at']})
        # 24 h OI change: provider hourly series (one series at a time), else own history.
        hourly = v.get('oi_hourly') or []
        change = None
        if len(hourly) >= 25:
            latest, base = hourly[-1], next((h for h in hourly if parse_time(h['t']) == parse_time(hourly[-1]['t']) - timedelta(hours=24)), None)
            if base and base['oi_btc'] > 0:
                change = 100 * (latest['oi_btc'] / base['oi_btc'] - 1)
                entry['oi_change_fact'] = F.observed(
                    record=rec, raw=parts['oi_hourly'], domain='derivatives', provider=venue, instrument=instrument,
                    metric='oi_change_24h', value=round(change, 2), unit='percent', display=f'{change:+.2f}%',
                    window='24h', observed_at=latest['t'], stale_after=RATIO_STALE, venue=venue,
                    method_id='provider_hourly_series_change',
                    population=f'{venue} 提供元の1時間OI系列（{v.get("oi_hourly_convention", "openInterestHist")}）')
        if change is None and oi['value'] is not None:
            past = [r for r in history.series(venue, instrument, 'oi_btc')
                    if abs((parse_time(r['observed_at']) - (parse_time(oi['observed_at']) - timedelta(hours=24))).total_seconds()) <= 5400]
            if past:
                base = past[-1]['value']
                change = 100 * (oi['value'] / base - 1)
                entry['oi_change_fact'] = F.derived(
                    sources=[oi], domain='derivatives', provider=venue, instrument=instrument, metric='oi_change_24h',
                    value=round(change, 2), unit='percent', display=f'{change:+.2f}%', window='24h',
                    method_id='own_history_change_24h', venue=venue,
                    coverage=_coverage(note=f'自前履歴（1日2回の取得）・比較点 {past[-1]["observed_at"]}'))
            else:
                entry['oi_change_fact'] = F.missing(domain='derivatives', provider=venue, instrument=instrument,
                                                    metric='oi_change_24h', unit='percent', window='24h',
                                                    reason='history_insufficient', source_id='computed',
                                                    status='warming_up', display='履歴不足（24時間前の自前観測なし）')
        venues.append(entry)
    # OI-weighted 8 h funding over venues that have both.
    pairs = [(e['funding_fact'], e['oi_usd_fact']) for e in venues if e.get('funding_fact') and e.get('oi_usd_fact')]
    if len(pairs) >= 2:
        w = sum(o['value'] for _, o in pairs)
        val = sum(f['value'] * o['value'] for f, o in pairs) / w
        F.derived(sources=[x for p in pairs for x in p], domain='derivatives', provider='composite', instrument='btcusdt_perp',
                  metric='funding_weighted_8h', value=val, unit='rate', display=f'{val * 100:.4f}%',
                  display_value=round(val * 100, 4), method_id='oi_weighted_funding_8h',
                  population='+'.join(f['venue'] for f, _ in pairs) + ' 線形BTC無期限')
    history.append(records)
    state['derivatives'] = {
        'venues': [{'venue': e['venue'], 'funding_fact_id': e.get('funding_fact', {}).get('fact_id') if e.get('funding_fact') else None,
                    'funding_percentile_fact_id': e['funding_percentile']['fact_id'] if e.get('funding_percentile') else None,
                    'oi_fact_id': e['oi_fact']['fact_id'], 'oi_change_fact_id': e['oi_change_fact']['fact_id'] if e.get('oi_change_fact') else None}
                   for e in venues],
        'current_ok': sum(1 for e in venues if e.get('funding_fact') and e['oi_fact']['value'] is not None),
    }


def ratio_facts(F: Facts, S: dict, state: dict):
    out = {}
    for source_id, metric, label in (('binance_ls', 'account_long_share', 'Binance全口座'),
                                      ('binance_top_position', 'top_position_long_share', 'Binance上位トレーダー建玉')):
        rec = S.get(source_id)
        if not _usable(rec) or not rec['values'].get('series'):
            out[metric] = F.missing(domain='positioning', provider='binance', instrument='btcusdt', metric=metric,
                                    unit='ratio', reason=(rec or {}).get('error_kind') or 'source_unavailable',
                                    source_id=source_id)
            continue
        series = rec['values']['series']
        last = series[-1]
        fact = F.observed(record=rec, raw=rec['values']['raw_parts']['r0'], domain='positioning', provider='binance',
                          instrument='btcusdt', metric=metric, value=last['long_share'], unit='ratio',
                          display=f'ロング{last["long_share"] * 100:.1f}%', display_value=round(last['long_share'] * 100, 1),
                          window='1h', observed_at=last['t'], stale_after=RATIO_STALE, venue='binance',
                          population=label, method_id='provider_long_account_share')
        perc, _ = _percentile_fact(F, fact, [{'observed_at': s['t'], 'value': s['long_share']} for s in series[:-1]],
                                   min_count=RATIO_MIN_BUCKETS, window_days=30, label=f'{label}・1時間足・過去30日')
        out[metric] = fact
        out[f'{metric}_percentile'] = perc
    rec = S.get('binance_taker')
    if _usable(rec) and rec['values'].get('series'):
        series = rec['values']['series']
        buy, sell = sum(s['buy'] for s in series[-24:]), sum(s['sell'] for s in series[-24:])
        share = buy / (buy + sell) if buy + sell > 0 else None
        out['taker'] = F.observed(record=rec, raw=rec['values']['raw_parts']['r0'], domain='positioning', provider='binance',
                                  instrument='btcusdt', metric='taker_buy_share', value=share, unit='ratio', window='24h',
                                  display=f'買い{share * 100:.1f}%' if share is not None else '—',
                                  display_value=round(share * 100, 1) if share is not None else None,
                                  observed_at=series[-1]['t'], stale_after=RATIO_STALE, venue='binance',
                                  population='Binance BTCUSDT 無期限 テイカー売買量', method_id='sum_volume_then_share',
                                  coverage=_coverage(observed=len(series[-24:]), expected_count=24))
    state['ratios'] = {k: v['fact_id'] for k, v in out.items()}


def options_facts(F: Facts, S: dict, state: dict, history: History, as_of: datetime):
    rec = S.get('deribit_options')
    state['options'] = {'status': 'missing', 'expiries': [], 'clusters': [], 'skew': {}, 'events': []}
    if not _usable(rec):
        F.missing(domain='options', provider='deribit', instrument='btc_options', metric='oi_btc_total', unit='BTC',
                  reason=(rec or {}).get('error_kind') or 'source_unavailable', source_id='deribit_options')
        return
    v = rec['values']
    parts = v['raw_parts']
    table = [r for r in v['table'] if parse_time(r['expiry']) > as_of]
    calls = sum(r['oi_btc'] for r in table if r['type'] == 'call')
    puts = sum(r['oi_btc'] for r in table if r['type'] == 'put')
    total = calls + puts
    observed = rec.get('observed_at')
    pop = 'Deribit BTC inverse options のみ'
    tot = F.observed(record=rec, raw=parts['book_summary'], domain='options', provider='deribit', instrument='btc_options',
                     metric='oi_btc_total', value=round(total, 1), unit='BTC', display=f'{total:,.0f} BTC',
                     observed_at=observed, stale_after=OPTIONS_STALE, venue='deribit', population=pop,
                     method_id='sum_open_interest_unexpired')
    pcr = F.observed(record=rec, raw=parts['book_summary'], domain='options', provider='deribit', instrument='btc_options',
                     metric='pcr_oi_all', value=round(puts / calls, 3) if calls > 0 else None, unit='ratio',
                     display=f'{puts / calls:.2f}' if calls > 0 else '—', observed_at=observed, stale_after=OPTIONS_STALE,
                     venue='deribit', population=pop, method_id='put_oi_over_call_oi',
                     missing_reason=None if calls > 0 else 'call_oi_zero')
    by_expiry: dict[str, list] = {}
    for r in table:
        by_expiry.setdefault(r['expiry'], []).append(r)
    expiries = sorted(by_expiry)
    chosen = []
    if expiries:
        chosen.append(expiries[0])
    week = [e for e in expiries if parse_time(e) <= as_of + timedelta(days=7)]
    if week:
        biggest = max(week, key=lambda e: sum(r['oi_btc'] for r in by_expiry[e]))
        if biggest not in chosen:
            chosen.append(biggest)
    for expiry in chosen:
        rows = by_expiry[expiry]
        oi = sum(r['oi_btc'] for r in rows)
        e_calls = sum(r['oi_btc'] for r in rows if r['type'] == 'call')
        e_puts = oi - e_calls
        label = jst(expiry)
        key = stamp_key(expiry)
        share = F.derived(sources=[tot], domain='options', provider='deribit', instrument=f'expiry_{key}',
                          metric='expiry_share', value=round(100 * oi / total, 1) if total else None, unit='percent',
                          display=f'{100 * oi / total:.1f}%（{label}満期）' if total else '—',
                          method_id='expiry_oi_over_total', venue='deribit', population=pop, key=key)
        e_pcr = F.derived(sources=[tot], domain='options', provider='deribit', instrument=f'expiry_{key}',
                          metric='pcr_oi', value=round(e_puts / e_calls, 3) if e_calls > 0 else None, unit='ratio',
                          display=f'{e_puts / e_calls:.2f}' if e_calls > 0 else '—', method_id='put_oi_over_call_oi',
                          venue='deribit', population=pop, key=key, missing_reason=None if e_calls > 0 else 'call_oi_zero')
        strikes = sorted({r['strike'] for r in rows})
        pain = []
        for k in strikes:
            payout = sum(r['oi_btc'] * max(k - r['strike'], 0) for r in rows if r['type'] == 'call') + \
                sum(r['oi_btc'] * max(r['strike'] - k, 0) for r in rows if r['type'] == 'put')
            pain.append((payout, k))
        best = min(p for p, _ in pain) if pain else None
        winners = [k for p, k in pain if p == best]
        mp = F.derived(sources=[tot], domain='options', provider='deribit', instrument=f'expiry_{key}',
                       metric='max_pain_usd_convention', value=winners[0] if winners else None, unit='USD',
                       display=f'{winners[0]:,.0f} USD' if winners else '—', key=key, venue='deribit', population=pop,
                       status='ok' if len(winners) == 1 else 'partial', method_id='max_pain_usd_payout_min',
                       coverage=_coverage(note='同点候補: ' + ', '.join(f'{w:,.0f}' for w in winners) if len(winners) > 1 else ''))
        per_strike = {}
        for r in rows:
            per_strike[r['strike']] = per_strike.get(r['strike'], 0) + r['oi_btc']
        top = sorted(per_strike.items(), key=lambda x: -x[1])[:3]
        clusters = []
        for strike, value in top:
            sh = 100 * value / oi if oi else 0
            sf = F.derived(sources=[tot], domain='options', provider='deribit', instrument=f'expiry_{key}',
                           metric='strike_oi', value=strike, unit='USD',
                           display=f'{strike:,.0f} USD', key=f'{key}_{int(strike)}', venue='deribit', population=pop,
                           method_id='strike_put_plus_call_oi',
                           coverage=_coverage(note=f'建玉{value:,.0f} BTC・満期内{sh:.1f}%'))
            clusters.append({'fact_id': sf['fact_id'], 'strike': strike, 'oi_btc': round(value, 1), 'share_pct': round(sh, 1)})
        info = {'expiry': expiry, 'label': label, 'oi_btc': round(oi, 1), 'share_fact_id': share['fact_id'],
                'share_pct': share['value'], 'pcr_fact_id': e_pcr['fact_id'], 'max_pain_fact_id': mp['fact_id'],
                'max_pain': mp['value'], 'top_strikes': clusters}
        state['options']['expiries'].append(info)
        state['options']['clusters'] += clusters
        if share['value'] is not None and share['value'] >= BIG_EXPIRY_SHARE and parse_time(expiry) <= as_of + timedelta(hours=24):
            day = parse_time(expiry).astimezone(JST)
            start = day.replace(hour=16, minute=30, second=0, microsecond=0)
            state['options']['events'].append({
                'id': f'deribit.expiry.{key}', 'name': f'Deribit大型満期（建玉シェア{share["value"]:.1f}%）',
                'category': 'derivatives_expiry', 'start_at': z(parse_time(expiry)), 'end_at': None, 'timezone': 'UTC',
                'importance': 'high', 'status': 'confirmed', 'source_id': 'deribit_options',
                'source_url': 'https://www.deribit.com/api/v2/public/get_instruments', 'published_at': None,
                'verified_at': rec['retrieved_at'],
                'stop_window': {'start_at': z(start), 'end_at': z(start + timedelta(hours=1)),
                                'event_ids': [f'deribit.expiry.{key}'], 'rule_id': 'deribit_big_expiry_1630_1730_jst'}})
    # DVOL
    latest = v.get('dvol_latest')
    if latest:
        dv = F.observed(record=rec, raw=parts['dvol_minute'], domain='options', provider='deribit', instrument='btc_dvol',
                        metric='dvol', value=latest['close'], unit='index', display=f'{latest["close"]:.2f}',
                        observed_at=latest['t'], stale_after=OPTIONS_STALE, venue='deribit',
                        population='Deribit DVOL（年率IV指数）', method_id='latest_closed_1m_bar')
        daily = [{'observed_at': d['t'], 'value': d['close']} for d in v.get('dvol_daily', [])]
        _percentile_fact(F, dv, daily, min_count=DAILY_MIN, window_days=90, label='DVOL日次終値・過去90日')
    # 25-delta skew per target (design 5.3): bracket, linear interpolation, quote quality gates.
    for target in (7, 30):
        quotes = [q for q in v.get('skew_quotes', []) if q['target_dte'] == target]
        result = _skew(quotes)
        name = f'skew_25d_{target}d'
        q_facts = []
        for q in quotes:
            q_facts.append(F.observed(record=rec, raw=q['raw'], domain='options', provider='deribit',
                                      instrument=slug(q['name']), metric='option_mark_iv', value=q['mark_iv'], unit='percent',
                                      display=f'{q["mark_iv"]:.2f}%' if q['mark_iv'] is not None else '—',
                                      observed_at=q['timestamp'], stale_after=OPTIONS_STALE, venue='deribit',
                                      population=f'{q["name"]} delta {q["delta"]}', method_id='ticker_mark_iv'))
        if result['value'] is not None:
            fact = F.derived(sources=q_facts, domain='options', provider='deribit', instrument=f'target_{target}d',
                             metric='skew_25d_pp', value=round(result['value'], 2), unit='pp',
                             display=f'{result["value"]:+.2f}pp（満期まで{result["dte"]:.1f}日）', window=f'{target}d',
                             method_id='skew_25d_bracket_linear', venue='deribit', population=pop,
                             coverage=_coverage(note=result['note']))
        else:
            fact = F.missing(domain='options', provider='deribit', instrument=f'target_{target}d', metric='skew_25d_pp',
                             unit='pp', window=f'{target}d', reason=result['reason'], source_id='computed')
        state['options']['skew'][name] = fact['fact_id']
        if fact['value'] is not None:
            series = history.series('deribit', f'skew_{target}d', 'skew_25d_pp')
            perc, _ = _percentile_fact(F, fact, series, min_count=DAILY_MIN, window_days=180, label='自前履歴のスキュー',
                                       as_of=fact['retrieved_at'])
            state['options']['skew'][name + '_percentile'] = perc['fact_id']
            history.append([{'source': 'deribit', 'instrument': f'skew_{target}d', 'metric': 'skew_25d_pp',
                             'observed_at': fact['retrieved_at'], 'value': fact['value']}])
    state['options'].update(status='ok', total_fact_id=tot['fact_id'], pcr_fact_id=pcr['fact_id'],
                            scope='Deribit BTC inverse options のみ')


def _skew(quotes: list) -> dict:
    out = {'value': None, 'reason': 'no_expiry_in_target_window', 'note': '', 'dte': None}
    if not quotes:
        return out
    times = [parse_time(q['timestamp']) for q in quotes]
    if (max(times) - min(times)).total_seconds() > 60:
        out['reason'] = 'quote_time_gap_over_60s'
        return out
    ivs = {}
    for kind, goal in (('put', -0.25), ('call', 0.25)):
        rows = [q for q in quotes if q['type'] == kind and q['delta'] is not None and q['mark_iv'] is not None]
        good = []
        for q in rows:
            bid, ask = q['bid'], q['ask']
            if not bid or bid <= 0 or ask is None or ask < bid or not q['oi_btc'] or q['oi_btc'] <= 0 or q['dte'] < 2:
                continue
            if (ask - bid) / ((ask + bid) / 2) > 0.5:
                continue
            good.append(q)
        below = [q for q in good if q['delta'] <= goal]
        above = [q for q in good if q['delta'] > goal]
        if not below or not above:
            out['reason'] = f'no_valid_{kind}_bracket'
            return out
        a = max(below, key=lambda q: q['delta'])
        b = min(above, key=lambda q: q['delta'])
        if b['delta'] == a['delta']:
            out['reason'] = 'degenerate_bracket'
            return out
        w = (goal - a['delta']) / (b['delta'] - a['delta'])
        ivs[kind] = a['mark_iv'] + w * (b['mark_iv'] - a['mark_iv'])
    out.update(value=ivs['put'] - ivs['call'], reason=None, dte=quotes[0]['dte'],
               note=f'put25Δ {ivs["put"]:.2f}% − call25Δ {ivs["call"]:.2f}%、満期 {jst(quotes[0]["expiry"])}')
    return out


def _us_business_day_before(day: date) -> date:
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def expected_etf_date(as_of: datetime) -> date:
    """Last completed US session (16:00 ET). US holidays are not modelled (noted in limitations)."""
    ny = as_of.astimezone(NY)
    day = ny.date()
    if day.weekday() < 5 and (ny.hour, ny.minute) >= (16, 0):
        return day
    return _us_business_day_before(day)


def business_lag(latest: date, expected: date) -> int:
    lag, day = 0, expected
    while day > latest:
        day = _us_business_day_before(day)
        lag += 1
    return lag


def etf_facts(F: Facts, S: dict, state: dict, history: History, as_of: datetime):
    rec = S.get('farside')
    expected = expected_etf_date(as_of)
    state['etf'] = {'status': 'missing', 'trade_date': None, 'expected_trade_date': expected.isoformat(),
                    'universe': [], 'flow_fact_ids': [], 'five_day_fact_id': None, 'lag_business_days': None}
    if not _usable(rec):
        F.missing(domain='etf', provider='farside', instrument='us_spot_btc_etf', metric='etf_netflow_usd', unit='USD',
                  reason=(rec or {}).get('error_kind') or 'source_unavailable', source_id='farside', window='1d')
        return
    v = rec['values']
    raw = v['raw_parts']['r0']
    # Rows after the last completed US session (today's row while the session runs) are not results yet:
    # they never become the latest day nor enter the 5-day window.
    rows = [r for r in v['rows'] if r['trade_date'] <= expected.isoformat()][-20:]
    state['etf']['in_progress_rows'] = [r['trade_date'] for r in v['rows'] if r['trade_date'] > expected.isoformat()]
    facts_by_date = {}
    arrival = []
    for row in rows:
        day = row['trade_date']
        if row['conflict']:
            value, status, reason = None, 'conflict', 'provider_total_mismatch'
        elif row['validated_total_musd'] is not None:
            value, status, reason = row['validated_total_musd'] * 1e6, 'provisional', None
        else:
            value, status, reason = None, 'partial', 'incomplete_row'
        prior = [r for r in history.read('farside', 'all', 'etf_row_seen') if r.get('trade_date') == day]
        first_seen = prior[0]['observed_at'] if prior else rec['retrieved_at']
        fact = F.observed(record=rec, raw=raw, domain='etf', provider='farside', instrument='us_spot_btc_etf',
                          metric='etf_netflow_usd', value=value, unit='USD', window='1d', key=day.replace('-', ''),
                          display=f'{value / 1e6:+,.1f}百万USD' if value is not None else missing_display(reason),
                          display_value=round(value / 1e6, 1) if value is not None else None,
                          observation_date=day, status=status, missing_reason=reason, first_seen_at=first_seen,
                          timestamp_quality='source_date', freshness_rule_id='us_business_day_lag',
                          population='Farside 米現物BTC ETF 全銘柄（表ヘッダーの銘柄）',
                          coverage=_coverage(v['universe'], v['universe'], len(row['funds']) - len(row['missing_funds']),
                                             len(row['funds']), 'publication_status=provisional（Farside速報）'),
                          method_id='farside_validated_total')
        if row['known_sum_musd'] is not None and value is None:
            F.observed(record=rec, raw=raw, domain='etf', provider='farside', instrument='us_spot_btc_etf',
                       metric='etf_known_partial_sum_usd', value=row['known_sum_musd'] * 1e6, unit='USD', window='1d',
                       key=day.replace('-', ''), display=f'{row["known_sum_musd"]:+,.1f}百万USD（既知分のみ）',
                       display_value=row['known_sum_musd'], observation_date=day, status='partial',
                       missing_reason='known_funds_only', timestamp_quality='source_date',
                       method_id='farside_known_sum')
        facts_by_date[day] = fact
    latest_day = rows[-1]['trade_date'] if rows else None
    if latest_day:
        lag = business_lag(date.fromisoformat(latest_day), expected)
        for fact in facts_by_date.values():
            if lag >= 2:
                fact['status'] = 'stale' if fact['value'] is not None else fact['status']
                fact['stale'] = True
        # Arrival record for the expected trade date (P8: row_present / full_numeric / changed_since_previous).
        target = next((r for r in rows if r['trade_date'] == expected.isoformat()), None)
        previous = [r for r in history.read('farside', 'all', 'etf_total_seen') if r.get('trade_date') == expected.isoformat()]
        changed = None
        if target is not None and previous:
            changed = 1 if previous[-1].get('value') != target['reported_total_musd'] else 0
        stamp = rec['retrieved_at']
        arrival = [
            {'source': 'farside', 'instrument': 'all', 'metric': 'etf_row_present', 'observed_at': stamp,
             'value': 1 if target else 0, 'trade_date': expected.isoformat()},
            {'source': 'farside', 'instrument': 'all', 'metric': 'etf_full_numeric', 'observed_at': stamp,
             'value': 1 if target and target['table_complete'] else 0, 'trade_date': expected.isoformat()},
            {'source': 'farside', 'instrument': 'all', 'metric': 'etf_changed_since_previous', 'observed_at': stamp,
             'value': changed, 'trade_date': expected.isoformat()},
        ]
        if target is not None:
            arrival.append({'source': 'farside', 'instrument': 'all', 'metric': 'etf_total_seen', 'observed_at': stamp,
                            'value': target['reported_total_musd'], 'trade_date': expected.isoformat()})
        for row in rows:
            if not [r for r in history.read('farside', 'all', 'etf_row_seen') if r.get('trade_date') == row['trade_date']]:
                arrival.append({'source': 'farside', 'instrument': 'all', 'metric': 'etf_row_seen', 'observed_at': stamp,
                                'value': 1, 'trade_date': row['trade_date']})
        history.append(arrival)
        last5 = [facts_by_date[r['trade_date']] for r in rows[-5:]]
        if len(last5) == 5 and all(f['value'] is not None for f in last5):
            total = sum(f['value'] for f in last5)
            five = F.derived(sources=last5, domain='etf', provider='farside', instrument='us_spot_btc_etf',
                             metric='etf_netflow_usd_5d', value=total, unit='USD', window='5d',
                             display=f'{total / 1e6:+,.1f}百万USD', display_value=round(total / 1e6, 1),
                             observation_date=latest_day, key=latest_day.replace('-', ''),
                             method_id='sum_5_us_trading_days', status='provisional',
                             coverage=_coverage(observed=5, expected_count=5,
                                                note=f'{rows[-5]["trade_date"]}〜{latest_day}'))
        else:
            five = F.missing(domain='etf', provider='farside', instrument='us_spot_btc_etf', metric='etf_netflow_usd_5d',
                             unit='USD', window='5d', reason='five_day_window_incomplete', source_id='computed')
        status = 'ok' if lag == 0 else ('lagged' if lag == 1 else 'stale')
        state['etf'].update(status=status, trade_date=latest_day, universe=v['universe'], lag_business_days=lag,
                            flow_fact_ids=[f['fact_id'] for f in facts_by_date.values()],
                            five_day_fact_id=five['fact_id'],
                            last_day_fact_id=facts_by_date[latest_day]['fact_id'],
                            arrival={'row_present': arrival[0]['value'], 'full_numeric': arrival[1]['value'],
                                     'changed_since_previous': arrival[2]['value']})


def fgi_facts(F: Facts, S: dict, state: dict):
    rec = S.get('fgi')
    if not _usable(rec) or not rec['values'].get('series'):
        state['fgi'] = {'fact_id': F.missing(domain='sentiment', provider='alternative_me', instrument='fgi',
                                             metric='fear_greed_index', unit='index', source_id='fgi',
                                             reason=(rec or {}).get('error_kind') or 'source_unavailable')['fact_id']}
        return
    series = rec['values']['series']
    last = series[-1]
    fact = F.observed(record=rec, raw=rec['values']['raw_parts']['r0'], domain='sentiment', provider='alternative_me',
                      instrument='fgi', metric='fear_greed_index', value=last['value'], unit='index', window='1d',
                      display=f'{last["value"]:.0f}（{last["label"]}、出典: Alternative.me）',
                      observed_at=last['t'], stale_after=FGI_STALE, population='Alternative.me Crypto Fear & Greed Index',
                      method_id='provider_index')
    out = {'fact_id': fact['fact_id']}
    for days in (1, 7):
        if len(series) > days:
            prev = series[-1 - days]['value']
            d = last['value'] - prev
            out[f'diff_{days}d'] = F.derived(sources=[fact], domain='sentiment', provider='alternative_me', instrument='fgi',
                                             metric=f'fear_greed_change_{days}d', value=d, unit='point',
                                             display=f'{d:+.0f}ポイント（出典: Alternative.me）', window=f'{days}d',
                                             method_id='index_difference')['fact_id']
    perc, _ = _percentile_fact(F, fact, [{'observed_at': s['t'], 'value': s['value']} for s in series[:-1]],
                               min_count=DAILY_MIN, window_days=365, label='FGI日次・過去365日（出典: Alternative.me）')
    perc['display'] = perc['display'] + '（出典: Alternative.me）'
    out['percentile'] = perc['fact_id']
    state['fgi'] = out


def cot_facts(F: Facts, S: dict, state: dict, as_of: datetime):
    rec = S.get('cftc_tff')
    state['cot'] = {}
    if not _usable(rec) or not rec['values'].get('rows'):
        F.missing(domain='positioning', provider='cftc', instrument='cme_btc_133741', metric='am_net_oi_ratio',
                  unit='percent', reason=(rec or {}).get('error_kind') or 'source_unavailable', source_id='cftc_tff')
        return
    rows = rec['values']['rows']
    last = rows[-1]
    report = date.fromisoformat(last['report_date'])
    friday = report + timedelta(days=(4 - report.weekday()) % 7)
    scheduled = datetime(friday.year, friday.month, friday.day, 15, 30, tzinfo=NY)
    stale = (as_of.date() - report).days > 10
    for prefix, long_key, short_key, label in (('am', 'asset_mgr_positions_long', 'asset_mgr_positions_short', 'Asset Manager'),
                                               ('lf', 'lev_money_positions_long', 'lev_money_positions_short', 'Leveraged Funds'),
                                               ('dealer', 'dealer_positions_long_all', 'dealer_positions_short_all', 'Dealer')):
        ratio = lambda r: 100 * (r[long_key] - r[short_key]) / r['open_interest_all']  # noqa: E731
        value = round(ratio(last), 2)
        fact = F.observed(record=rec, raw=rec['values']['raw_parts']['r0'], domain='positioning', provider='cftc',
                          instrument='cme_btc_133741', metric=f'{prefix}_net_oi_ratio', value=value, unit='percent',
                          display=f'{value:+.1f}%（{label}、{last["report_date"]}時点）', window='1w',
                          observation_date=last['report_date'], scheduled_release_at=z(scheduled),
                          timestamp_quality='source_date', status='stale' if stale else 'ok',
                          freshness_rule_id='cot_10_days', population=f'CFTC TFF Futures Only・CME Bitcoin（133741）{label}',
                          method_id='net_over_open_interest')
        if stale:
            fact['stale'] = True
        history_rows = [{'observed_at': f'{r["report_date"]}T00:00:00+00:00', 'value': ratio(r)} for r in rows[:-1]]
        perc, _ = _percentile_fact(F, fact, history_rows, min_count=COT_MIN_WEEKS, window_count=156,
                                   label=f'{label}・直近156週', as_of=f'{last["report_date"]}T00:00:00+00:00')
        state['cot'][prefix] = fact['fact_id']
        state['cot'][f'{prefix}_percentile'] = perc['fact_id']


def stable_facts(F: Facts, S: dict, state: dict):
    rec = S.get('defillama')
    state['stable'] = {'status': 'missing'}
    if not _usable(rec):
        F.missing(domain='onchain', provider='defillama', instrument='pegged_usd_all', metric='stable_supply_usd',
                  unit='USD', reason=(rec or {}).get('error_kind') or 'source_unavailable', source_id='defillama')
        return
    v = rec['values']
    daily = {d['date']: d['supply_usd'] for d in v['daily']}
    last_day = max(daily)
    total = F.observed(record=rec, raw=v['raw_parts']['history'], domain='onchain', provider='defillama',
                       instrument='pegged_usd_all', metric='stable_supply_usd', value=daily[last_day], unit='USD',
                       window='1d', display=f'{daily[last_day] / 1e9:,.1f}十億USD', display_value=round(daily[last_day] / 1e9, 1),
                       observation_date=last_day, timestamp_quality='source_date', stale_after=STABLE_STALE,
                       observed_at=None, population='DefiLlama peggedUSD 全ステーブル合計（名目供給）',
                       method_id='provider_daily_aggregate',
                       valid_until=z(datetime.fromisoformat(last_day + 'T00:00:00+00:00') + timedelta(days=1, seconds=STABLE_STALE)))
    out = {'status': 'ok', 'total_fact_id': total['fact_id']}
    for days in (7, 30):
        base_day = (date.fromisoformat(last_day) - timedelta(days=days)).isoformat()
        if base_day in daily and daily[base_day] > 0:
            change = 100 * (daily[last_day] / daily[base_day] - 1)
            f = F.derived(sources=[total], domain='onchain', provider='defillama', instrument='pegged_usd_all',
                          metric=f'stable_supply_change_{days}d', value=round(change, 3), unit='percent',
                          display=f'{change:+.2f}%', window=f'{days}d', observation_date=last_day,
                          method_id='supply_change_vs_base_day', coverage=_coverage(note=f'{base_day}比'))
        else:
            f = F.missing(domain='onchain', provider='defillama', instrument='pegged_usd_all',
                          metric=f'stable_supply_change_{days}d', unit='percent', window=f'{days}d',
                          reason='base_day_missing', source_id='computed')
        out[f'change_{days}d'] = f['fact_id']
    depeg = False
    for symbol, asset in (v.get('assets') or {}).items():
        if asset.get('price') is None:
            continue
        dev = round(10000 * (asset['price'] - 1), 1)
        depeg = depeg or abs(dev) > USDT_DEPEG_BPS
        F.observed(record=rec, raw=v['raw_parts']['current'], domain='onchain', provider='defillama',
                   instrument=symbol.lower(), metric='peg_deviation_bps', value=dev, unit='bp', display=f'{dev:+.1f}bp',
                   timestamp_quality='receipt_interval', stale_after=STABLE_STALE, method_id='provider_price_minus_one')
    out['depeg'] = depeg
    state['stable'] = out


def misc_onchain_facts(F: Facts, S: dict, state: dict):
    rec = S.get('coingecko_global')
    if _usable(rec):
        d = rec['values']['btc_dominance_pct']
        state['dominance'] = F.observed(record=rec, raw=rec['values']['raw_parts']['r0'], domain='onchain',
                                        provider='coingecko', instrument='btc', metric='btc_dominance', value=round(d, 2),
                                        unit='percent', display=f'{d:.2f}%（出典: CoinGecko）', observed_at=rec.get('observed_at'),
                                        stale_after=DOMINANCE_STALE, method_id='provider_market_cap_share',
                                        population='CoinGecko global market cap')['fact_id']
    rec = S.get('mempool_fees')
    if _usable(rec):
        fees = rec['values']['fees_sat_vb']
        for key, metric in (('fastestFee', 'fee_fastest'), ('hourFee', 'fee_hour')):
            F.observed(record=rec, raw=rec['values']['raw_parts']['r0'], domain='onchain', provider='mempool_space',
                       instrument='btc', metric=metric, value=fees[key], unit='sat/vB', display=f'{fees[key]:g} sat/vB',
                       stale_after=MEMPOOL_STALE, method_id='provider_recommended_fee',
                       population='mempool.space 推奨手数料（混雑の観測）')


def macro_facts(F: Facts, S: dict, state: dict, as_of: datetime):
    out = {'status': 'unknown', 'reason': 'inputs_missing', 'fact_ids': [], 'proxy_labels': ['broad_usd_proxy']}
    rec = S.get('treasury_yields')
    treasury = {}
    if _usable(rec):
        for row in rec['values']['series']:
            treasury[row['date']] = row
    fred = S.get('fred') if _usable(S.get('fred')) else None
    fred_series = (fred or {}).get('values', {}).get('series', {})
    # latest yields
    if treasury:
        last = max(treasury)
        row = treasury[last]
        for key, metric, raw_key in (('y2_pct', 'ust_2y', 'nominal_raw'), ('y10_pct', 'ust_10y', 'nominal_raw'),
                                     ('real10_pct', 'ust_real_10y', 'real_raw')):
            if row.get(key) is None:
                continue
            f = F.observed(record=rec, raw=row[raw_key], domain='macro', provider='us_treasury', instrument=metric,
                           metric='yield', value=row[key], unit='percent', display=f'{row[key]:.2f}%（{last}）',
                           observation_date=last, timestamp_quality='source_date', window='1d',
                           freshness_rule_id='daily_4_calendar_days', method_id='treasury_par_yield_eod',
                           valid_until=z(datetime.fromisoformat(last + 'T23:59:59+00:00') + timedelta(days=4)))
            out[metric] = f['fact_id']
            out['fact_ids'].append(f['fact_id'])
    for sid, info in fred_series.items():
        rows = info['rows']
        if not rows:
            continue
        lastrow = rows[-1]
        limit = {'daily': 4, 'weekly': 10, 'monthly': 75}[info['frequency']]
        unit = info['unit']
        if sid in ('WALCL', 'WDTGAL'):
            disp = f'{lastrow["value"] / 1e6:,.3f}兆USD（{lastrow["date"]}水曜時点）'
        elif sid == 'RRPONTSYD':
            disp = f'{lastrow["value"]:,.1f}十億USD（{lastrow["date"]}）'
        elif sid == 'M2SL':
            disp = f'{lastrow["value"] / 1000:,.2f}兆USD（{lastrow["date"][:7]}分）'
        else:
            disp = f'{lastrow["value"]:,.2f}（{lastrow["date"]}）'
        f = F.observed(record=fred, raw=info['raw'], domain='macro', provider='fred', instrument=sid.lower(),
                       metric='level', value=lastrow['value'], unit=unit, display=disp, observation_date=lastrow['date'],
                       timestamp_quality='source_date', window=info['frequency'],
                       freshness_rule_id=f'{info["frequency"]}_{limit}_calendar_days', method_id='fred_observation',
                       population=info['label'],
                       valid_until=z(datetime.fromisoformat(lastrow['date'] + 'T23:59:59+00:00') + timedelta(days=limit)))
        out[sid] = f['fact_id']
        out['fact_ids'].append(f['fact_id'])
        if sid in ('DTWEXBGS', 'VIXCLS', 'NASDAQCOM', 'SP500') and len(rows) >= 2:
            prev = rows[-2]
            change = 100 * (lastrow['value'] / prev['value'] - 1)
            c = F.derived(sources=[f], domain='macro', provider='fred', instrument=sid.lower(), metric='change_1d',
                          value=round(change, 3), unit='percent', display=f'{change:+.2f}%（{prev["date"]}→{lastrow["date"]}）',
                          window='1d', observation_date=lastrow['date'], method_id='pct_change_previous_observation')
            out[f'{sid}_change'] = c['fact_id']
    # Net liquidity proxy: same Wednesday for WALCL, WDTGAL and RRP.
    if all(k in fred_series for k in ('WALCL', 'WDTGAL', 'RRPONTSYD')):
        dates = {r['date'] for r in fred_series['WALCL']['rows']} & {r['date'] for r in fred_series['WDTGAL']['rows']} & \
            {r['date'] for r in fred_series['RRPONTSYD']['rows']}
        if dates:
            d = max(dates)
            pick = lambda sid: next(r['value'] for r in fred_series[sid]['rows'] if r['date'] == d)  # noqa: E731
            value = pick('WALCL') * 1e6 - pick('WDTGAL') * 1e6 - pick('RRPONTSYD') * 1e9
            src = [F.index[out[k]] for k in ('WALCL', 'WDTGAL', 'RRPONTSYD') if k in out]
            nl = F.derived(sources=src, domain='macro', provider='fred', instrument='net_liquidity_proxy',
                           metric='net_liquidity_proxy_usd', value=value, unit='USD',
                           display=f'{value / 1e12:,.3f}兆USD（{d}水曜、WALCL−TGA−RRP）', display_value=round(value / 1e12, 3),
                           observation_date=d, window='1w', method_id='walcl_minus_tga_minus_rrp_same_wednesday')
            out['net_liquidity'] = nl['fact_id']
    # Macro group rule (design 7.2): latest common US date of the 2y yield and the broad dollar proxy vs the previous one.
    dollar_rows = {r['date']: r['value'] for r in fred_series.get('DTWEXBGS', {}).get('rows', [])}
    common = sorted(set(treasury) & set(dollar_rows))
    if not treasury:
        out.update(status='unknown', reason='treasury_missing')
    elif not dollar_rows:
        out.update(status='unknown', reason='dollar_proxy_missing' if fred else 'fred_unavailable')
    elif len(common) < 2:
        out.update(status='unknown', reason='no_common_dates')
    else:
        d1, d0 = common[-1], common[-2]
        if (as_of.date() - date.fromisoformat(d1)).days > 4:
            out.update(status='unknown', reason='common_observation_stale')
        else:
            y = (treasury[d1]['y2_pct'] - treasury[d0]['y2_pct']) * 100
            usd = 100 * (dollar_rows[d1] / dollar_rows[d0] - 1)
            y2_fact = F.observed(record=rec, raw=treasury[d1]['nominal_raw'], domain='macro', provider='us_treasury',
                                 instrument='ust_2y', metric='yield_common_date', value=treasury[d1]['y2_pct'],
                                 unit='percent', display=f'{treasury[d1]["y2_pct"]:.2f}%（{d1}）', observation_date=d1,
                                 timestamp_quality='source_date', window='1d', method_id='treasury_par_yield_eod')
            dy = F.derived(sources=[y2_fact], domain='macro', provider='us_treasury', instrument='ust_2y',
                           metric='change_bp_common_window', value=round(y, 1), unit='bp',
                           display=f'{y:+.1f}bp（{d0}→{d1}）', window='1d', observation_date=d1,
                           method_id='yield_change_bp_common_dates')
            dollar_fact = F.index.get(out.get('DTWEXBGS'))
            du = F.derived(sources=[dollar_fact] if dollar_fact else [y2_fact], domain='macro', provider='fred',
                           instrument='dtwexbgs', metric='change_pct_common_window', value=round(usd, 3), unit='percent',
                           display=f'{usd:+.2f}%（{d0}→{d1}、米ドル広義指数）', window='1d', observation_date=d1,
                           method_id='pct_change_common_dates')
            if y <= -5 and usd <= -0.30:
                status = 'supportive'
            elif y >= 5 and usd >= 0.30:
                status = 'adverse'
            elif (y <= -5 and usd >= 0.30) or (y >= 5 and usd <= -0.30):
                status = 'mixed'
            else:
                status = 'neutral'
            out.update(status=status, reason=f'macro_{status}', window='daily_common_observation',
                       common_date=d1, previous_date=d0, change_fact_ids=[dy['fact_id'], du['fact_id']])
    state['macro'] = out


def calendar_events(S: dict, as_of: datetime, cache: dict) -> tuple[list, dict]:
    events = []
    status = {'bls_cache': 'ok', 'fed': 'missing', 'bea': 'missing', 'coverage_end': cache['coverage_end'],
              'verified_at': cache['verified_at'], 'reasons': []}
    for event in cache['events']:
        events.append(dict(event))
    if (as_of + timedelta(hours=24)).date() > date.fromisoformat(cache['coverage_end']):
        status['bls_cache'] = 'beyond_coverage'
        status['reasons'].append('BLS予定はキャッシュ範囲外（予定未確認）')
    for key, source_id in (('fed', 'fed_calendar'), ('bea', 'bea_calendar')):
        rec = S.get(source_id)
        if _usable(rec):
            status[key] = 'ok'
            for event in rec['values']['events']:
                events.append(dict(event, verified_at=rec['retrieved_at']))
        else:
            status['reasons'].append(f'{source_id}: {(rec or {}).get("error_kind") or "source_unavailable"}（予定未確認）')
    status['next_24h'] = 'ok' if status['bls_cache'] == 'ok' and status['fed'] == 'ok' and status['bea'] == 'ok' else 'unknown'
    # B8: CME contract calendars are not reachable from the Mac (403); expiries stay unconfirmed and never
    # create a stop window from a "last Friday" estimate.
    status['cme_expiry'] = 'unconfirmed'
    return events, status


def stop_window(event: dict) -> dict | None:
    start = parse_time(event['start_at'])
    ident = event['id']
    if ident.startswith('fomc.statement'):
        return {'start_at': z(start - timedelta(minutes=30)), 'end_at': z(start + timedelta(minutes=90)),
                'event_ids': [ident], 'rule_id': 'fomc_statement_m30_p90'}
    if ident.startswith('fomc.press'):
        statement = start - timedelta(minutes=30)
        return {'start_at': z(start - timedelta(minutes=15)), 'end_at': z(statement + timedelta(minutes=120)),
                'event_ids': [ident], 'rule_id': 'fomc_press_m15_to_statement_p120'}
    if any(ident.startswith(p) for p in ('bls.cpi', 'bls.empsit', 'bea.pce', 'bea.gdp')):
        return {'start_at': z(start - timedelta(minutes=15)), 'end_at': z(start + timedelta(minutes=30)),
                'event_ids': [ident], 'rule_id': 'macro_release_m15_p30'}
    return None


def events_state(F: Facts, S: dict, state: dict, as_of: datetime):
    from btc.sources.calendar import load_cache
    cache = load_cache()
    events, status = calendar_events(S, as_of, cache)
    events += state.get('options', {}).get('events', [])
    out = []
    for event in events:
        start = event.get('start_at')
        if not start:
            continue
        t = parse_time(start)
        if not (as_of - timedelta(days=1) <= t <= as_of + timedelta(days=8)):
            continue
        item = {'id': event['id'], 'name': md_safe(event['name']), 'category': event['category'], 'start_at': z(start),
                'end_at': z(event['end_at']) if event.get('end_at') else None, 'timezone': event.get('timezone', 'UTC'),
                'importance': event['importance'], 'status': event['status'], 'source_id': event['source_id'],
                'source_url': event['source_url'], 'published_at': None,
                'verified_at': z(event.get('verified_at') or as_of)}
        item['stop_window'] = event.get('stop_window') or stop_window(item)
        out.append(item)
    out.sort(key=lambda e: e['start_at'])
    state['events'] = out
    state['calendar'] = status


def news_state(F: Facts, S: dict, state: dict):
    rec = S.get('news')
    state['news'] = {'status': 'missing', 'items': [], 'feeds': [], 'selection': None}
    if not _usable(rec):
        return
    v = rec['values']
    items = []
    for item in v['kept']:
        if not re.match(r'^https?://[^\s]+$', item.get('url') or ''):
            continue  # a feed link that is not plain http(s) is never shown or linked
        reaction = item.get('reaction')
        fact_ids = []
        if reaction and reaction.get('raw_sha256'):
            for key, label in (('return_before_5m_bps', 'before_5m'), ('return_after_1m_bps', 'after_1m'),
                               ('return_after_5m_bps', 'after_5m')):
                value = reaction.get(key)
                f = F.observed(record=rec, raw=reaction['raw_sha256'], domain='news', provider='binance',
                               instrument=item['id'], metric=f'reaction_{label}', value=value, unit='bp',
                               display=f'{value:+.1f}bp' if value is not None else '—', observed_at=reaction['baseline_at'],
                               key=stamp_key(reaction['baseline_at']), timestamp_quality='source_observed',
                               method_id='binance_btcusdt_1m_close_to_close', status='ok' if value is not None else 'missing',
                               missing_reason=None if value is not None else 'bar_missing',
                               population='Binance BTCUSDT 1分足（USDT建て・因果ではない）', source_id='binance_klines')
                fact_ids.append(f['fact_id'])
        items.append({
            'id': item['id'], 'event_cluster_id': item['event_cluster_id'], 'title': md_safe(item['title']),
            'url': item['url'],
            'publisher': item['publisher'], 'official': item['official'], 'published_at': z(item['published_at']),
            'first_seen_at': z(item['first_seen_at']) if item.get('first_seen_at') else None,
            'code_verification': item['code_verification'], 'body': _safe_body(item.get('body') or {}),
            'excerpt': md_safe(item.get('excerpt', '')), 'p': item.get('p'), 'reaction': reaction, 'fact_ids': fact_ids,
            'feed_raw_sha256': item.get('feed_raw_sha256'),
        })
    state['news'] = {'status': rec['status'], 'items': items, 'feeds': v['feeds'], 'selection': v['selection'],
                     'selection_rule': v['selection_rule'], 'candidate_count': v['candidate_count'],
                     'window_start': v['window_start'], 'window_end': v['window_end'],
                     'lookback_hours': v['lookback_hours'], 'feeds_ok': v['feeds_ok']}


def _safe_body(body: dict) -> dict:
    out = dict(body)
    if 'excerpt' in out:
        out['excerpt'] = md_safe(out['excerpt'])
    return out


def source_health(S: dict, as_of: datetime) -> list[dict]:
    out = []
    for source_id, rec in S.items():
        status = {'ok': 'ok', 'partial': 'partial', 'stale': 'stale', 'unavailable': 'missing'}[rec['status']]
        out.append({'source_id': source_id, 'status': status, 'checked_at': z(rec.get('retrieved_at') or as_of),
                    'last_observed_at': z(rec['observed_at']) if rec.get('observed_at') else None,
                    'reason': rec.get('error_kind') or ''})
    out.append({'source_id': 'coinbase', 'status': 'terms_restricted', 'checked_at': z(as_of), 'last_observed_at': None,
                'reason': '規約により未使用'})
    out.append({'source_id': 'coinglass', 'status': 'not_applicable', 'checked_at': z(as_of), 'last_observed_at': None,
                'reason': '未使用（有料・推定モデル）'})
    return out


def build(collection: dict, *, mode: str, session_slot: str, collection_path, history_dir) -> dict:
    as_of = parse_time(collection['collection_completed_at']).astimezone(UTC)
    manifest = digest(collection_path)
    F = Facts(as_of, manifest)
    history = History(history_dir)
    S = {r['source_id']: r for r in collection['sources']}
    state: dict = {}
    price_facts(F, S, state)
    book_facts(F, S, state)
    derivatives_facts(F, S, state, history, as_of)
    ratio_facts(F, S, state)
    options_facts(F, S, state, history, as_of)
    etf_facts(F, S, state, history, as_of)
    fgi_facts(F, S, state)
    cot_facts(F, S, state, as_of)
    stable_facts(F, S, state)
    misc_onchain_facts(F, S, state)
    macro_facts(F, S, state, as_of)
    events_state(F, S, state, as_of)
    news_state(F, S, state)
    state['source_health'] = source_health(S, as_of)
    state['terms_restricted'] = [{'source_id': t['source_id'], 'reason': t['reason']}
                                 for t in collection.get('terms_restricted', []) if t.get('status') == 'terms_restricted']
    state['not_used'] = [{'source_id': t['source_id'], 'reason': t['reason']}
                         for t in collection.get('terms_restricted', []) if t.get('status') != 'terms_restricted']
    state['collection'] = {'started_at': z(collection['collection_started_at']), 'completed_at': z(as_of),
                           'elapsed_ms': collection.get('elapsed_ms'), 'budget_exhausted': collection.get('budget_exhausted'),
                           'collector_version': collection.get('collector_version'),
                           'news_lookback_hours': collection.get('news_lookback_hours')}
    return {'schema_version': SCHEMA_VERSION, 'symbol': SYMBOL, 'asset': SYMBOL, 'mode': mode,
            'session_slot': session_slot, 'as_of': z(as_of), 'collected_at': z(collection['collection_started_at']),
            'input_manifest_sha256': manifest, 'facts': F.items, 'state': state}


def index(facts: dict) -> dict:
    return {f['fact_id']: f for f in facts['facts']}


def dumps_state(state: dict) -> str:
    return json.dumps(state, ensure_ascii=False, sort_keys=True)
