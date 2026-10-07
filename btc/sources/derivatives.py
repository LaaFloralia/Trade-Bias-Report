"""Binance / Bybit / OKX linear BTCUSDT perpetual data (design 5.1, 5.3).

- Settled funding comes from history endpoints (Binance/Bybit ``fundingRate``,
  OKX ``realizedRate``); current/predicted rates are kept separately.
- OI is single-sided: Binance ``openInterest`` (BTC), Bybit
  ``singleOpenInterest`` (BTC; ``openInterest`` is two-sided and never halved),
  OKX ``oiCcy`` (BTC). 24 h change uses one provider series at a time.
"""
from __future__ import annotations

from btc.fetch import FetchError, Fetcher, SourceResult
from btc.sources.base import from_ms, json_of, num, ok

SYMBOL = 'BTCUSDT'
FUNDING_HISTORY_LIMIT = 100  # about 33 days of 8 h settlements
RATIO_HISTORY_LIMIT = 500    # 1 h buckets, about 20.8 days (>= 336 required)


def _settlements(rows, time_key, rate_key):
    out = []
    for row in rows:
        out.append({'t': from_ms(row[time_key]), 'rate': num(row[rate_key])})
    out.sort(key=lambda x: x['t'])
    return out


def binance_derivatives(f: Fetcher) -> SourceResult:
    premium = f.get('https://fapi.binance.com/fapi/v1/premiumIndex', params={'symbol': SYMBOL})
    p = json_of(premium)
    history = f.get('https://fapi.binance.com/fapi/v1/fundingRate', params={'symbol': SYMBOL, 'limit': FUNDING_HISTORY_LIMIT})
    oi = f.get('https://fapi.binance.com/fapi/v1/openInterest', params={'symbol': SYMBOL})
    o = json_of(oi)
    oi_hist = f.get('https://fapi.binance.com/futures/data/openInterestHist', params={'symbol': SYMBOL, 'period': '1h', 'limit': 25})
    mark = num(p['markPrice'])
    values = {
        'venue': 'binance', 'instrument': SYMBOL, 'mark_price': mark, 'index_price': num(p['indexPrice']),
        'mark_time': from_ms(p['time']), 'current_funding_rate': num(p['lastFundingRate']),
        'next_funding_time': from_ms(p['nextFundingTime']),
        'funding_settled': _settlements(json_of(history), 'fundingTime', 'fundingRate'),
        'oi_btc': num(o['openInterest']), 'oi_time': from_ms(o['time']),
        'oi_hourly': [{'t': from_ms(row['timestamp']), 'oi_btc': num(row['sumOpenInterest'])}
                      for row in sorted(json_of(oi_hist), key=lambda x: x['timestamp'])],
    }
    values['oi_usd'] = values['oi_btc'] * mark
    return ok('binance_derivatives', {'premium': premium, 'funding_history': history, 'oi': oi, 'oi_hourly': oi_hist}, values, observed_at=values['oi_time'])


def _ratio(f: Fetcher, path: str, source_id: str, population: str) -> SourceResult:
    r = f.get(f'https://fapi.binance.com/futures/data/{path}', params={'symbol': SYMBOL, 'period': '1h', 'limit': RATIO_HISTORY_LIMIT})
    rows = sorted(json_of(r), key=lambda x: x['timestamp'])
    series = [{'t': from_ms(row['timestamp']), 'long_share': num(row['longAccount'])} for row in rows]
    for item in series:
        if not 0 <= item['long_share'] <= 1:
            raise FetchError('invalid_payload')
    return ok(source_id, [r], {'venue': 'binance', 'instrument': SYMBOL, 'period': '1h', 'population': population,
                               'series': series}, observed_at=series[-1]['t'] if series else None)


def binance_ls(f: Fetcher) -> SourceResult:
    return _ratio(f, 'globalLongShortAccountRatio', 'binance_ls', 'binance_all_accounts')


def binance_top_position(f: Fetcher) -> SourceResult:
    return _ratio(f, 'topLongShortPositionRatio', 'binance_top_position', 'binance_top_trader_positions')


