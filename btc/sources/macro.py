"""Stablecoin supply, mempool fees, BTC dominance, US Treasury yields and FRED series.

FRED is the only credentialed source (FRED_API_KEY, P2/P8). The key is passed
as a query parameter and removed from every recorded URL. ``DTWEXBGS`` is the
FRED broad US dollar index, never "DXY"; equity series are cash index closes.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import os
import xml.etree.ElementTree as ET

from btc.fetch import FetchError, Fetcher, SourceResult
from btc.sources.base import from_s, json_of, num, ok

UTC = timezone.utc
STABLES = ('USDT', 'USDC')
FRED_SERIES = {
    # series: (label, unit, frequency)
    'DTWEXBGS': ('米ドル広義指数（FRED）', 'index', 'daily'),
    'VIXCLS': ('VIX（現物指数・終値）', 'index', 'daily'),
    'NASDAQCOM': ('Nasdaq総合（現物指数・終値）', 'index', 'daily'),
    'SP500': ('S&P 500（現物指数・終値）', 'index', 'daily'),
    'WALCL': ('Fed総資産（水曜時点）', 'USD million', 'weekly'),
    'WDTGAL': ('財務省一般口座TGA（水曜時点）', 'USD million', 'weekly'),
    'RRPONTSYD': ('翌日物リバースレポ', 'USD billion', 'daily'),
    'M2SL': ('M2（月次）', 'USD billion', 'monthly'),
}


def defillama(f: Fetcher) -> SourceResult:
    current = f.get('https://stablecoins.llama.fi/stablecoins', params={'includePrices': 'true'})
    history = f.get('https://stablecoins.llama.fi/stablecoincharts/all')
    assets = {}
    for row in json_of(current).get('peggedAssets', []):
        if row.get('symbol') in STABLES and row.get('pegType') == 'peggedUSD':
            assets[row['symbol']] = {'supply_usd': num((row.get('circulating') or {}).get('peggedUSD')),
                                     'price': num(row.get('price'), allow_none=True)}
    series = []
    for row in json_of(history)[-60:]:
        value = num(((row.get('totalCirculatingUSD') or {}).get('peggedUSD')), allow_none=True)
        if value is not None:
            series.append({'date': datetime.fromtimestamp(int(row['date']), UTC).date().isoformat(), 'supply_usd': value})
    if len(series) < 31:
        raise FetchError('missing_field')
    return ok('defillama', {'current': current, 'history': history}, {'scope': 'all peggedUSD stablecoins (DefiLlama aggregate)',
                                                'daily': series, 'assets': assets},
              timestamp_quality='source_date')


def mempool(f: Fetcher) -> SourceResult:
    r = f.get('https://mempool.space/api/v1/fees/recommended')
    d = json_of(r)
    values = {k: num(d[k]) for k in ('fastestFee', 'halfHourFee', 'hourFee', 'economyFee', 'minimumFee')}
    return ok('mempool_fees', [r], {'fees_sat_vb': values}, timestamp_quality='receipt_interval')


def coingecko_global(f: Fetcher) -> SourceResult:
    r = f.get('https://api.coingecko.com/api/v3/global')
    d = json_of(r)['data']
    share = num(d['market_cap_percentage']['btc'])
    if not 0 < share < 100:
        raise FetchError('invalid_payload')
    return ok('coingecko_global', [r], {'btc_dominance_pct': share, 'attribution': 'CoinGecko'},
              observed_at=from_s(d['updated_at']))


def _treasury_month(f: Fetcher, data: str, month: str):
    r = f.get('https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml',
              params={'data': data, 'field_tdr_date_value_month': month})
    if b'<!DOCTYPE' in r.body[:2000].upper() or b'<!ENTITY' in r.body.upper():
        raise FetchError('invalid_payload')
    try:
        root = ET.fromstring(r.body)
    except ET.ParseError:
        raise FetchError('parse_error') from None
    rows = []
    for props in root.iter('{http://schemas.microsoft.com/ado/2007/08/dataservices/metadata}properties'):
        item = {}
        for child in props:
            item[child.tag.rsplit('}', 1)[-1]] = (child.text or '').strip()
        if item.get('NEW_DATE'):
            rows.append(item)
    return r, rows


def treasury(f: Fetcher, today: date | None = None) -> SourceResult:
    today = today or datetime.now(UTC).date()
    months = [today.strftime('%Y%m'), (today.replace(day=1) - timedelta(days=1)).strftime('%Y%m')]
    responses, nominal, real = {}, {}, {}
    for month in months:
        r, rows = _treasury_month(f, 'daily_treasury_yield_curve', month)
        responses[f'nominal_{month}'] = r
        for row in rows:
            day = row['NEW_DATE'][:10]
            nominal[day] = {'y2': num(row.get('BC_2YEAR'), allow_none=True), 'y10': num(row.get('BC_10YEAR'), allow_none=True),
                            'raw': r.raw_sha256}
        r, rows = _treasury_month(f, 'daily_treasury_real_yield_curve', month)
        responses[f'real_{month}'] = r
        for row in rows:
            real[row['NEW_DATE'][:10]] = (num(row.get('TC_10YEAR'), allow_none=True), r.raw_sha256)
    days = sorted(nominal)
    if not days:
        raise FetchError('empty_payload')
    series = [{'date': d, 'y2_pct': nominal[d]['y2'], 'y10_pct': nominal[d]['y10'], 'nominal_raw': nominal[d]['raw'],
               'real10_pct': (real.get(d) or (None, None))[0], 'real_raw': (real.get(d) or (None, None))[1]} for d in days]
    return ok('treasury_yields', responses, {'series': series[-30:], 'unit': 'percent',
                                             'note': 'Daily Treasury Par Yield Curve / Real Yield Curve (end of day)'},
              timestamp_quality='source_date')


def fred(f: Fetcher, env=os.environ) -> SourceResult:
    key = env.get('FRED_API_KEY')
    if not key:
        raise FetchError('credentials_missing')
    responses, series = {}, {}
    failed = []
    for sid, (label, unit, freq) in FRED_SERIES.items():
        limit = {'daily': 40, 'weekly': 12, 'monthly': 6}[freq]
        try:
            r = f.get('https://api.stlouisfed.org/fred/series/observations', params={
                'series_id': sid, 'api_key': key, 'file_type': 'json', 'sort_order': 'desc', 'limit': limit},
                secret_params=['api_key'])
        except FetchError as error:
            failed.append({'series_id': sid, 'error_kind': error.kind})
            continue
        responses[sid] = r
        rows = []
        for obs in json_of(r).get('observations', []):
            value = num(obs.get('value'), allow_none=True)
            if value is not None:
                rows.append({'date': obs['date'], 'value': value})
        rows.sort(key=lambda x: x['date'])
        series[sid] = {'label': label, 'unit': unit, 'frequency': freq, 'rows': rows, 'raw': r.raw_sha256}
    if not series:
        raise FetchError(failed[0]['error_kind'] if failed else 'empty_payload')
    return ok('fred', responses, {'series': series, 'failed': failed},
              status='ok' if not failed else 'partial', timestamp_quality='source_date')
