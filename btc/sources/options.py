"""Deribit BTC inverse options: OI by expiry/strike, DVOL and 25-delta skew inputs.

Kept (P8): a normalised table of expiry, strike, type and OI (BTC) plus the
few ticker quotes used for skew; never the raw instrument list.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
import re
import time

from btc.fetch import FetchError, Fetcher, SourceResult
from btc.sources.base import from_ms, json_of, num, ok

UTC = timezone.utc
API = 'https://www.deribit.com/api/v2/public'
INSTRUMENT = re.compile(r'BTC-\d{1,2}[A-Z]{3}\d{2}-\d{1,7}(?:d\d{1,4})?-[CP]')
SKEW_TARGETS = ((7, 3), (30, 10))  # (target DTE, tolerance) per design 5.3
MAX_TICKERS = 16


def _result(data):
    if 'result' not in data:
        raise FetchError('invalid_payload')
    return data['result']


def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def approx_delta(kind: str, spot: float, strike: float, years: float, iv_pct: float) -> float | None:
    """Black-Scholes delta (r=0) only to choose candidate strikes; never reported as a fact."""
    if spot <= 0 or strike <= 0 or years <= 0 or iv_pct <= 0:
        return None
    sigma = iv_pct / 100
    d1 = (math.log(spot / strike) + 0.5 * sigma * sigma * years) / (sigma * math.sqrt(years))
    return _norm_cdf(d1) if kind == 'call' else _norm_cdf(d1) - 1


def deribit_options(f: Fetcher, now: datetime | None = None, sleep=time.sleep) -> SourceResult:
    now = now or datetime.now(UTC)
    summary_r = f.get(f'{API}/get_book_summary_by_currency', params={'currency': 'BTC', 'kind': 'option'})
    instruments_r = f.get(f'{API}/get_instruments', params={'currency': 'BTC', 'kind': 'option', 'expired': 'false'})
    meta = {}
    for row in _result(json_of(instruments_r)):
        meta[row['instrument_name']] = {'expiry': from_ms(row['expiration_timestamp']), 'strike': num(row['strike']),
                                        'type': row['option_type'], 'settlement': row.get('settlement_currency')}
    table, underlying = [], {}
    for row in _result(json_of(summary_r)):
        info = meta.get(row.get('instrument_name'))
        if info is None or info['type'] not in ('call', 'put'):
            continue
        oi = num(row.get('open_interest'), allow_none=True)
        if oi is None or oi < 0:
            continue
        item = {'name': row['instrument_name'], 'expiry': info['expiry'], 'strike': info['strike'], 'type': info['type'],
                'oi_btc': oi, 'mark_iv': num(row.get('mark_iv'), allow_none=True),
                'underlying_price': num(row.get('underlying_price'), allow_none=True)}
        table.append(item)
        if item['underlying_price']:
            underlying[item['expiry']] = item['underlying_price']
    if not table:
        raise FetchError('empty_payload')
    responses = {'book_summary': summary_r, 'instruments': instruments_r}
    observed = from_ms(max(int(r.get('creation_timestamp') or 0) for r in _result(json_of(summary_r))))

    # DVOL: daily closes for the 90-day rank and the latest closed 1-minute value.
    end_ms = int(now.timestamp() * 1000)
    dvol_daily_r = f.get(f'{API}/get_volatility_index_data', params={
        'currency': 'BTC', 'start_timestamp': end_ms - 120 * 86400000, 'end_timestamp': end_ms, 'resolution': '1D'})
    dvol_min_r = f.get(f'{API}/get_volatility_index_data', params={
        'currency': 'BTC', 'start_timestamp': end_ms - 3600000, 'end_timestamp': end_ms, 'resolution': '60'})
    responses.update(dvol_daily=dvol_daily_r, dvol_minute=dvol_min_r)
    daily = [{'t': from_ms(row[0]), 'close': num(row[4])} for row in _result(json_of(dvol_daily_r)).get('data', [])]
    minute = [{'t': from_ms(row[0]), 'close': num(row[4])} for row in _result(json_of(dvol_min_r)).get('data', [])]
    # The last minute bar may still be forming: use the last bar that closed before now.
    closed = [m for m in minute if datetime.fromisoformat(m['t']).timestamp() + 60 <= now.timestamp()]

    # 25-delta skew candidates: per target expiry, two strikes bracketing |delta|=0.25 per type.
    skew_quotes, picked = [], 0
    expiries = sorted({row['expiry'] for row in table})
    for target, tolerance in SKEW_TARGETS:
        best = None
        for expiry in expiries:
            dte = (datetime.fromisoformat(expiry) - now).total_seconds() / 86400
            if dte >= 2 and abs(dte - target) <= tolerance and (best is None or abs(dte - target) < abs(best[1] - target)):
                best = (expiry, dte)
        if best is None:
            continue
        expiry, dte = best
        spot = underlying.get(expiry)
        for kind, goal in (('put', -0.25), ('call', 0.25)):
            rows = [r for r in table if r['expiry'] == expiry and r['type'] == kind and r['mark_iv']]
            scored = []
            for r in rows:
                d = approx_delta(kind, spot or 0, r['strike'], dte / 365, r['mark_iv'])
                if d is not None:
                    scored.append((d, r))
            below = sorted([x for x in scored if x[0] <= goal], key=lambda x: -x[0])[:1]
            above = sorted([x for x in scored if x[0] > goal], key=lambda x: x[0])[:1]
            for _, r in below + above:
                if picked >= MAX_TICKERS:
                    break
                if not INSTRUMENT.fullmatch(str(r['name'])):  # remote value used as a request parameter
                    continue
                try:
                    tr = f.get(f'{API}/ticker', params={'instrument_name': r['name']})
                    t = _result(json_of(tr))
                    picked += 1
                    responses[f'ticker_{picked}'] = tr
                    skew_quotes.append({'name': r['name'], 'expiry': expiry, 'dte': dte, 'type': kind, 'strike': r['strike'],
                                        'target_dte': target, 'delta': num(t['greeks']['delta'], allow_none=True),
                                        'mark_iv': num(t.get('mark_iv'), allow_none=True),
                                        'bid': num(t.get('best_bid_price'), allow_none=True),
                                        'ask': num(t.get('best_ask_price'), allow_none=True),
                                        'oi_btc': num(t.get('open_interest'), allow_none=True),
                                        'timestamp': from_ms(t['timestamp']), 'raw': tr.raw_sha256})
                except (FetchError, KeyError, TypeError):
                    continue
    values = {'venue': 'deribit', 'underlying_scope': 'BTC inverse options (Deribit only)', 'table': table,
              'underlying_by_expiry': underlying, 'dvol_daily': daily,
              'dvol_latest': closed[-1] if closed else None, 'skew_quotes': skew_quotes}
    return ok('deribit_options', responses, values, observed_at=observed)
