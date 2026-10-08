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


MONTH_START = re.compile(r'([A-Z][a-z]+)(?:/([A-Z][a-z]+))?\b')
# Entries the Fed lists inside a year section that are not scheduled meetings: a single day with a parenthetical
# such as "22 (notation vote)" or "(unscheduled)". They carry no statement time, so they are excluded explicitly.
FOMC_NON_MEETING = re.compile(r'\d{1,2} \((notation vote|unscheduled)\)')


def _fomc_days(year: int, first: int, last: int, token: str):
    """(start, end) dates of "D-D(*)", or None when the days do not form a valid meeting."""
    days = FOMC_DAYS.fullmatch(token)
    if not days:
        return None
    try:
        begin = date(year, first, int(days.group(1)))
        finish = date(year, last, int(days.group(2)))
    except ValueError:
        return None
    return (begin, finish) if begin < finish <= begin + timedelta(days=3) else None


def parse_fomc_detail(body: str, today: date) -> tuple[list[dict], dict]:
    """FOMC meetings plus parse statistics (R2-05, R3-04).

    Inside a "YYYY FOMC Meetings" section, any text token that STARTS with a month name (or Month/Month) is a
    meeting candidate; it is in range when its month overlaps today-2 .. today+120. It counts as parsed only
    when the token is exactly the month name(s) and the next token is a valid "D-D" (optionally "*") meeting;
    anything else (e.g. "October 6 – 7*" in one token, "99-7*") is unparsed. A day marked "(notation vote)" or
    "(unscheduled)" is not a scheduled meeting and is excluded explicitly. ``coverage_end`` comes only from
    parsed structure: Dec 31 of the latest year with a parsed meeting, capped at the 120-day window; yesterday
    when the current year has none.
    """
    parser = _Text()
    parser.feed(body)
    tokens = parser.parts
    start, end = today - timedelta(days=2), today + timedelta(days=120)
    events, unparsed, excluded, parsed_years = [], [], [], set()
    candidates = parsed = 0
    year = None
    url = 'https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm'
    for i, token in enumerate(tokens):
        heading = re.fullmatch(r'(\d{4}) FOMC Meetings', token)
        if heading:
            year = int(heading.group(1))
            continue
        month = MONTH_START.match(token)
        if not year or not month or month.group(1) not in MONTHS or (month.group(2) and month.group(2) not in MONTHS):
            continue
        first, last = MONTHS[month.group(1)], MONTHS[month.group(2) or month.group(1)]
        in_range = _month_overlaps(year, first, start, end) or _month_overlaps(year, last, start, end)
        following = tokens[i + 1] if i + 1 < len(tokens) else ''
        exact = MONTH_TOKEN.fullmatch(token) is not None
        if exact and FOMC_NON_MEETING.fullmatch(following):
            excluded.append(f'{year} {token} {following}'[:80])
            continue
        candidates += in_range
        meeting = _fomc_days(year, first, last, following) if exact else None
        if meeting is None:
            if in_range:
                unparsed.append(f'{year} {token} {following if exact else ""}'.strip()[:80])
            continue
        day = meeting[1]
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
             'excluded_non_meetings': excluded, 'parsed_years': sorted(parsed_years), 'coverage_end': coverage_end}
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


def _bea_when(cell: str, today: date):
    """(date, hour24, minute) of "Month D H:MM AM|PM" with a valid date, hour 1-12 and minute 00-59, else None."""
    when = BEA_WHEN.fullmatch(cell)
    if not when or when.group(1) not in MONTHS:
        return None
    hour, minute = int(when.group(3)), int(when.group(4))
    if not (1 <= hour <= 12 and 0 <= minute <= 59):
        return None
    try:
        day = date(today.year, MONTHS[when.group(1)], int(when.group(2)))
    except ValueError:
        return None
    if day < today - timedelta(days=180):
        day = day.replace(year=today.year + 1)  # schedule pages list the coming months without a year
    return day, hour % 12 + (12 if when.group(5) == 'PM' else 0), minute


def parse_bea_detail(body: str, today: date) -> tuple[list[dict], dict]:
    """BEA GDP/PCE releases plus parse statistics (R2-05, R3-04).

    Every table row with a GDP/PCE release title in any cell is a candidate. It is unparsed when it has fewer
    than the three cells (date, type, title) or its date cell is not a valid "Month D H:MM AM|PM" (hour 1-12,
    minute 00-59). "To Be Announced" rows are counted separately; they carry no date. ``coverage_end`` is the
    latest successfully parsed release date of any title.
    """
    parser = _BeaRows()
    parser.feed(body)
    events, unparsed, tba, dated = [], [], 0, []
    candidates = 0
    for row in parser.rows:
        keep = any(BEA_KEEP.match(cell) for cell in row)
        candidates += keep
        if keep and row[0].startswith('To Be Announced'):
            tba += 1
            continue
        if len(row) < 3:
            if keep:
                unparsed.append(' | '.join(row)[:120])
            continue
        title = row[-1]
        when = _bea_when(row[0], today)
        if when is None:
            if keep:
                unparsed.append(f'{row[0]} | {title}'[:120])
            continue
        day, hour, minute = when
        dated.append(day)
        if not keep or not BEA_KEEP.match(title):
            if keep:
                unparsed.append(f'{row[0]} | {title}'[:120])  # release title not in the title cell
            continue
        local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=NY)
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