def binance_taker(f: Fetcher) -> SourceResult:
    r = f.get('https://fapi.binance.com/futures/data/takerlongshortRatio', params={'symbol': SYMBOL, 'period': '1h', 'limit': 24})
    rows = sorted(json_of(r), key=lambda x: x['timestamp'])
    series = [{'t': from_ms(row['timestamp']), 'buy': num(row['buyVol']), 'sell': num(row['sellVol'])} for row in rows]
    return ok('binance_taker', [r], {'venue': 'binance', 'instrument': SYMBOL, 'period': '1h', 'series': series,
                                     'unit': 'BTC'}, observed_at=series[-1]['t'] if series else None)


def _bybit(data):
    if str(data.get('retCode')) != '0':
        raise FetchError('invalid_payload')
    return data['result']


def bybit_derivatives(f: Fetcher) -> SourceResult:
    ticker = f.get('https://api.bybit.com/v5/market/tickers', params={'category': 'linear', 'symbol': SYMBOL})
    t = _bybit(json_of(ticker))['list'][0]
    history = f.get('https://api.bybit.com/v5/market/funding/history',
                    params={'category': 'linear', 'symbol': SYMBOL, 'limit': 200})
    oi_hist = f.get('https://api.bybit.com/v5/market/open-interest',
                    params={'category': 'linear', 'symbol': SYMBOL, 'intervalTime': '1h', 'limit': 25})
    tdata = json_of(ticker)
    mark = num(t['markPrice'])
    single = num(t.get('singleOpenInterest'), allow_none=True)
    notes = [] if single is not None else ['single_sided_oi_missing']
    values = {
        'venue': 'bybit', 'instrument': SYMBOL, 'mark_price': mark, 'index_price': num(t['indexPrice']),
        'mark_time': from_ms(tdata['time']), 'current_funding_rate': num(t['fundingRate']),
        'next_funding_time': from_ms(t['nextFundingTime']),
        'funding_interval_hours': num(t.get('fundingIntervalHour'), allow_none=True),
        'funding_settled': _settlements(_bybit(json_of(history))['list'], 'fundingRateTimestamp', 'fundingRate')[-FUNDING_HISTORY_LIMIT:],
        'oi_btc': single, 'oi_time': from_ms(tdata['time']),
        # History: the provider's own series; only its ratio is used (two-sided or not, never mixed with `single`).
        'oi_hourly': [{'t': from_ms(row['timestamp']), 'oi_btc': num(row['openInterest'])}
                      for row in sorted(_bybit(json_of(oi_hist))['list'], key=lambda x: int(x['timestamp']))],
        'oi_hourly_convention': 'provider_openInterest_series',
    }
    values['oi_usd'] = single * mark if single is not None else None
    return ok('bybit_derivatives', {'ticker': ticker, 'funding_history': history, 'oi_hourly': oi_hist}, values, observed_at=values['oi_time'],
              status='ok' if single is not None else 'partial', notes=notes)


def _okx(data):
    if str(data.get('code')) != '0' or not data.get('data'):
        raise FetchError('invalid_payload')
    return data['data']


def okx_derivatives(f: Fetcher) -> SourceResult:
    current = f.get('https://www.okx.com/api/v5/public/funding-rate', params={'instId': 'BTC-USDT-SWAP'})
    c = _okx(json_of(current))[0]
    history = f.get('https://www.okx.com/api/v5/public/funding-rate-history', params={'instId': 'BTC-USDT-SWAP', 'limit': 100})
    oi = f.get('https://www.okx.com/api/v5/public/open-interest', params={'instType': 'SWAP', 'instId': 'BTC-USDT-SWAP'})
    o = _okx(json_of(oi))[0]
    mark_r = f.get('https://www.okx.com/api/v5/public/mark-price', params={'instType': 'SWAP', 'instId': 'BTC-USDT-SWAP'})
    m = _okx(json_of(mark_r))[0]
    mark = num(m['markPx'])
    values = {
        'venue': 'okx', 'instrument': 'BTC-USDT-SWAP', 'mark_price': mark, 'mark_time': from_ms(m['ts']),
        'current_funding_rate': num(c['fundingRate']), 'current_funding_method': c.get('method'),
        'next_funding_time': from_ms(c['fundingTime']),
        'funding_settled': _settlements(_okx(json_of(history)), 'fundingTime', 'realizedRate'),
        'oi_btc': num(o['oiCcy']), 'oi_time': from_ms(o['ts']), 'oi_hourly': [],
    }
    values['oi_usd'] = values['oi_btc'] * mark
    return ok('okx_derivatives', {'funding_current': current, 'funding_history': history, 'oi': oi, 'mark': mark_r}, values, observed_at=values['oi_time'])
