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


FOMC_DAYS = re.compile(r'(\d{1,2})-(\d{1,2})\*?')
MONTH_TOKEN = re.compile(r'([A-Z][a-z]+)(?:/([A-Z][a-z]+))?')


def _month_overlaps(year: int, month: int, start: date, end: date) -> bool:
    first = date(year, month, 1)
    last = (date(year + (month == 12), month % 12 + 1, 1)) - timedelta(days=1)
    return first <= end and last >= start


def parse_fomc_detail(body: str, today: date) -> tuple[list[dict], dict]:
    """FOMC meetings plus parse statistics (R2-05).

    Every month token inside a "YYYY FOMC Meetings" section is a meeting candidate. A candidate whose month
    overlaps the target range (today-2 .. today+120) and whose day token is not "D-D" (optionally "*") is
    counted as unparsed. ``coverage_end`` comes only from parsed structure: Dec 31 of the latest year with at
    least one parsed meeting, capped at the 120-day window; yesterday when the current year has none.
    """
    parser = _Text()
    parser.feed(body)
    tokens = parser.parts
    start, end = today - timedelta(days=2), today + timedelta(days=120)
    events, unparsed, parsed_years = [], [], set()
    candidates = parsed = 0
    year = None
    url = 'https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm'
    for i, token in enumerate(tokens):
        heading = re.fullmatch(r'(\d{4}) FOMC Meetings', token)
        if heading:
            year = int(heading.group(1))
            continue
        month = MONTH_TOKEN.fullmatch(token)
        if not year or not month or month.group(1) not in MONTHS or (month.group(2) and month.group(2) not in MONTHS):
            continue
        last_month = MONTHS[month.group(2) or month.group(1)]
        in_range = _month_overlaps(year, last_month, start, end) or _month_overlaps(year, MONTHS[month.group(1)], start, end)
        candidates += in_range
        days = FOMC_DAYS.fullmatch(tokens[i + 1]) if i + 1 < len(tokens) else None
        try:
            day = date(year, last_month, int(days.group(2))) if days else None
        except ValueError:
            day = None
        if day is None:
            if in_range:
                unparsed.append(f'{year} {token} {tokens[i + 1] if i + 1 < len(tokens) else ""}'.strip()[:80])
            continue
        parsed_years.add(year)
        parsed += in_range
        if start <= day <= end:
            statement = datetime(day.year, day.month, day.day, 14, 0, tzinfo=NY)
            events.append(_event(f'fomc.statement.{day.isoformat()}', 'FOMC 声明（政策金利の発表）', statement,
                                 category='monetary_policy', importance='high', status='tentative',
                                 source_id='fed_calendar', source_url=url))
            events.append(_event(f'fomc.press.{day.isoformat()}', 'FOMC 議長会見', statement + timedelta(minutes=30),
                                 category='monetary_policy', importance='high', status='tentative',
                                 source_id='fed_calendar', source_url=url))
    if not parsed_years or max(parsed_years) < today.year:
        coverage_end = (today - timedelta(days=1)).isoformat()
    else:
        coverage_end = min(date(max(parsed_years), 12, 31), end).isoformat()
    stats = {'candidates_in_range': candidates, 'parsed_in_range': parsed, 'unparsed': unparsed,
             'parsed_years': sorted(parsed_years), 'coverage_end': coverage_end}
    return events, stats


def parse_fomc(body: str, today: date) -> list[dict]:
    return parse_fomc_detail(body, today)[0]


def fomc_coverage_end(body: str, today: date) -> str:
    """Coverage from parsed meetings only (see ``parse_fomc_detail``)."""
    return parse_fomc_detail(body, today)[1]['coverage_end']


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


BEA_WHEN = re.compile(r'([A-Z][a-z]+) (\d{1,2}) (\d{1,2}):(\d{2}) (AM|PM)')


def parse_bea_detail(body: str, today: date) -> tuple[list[dict], dict]:
    """BEA GDP/PCE releases plus parse statistics (R2-05).

    Every table row with a release title is a candidate. A GDP/PCE row whose date cell is not
    "Month D H:MM AM|PM" counts as unparsed ("To Be Announced" rows are counted separately; they carry no
    date). ``coverage_end`` is the latest successfully parsed release date of any title.
    """
    parser = _BeaRows()
    parser.feed(body)
    events, unparsed, tba, dated = [], [], 0, []
    candidates = 0
    for row in parser.rows:
        if len(row) < 3:
            continue
        title = row[-1]
        keep = bool(BEA_KEEP.match(title))
        candidates += keep
        when = BEA_WHEN.fullmatch(row[0])
        day = None
        if when and when.group(1) in MONTHS:
            year = today.year
            try:
                day = date(year, MONTHS[when.group(1)], int(when.group(2)))
            except ValueError:
                day = None
            if day and day < today - timedelta(days=180):
                day = day.replace(year=year + 1)  # schedule pages list the coming months without a year
        if day is None:
            if row[0].startswith('To Be Announced'):
                tba += keep
            elif keep:
                unparsed.append(f'{row[0]} | {title}'[:120])
            continue
        dated.append(day)
        if not keep:
            continue
        hour = int(when.group(3)) % 12 + (12 if when.group(5) == 'PM' else 0)
        local = datetime(day.year, day.month, day.day, hour, int(when.group(4)), tzinfo=NY)
        kind = 'gdp' if title.upper().startswith('GDP') else 'pce'
        name = 'GDP（BEA）' if kind == 'gdp' else 'PCE・個人所得支出（BEA）'
        events.append(_event(f'bea.{kind}.{day.isoformat()}', f'{name} {title[:80]}', local, category='macro_release',
                             importance='high', status='confirmed', source_id='bea_calendar',
                             source_url='https://www.bea.gov/news/schedule'))
    stats = {'candidates': candidates, 'parsed': len(events), 'unparsed': unparsed, 'to_be_announced': tba,
             'coverage_end': max(dated).isoformat() if dated else (today - timedelta(days=1)).isoformat()}
    return events, stats


def parse_bea(body: str, today: date) -> list[dict]:
    return parse_bea_detail(body, today)[0]


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
    events, stats = parse_fomc_detail(r.text(), today)
    if not events and not stats['parsed_years']:
        raise FetchError('missing_field')
    # Any unparsed in-range candidate makes the source partial -> next_24h unknown (R2-05).
    return ok('fed_calendar', [r], {'events': events, 'coverage_end': stats['coverage_end'], 'parse': stats},
              status='partial' if stats['unparsed'] else 'ok', timestamp_quality='source_date')


def bea_calendar(f: Fetcher, today: date | None = None) -> SourceResult:
    today = today or datetime.now(UTC).date()
    r = f.get('https://www.bea.gov/news/schedule')
    events, stats = parse_bea_detail(r.text(), today)
    if not events and not stats['candidates']:
        raise FetchError('missing_field')
    return ok('bea_calendar', [r], {'events': events, 'coverage_end': stats['coverage_end'], 'parse': stats},
              status='partial' if stats['unparsed'] else 'ok', timestamp_quality='source_date')
