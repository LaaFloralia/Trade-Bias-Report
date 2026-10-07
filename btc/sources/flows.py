"""US spot BTC ETF flows (Farside), Fear & Greed (Alternative.me), CFTC TFF (CME Bitcoin).

Farside (design 4.1): the fund universe comes from the header row; parentheses
are negative, only an explicit 0.0 is zero, blank/'-'/N/A are null.
``known_sum`` covers numeric funds only; ``validated_total`` exists only when
every fund is numeric and the provider total matches within tolerance.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser
import re

from btc.fetch import FetchError, Fetcher, SourceResult
from btc.sources.base import from_s, json_of, num, ok

UTC = timezone.utc
FARSIDE_URL = 'https://farside.co.uk/btc/'
TICKER = re.compile(r'^[A-Z]{2,6}$')


class _Tables(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.row, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            self.tables.append([])
        elif tag == 'tr' and self.tables:
            self.row = []
        elif tag in ('td', 'th') and self.row is not None:
            self.cell = []

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.cell is not None and self.row is not None:
            self.row.append(' '.join(''.join(self.cell).split()))
            self.cell = None
        elif tag == 'tr' and self.row is not None and self.tables:
            self.tables[-1].append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


def farside_cell(text: str):
    """'(60.7)' -> -60.7, '0.0' -> 0.0, '' / '-' / 'N/A' -> None."""
    text = (text or '').replace(',', '').replace('\xa0', '').strip()
    if text in ('', '-', '–', 'N/A', 'n/a'):
        return None
    negative = text.startswith('(') and text.endswith(')')
    text = text.strip('()')
    try:
        value = float(text)
    except ValueError:
        return None
    return -value if negative else value


def parse_farside(body: str) -> dict:
    parser = _Tables()
    parser.feed(body)
    for table in parser.tables:
        header = next((row for row in table if sum(1 for c in row if TICKER.match(c)) >= 5), None)
        if header is None:
            continue
        universe = [(i, c) for i, c in enumerate(header) if TICKER.match(c)]
        total_index = next((i for i, row in enumerate(table[0]) if row == 'Total'), len(header) - 1)
        rows = []
        for row in table:
            try:
                day = datetime.strptime(row[0], '%d %b %Y').date()
            except (ValueError, IndexError):
                continue  # header, fee, Total/Average/Maximum/Minimum rows
            funds = {ticker: farside_cell(row[i]) if i < len(row) else None for i, ticker in universe}
            reported = farside_cell(row[total_index]) if total_index < len(row) else None
            numeric = [v for v in funds.values() if v is not None]
            complete = len(numeric) == len(funds)
            known_sum = round(sum(numeric), 4) if numeric else None
            tolerance = 0.05 * (len(funds) + 1)
            conflict = complete and reported is not None and abs(known_sum - reported) > tolerance
            rows.append({'trade_date': day.isoformat(), 'funds': funds, 'reported_total_musd': reported,
                         'known_sum_musd': known_sum, 'table_complete': complete,
                         'validated_total_musd': reported if complete and reported is not None and not conflict else None,
                         'conflict': conflict, 'missing_funds': [t for t, v in funds.items() if v is None]})
        if rows:
            rows.sort(key=lambda r: r['trade_date'])
            return {'universe': [t for _, t in universe], 'rows': rows}
    raise FetchError('missing_field')


def farside(f: Fetcher) -> SourceResult:
    r = f.get(FARSIDE_URL, timeout=15)
    if b'<table' not in r.body:
        raise FetchError('invalid_payload')  # challenge page or layout change
    parsed = parse_farside(r.text())
    # Trade dates only: no publication time is given (timestamp_quality=source_date).
    return ok('farside', [r], {**parsed, 'unit': 'USD million', 'publication_status': 'provisional'},
              timestamp_quality='source_date')


def fgi(f: Fetcher) -> SourceResult:
    r = f.get('https://api.alternative.me/fng/', params={'limit': 400, 'format': 'json'})
    data = json_of(r)
    if (data.get('metadata') or {}).get('error'):
        raise FetchError('invalid_payload')
    series = []
    for row in data['data']:
        value = num(row['value'])
        if not 0 <= value <= 100:
            raise FetchError('invalid_payload')
        series.append({'t': from_s(row['timestamp']), 'value': value, 'label': str(row.get('value_classification', ''))[:40]})
    series.sort(key=lambda x: x['t'])
    if not series:
        raise FetchError('empty_payload')
    return ok('fgi', [r], {'series': series, 'attribution': 'Alternative.me'}, observed_at=series[-1]['t'])


COT_FIELDS = ('open_interest_all', 'asset_mgr_positions_long', 'asset_mgr_positions_short',
              'lev_money_positions_long', 'lev_money_positions_short',
              'dealer_positions_long_all', 'dealer_positions_short_all')


def cftc_tff(f: Fetcher) -> SourceResult:
    r = f.get('https://publicreporting.cftc.gov/resource/gpe5-46if.json', params={
        '$where': "cftc_contract_market_code='133741'", '$order': 'report_date_as_yyyy_mm_dd DESC', '$limit': 160})
    rows = []
    for row in json_of(r):
        if row.get('cftc_contract_market_code') != '133741' or 'BITCOIN' not in str(row.get('market_and_exchange_names', '')):
            continue
        item = {'report_date': str(row['report_date_as_yyyy_mm_dd'])[:10]}
        for key in COT_FIELDS:
            item[key] = num(row[key])
        if item['open_interest_all'] <= 0:
            raise FetchError('invalid_payload')
        rows.append(item)
    if not rows:
        raise FetchError('empty_payload')
    rows.sort(key=lambda x: x['report_date'])
    return ok('cftc_tff', [r], {'report': 'TFF Futures Only', 'contract_code': '133741',
                                'market': 'BITCOIN - CHICAGO MERCANTILE EXCHANGE', 'rows': rows, 'unit': 'contracts'},
              timestamp_quality='source_date')
