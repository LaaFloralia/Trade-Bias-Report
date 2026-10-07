"""Official event calendar: FOMC (federalreserve.gov), BEA release schedule, BLS verified cache.

Times are converted with zoneinfo. FOMC pages give dates only: the statement
(14:00 ET) and press conference (14:30 ET) times are the Fed's standing
practice and marked ``tentative``. Anything past a source's coverage is
``unknown``, never "no events".
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from btc.fetch import FetchError, Fetcher, SourceResult
from btc.sources.base import ok, z

UTC = timezone.utc
NY = ZoneInfo('America/New_York')
CACHE = Path(__file__).resolve().parent.parent / 'data' / 'official_calendar_cache.json'
MONTHS = {m: i for i, m in enumerate(('January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
                                       'September', 'October', 'November', 'December'), 1)}
BEA_KEEP = re.compile(r'^(GDP \(|Personal Income and Outlays,)')


def _event(ident, name, local: datetime, *, category, importance, status, source_id, source_url, end=None):
    return {'id': ident, 'name': name, 'category': category, 'start_at': z(local), 'end_at': z(end) if end else None,
            'timezone': 'America/New_York', 'importance': importance, 'status': status,
            'source_id': source_id, 'source_url': source_url}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        if data.strip():
            self.parts.append(' '.join(data.split()))


def parse_fomc(body: str, today: date) -> list[dict]:
    parser = _Text()
    parser.feed(body)
    tokens = parser.parts
    events, year = [], None
    i = 0
    while i < len(tokens):
        heading = re.fullmatch(r'(\d{4}) FOMC Meetings', tokens[i])
        if heading:
            year = int(heading.group(1))
        elif year and i + 1 < len(tokens):
            month = re.fullmatch(r'([A-Z][a-z]+)(?:/([A-Z][a-z]+))?', tokens[i])
            days = re.fullmatch(r'(\d{1,2})-(\d{1,2})\*?', tokens[i + 1])
            if month and days and month.group(1) in MONTHS:
                last_month = MONTHS[month.group(2) or month.group(1)]
                last_day = int(days.group(2))
                try:
                    day = date(year, last_month, last_day)
                except ValueError:
                    i += 1
                    continue
                if today - timedelta(days=2) <= day <= today + timedelta(days=120):
                    statement = datetime(day.year, day.month, day.day, 14, 0, tzinfo=NY)
                    url = 'https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm'
                    events.append(_event(f'fomc.statement.{day.isoformat()}', 'FOMC 声明（政策金利の発表）', statement,
                                         category='monetary_policy', importance='high', status='tentative',
                                         source_id='fed_calendar', source_url=url))
                    events.append(_event(f'fomc.press.{day.isoformat()}', 'FOMC 議長会見', statement + timedelta(minutes=30),
                                         category='monetary_policy', importance='high', status='tentative',
                                         source_id='fed_calendar', source_url=url))
                i += 2
                continue
        i += 1
    return events


class _BeaRows(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.row, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self.row = []
        elif tag == 'td' and self.row is not None:
            self.cell = []

    def handle_endtag(self, tag):
        if tag == 'td' and self.cell is not None:
            self.row.append(' '.join(''.join(self.cell).split()))
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


def parse_bea(body: str, today: date) -> list[dict]:
    parser = _BeaRows()
    parser.feed(body)
    events = []
    for row in parser.rows:
        if len(row) < 3:
            continue
        when = re.fullmatch(r'([A-Z][a-z]+) (\d{1,2}) (\d{1,2}):(\d{2}) (AM|PM)', row[0])
        title = row[-1]
        if not when or not BEA_KEEP.match(title) or when.group(1) not in MONTHS:
            continue
        hour = int(when.group(3)) % 12 + (12 if when.group(5) == 'PM' else 0)
        year = today.year
        try:
            day = date(year, MONTHS[when.group(1)], int(when.group(2)))
        except ValueError:
            continue
        if day < today - timedelta(days=180):
            day = day.replace(year=year + 1)  # schedule pages list the coming months without a year
        local = datetime(day.year, day.month, day.day, hour, int(when.group(4)), tzinfo=NY)
        kind = 'gdp' if title.upper().startswith('GDP') else 'pce'
        name = 'GDP（BEA）' if kind == 'gdp' else 'PCE・個人所得支出（BEA）'
        events.append(_event(f'bea.{kind}.{day.isoformat()}', f'{name} {title[:80]}', local, category='macro_release',
                             importance='high', status='confirmed', source_id='bea_calendar',
                             source_url='https://www.bea.gov/news/schedule'))
    return events


def load_cache(path: Path = CACHE) -> dict:
    data = json.loads(path.read_text(encoding='utf-8'))
    events = []
    for item in data['events']:
        local = datetime.fromisoformat(f'{item["date"]}T{item["time"]}').replace(tzinfo=ZoneInfo(item['timezone']))
        event = _event(item['id'], f'{item["name"]} {item["period"]}', local, category=item['category'],
                       importance=item['importance'], status='confirmed', source_id=data['source_id'],
                       source_url=item['source_url'])
        event['verified_at'] = data['verified_at']
        event['verified_by'] = data['verified_by']
        events.append(event)
    return {'events': events, 'verified_at': data['verified_at'], 'coverage_end': data['coverage_end'],
            'source_id': data['source_id']}


def fed_calendar(f: Fetcher, today: date | None = None) -> SourceResult:
    today = today or datetime.now(UTC).date()
    r = f.get('https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm')
    events = parse_fomc(r.text(), today)
    if not events:
        raise FetchError('missing_field')
    return ok('fed_calendar', [r], {'events': events, 'coverage_end': (today + timedelta(days=120)).isoformat()},
              timestamp_quality='source_date')


def bea_calendar(f: Fetcher, today: date | None = None) -> SourceResult:
    today = today or datetime.now(UTC).date()
    r = f.get('https://www.bea.gov/news/schedule')
    events = parse_bea(r.text(), today)
    if not events:
        raise FetchError('missing_field')
    end = max(e['start_at'] for e in events)[:10]
    return ok('bea_calendar', [r], {'events': events, 'coverage_end': end}, timestamp_quality='source_date')
