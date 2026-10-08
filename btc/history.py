"""Job-owned observation history: append-only JSONL under ``<root>/history/``.

- One file per series (source, instrument, metric).
- Unique key (P8): source + instrument + metric + observed_at + revision (+ trade_date when the
  record has one, e.g. ETF arrival records).
  A repeated key is not counted twice; a repeated key with a different value
  keeps the first record and is reported as a conflict (a provider revision
  must use a new ``revision``).
- Files are rewritten through a temporary file and ``os.replace`` under a lock;
  existing records are never removed or edited.
- Percentiles and z-scores always carry their comparison window and sample
  count. Too few observations give ``status: insufficient_history`` with the
  label 「履歴不足」 and no number.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
import fcntl
import json
import math
from pathlib import Path
import re
import statistics

from btc.common import UTC, ensure_dir, now_utc, parse_time, write_text

INSUFFICIENT_LABEL = '履歴不足'
DEFAULT_MIN_COUNT = 20
_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$')
REQUIRED = ('source', 'instrument', 'metric', 'observed_at', 'value')


def _check_name(value, field):
    if not isinstance(value, str) or not _NAME.match(value) or '..' in value:
        raise ValueError(f'invalid_history_{field}')
    return value


def _number(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('invalid_history_value')
    return value


def normalize_record(record: dict, recorded_at: str | None = None) -> dict:
    missing = [key for key in REQUIRED if key not in record]
    if missing:
        raise ValueError('history_record_missing_field')
    out = dict(record)
    for key in ('source', 'instrument', 'metric'):
        _check_name(out[key], key)
    out['observed_at'] = parse_time(out['observed_at']).astimezone(UTC).isoformat()
    if out.get('retrieved_at') is not None:
        out['retrieved_at'] = parse_time(out['retrieved_at']).isoformat()
    revision = out.get('revision', 0)
    if isinstance(revision, bool) or not isinstance(revision, (int, str)):
        raise ValueError('invalid_history_revision')
    out['revision'] = revision
    out['value'] = _number(out['value'])
    out['recorded_at'] = recorded_at or now_utc().isoformat()
    return out


def record_key(record: dict) -> tuple:
    # trade_date (ETF arrival records) is part of the identity: one fetch can see several trade dates (R-08).
    return (record['source'], record['instrument'], record['metric'],
            parse_time(record['observed_at']).astimezone(UTC).isoformat(), str(record.get('revision', 0)),
            str(record.get('trade_date') or ''))


class History:
    def __init__(self, directory):
        self.directory = Path(directory)

    def path(self, source: str, instrument: str, metric: str) -> Path:
        return self.directory / f'{_check_name(source, "source")}__{_check_name(instrument, "instrument")}__{_check_name(metric, "metric")}.jsonl'

    @contextmanager
    def _lock(self):
        ensure_dir(self.directory)
        with (self.directory / '.lock').open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    @staticmethod
    def _read(path: Path) -> list[dict]:
        if not path.is_file():
            return []
        records = []
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.strip():
                records.append(json.loads(line))
        return records

    def read(self, source: str, instrument: str, metric: str) -> list[dict]:
        return self._read(self.path(source, instrument, metric))

    def append(self, records: list[dict], recorded_at: str | None = None) -> dict:
        """Add new observations. Returns counts; never removes existing lines."""
        stamp = recorded_at or now_utc().isoformat()
        normalized = [normalize_record(r, stamp) for r in records]
        grouped: dict[Path, list[dict]] = {}
        for record in normalized:
            grouped.setdefault(self.path(record['source'], record['instrument'], record['metric']), []).append(record)
        summary = {'added': 0, 'duplicates': 0, 'conflicts': 0, 'conflict_keys': []}
        with self._lock():
            for path, items in grouped.items():
                existing = self._read(path)
                known = {record_key(r): r for r in existing}
                fresh = []
                for item in items:
                    key = record_key(item)
                    if key in known:
                        if known[key].get('value') == item.get('value'):
                            summary['duplicates'] += 1
                        else:
                            summary['conflicts'] += 1
                            summary['conflict_keys'].append(list(key))
                        continue
                    known[key] = item
                    fresh.append(item)
                if fresh:
                    old = path.read_bytes().decode('utf-8') if path.is_file() else ''
                    if old and not old.endswith('\n'):
                        old += '\n'
                    new = ''.join(json.dumps(r, ensure_ascii=False, allow_nan=False, sort_keys=True) + '\n' for r in fresh)
                    write_text(path, old + new)
                    summary['added'] += len(fresh)
        return summary

    def series(self, source: str, instrument: str, metric: str) -> list[dict]:
        """One value per observation time: the most recently recorded revision."""
        latest: dict[str, dict] = {}
        for record in self.read(source, instrument, metric):
            latest[record['observed_at']] = record  # file order == record order
        return sorted(latest.values(), key=lambda r: parse_time(r['observed_at']))

    def stats(self, source: str, instrument: str, metric: str, value, *, as_of,
              window_days: float | None = None, window_count: int | None = None,
              min_count: int = DEFAULT_MIN_COUNT) -> dict:
        return comparison_stats(self.series(source, instrument, metric), value, as_of=as_of,
                                window_days=window_days, window_count=window_count, min_count=min_count,
                                metric=f'{source}/{instrument}/{metric}')


def comparison_stats(series: list[dict], value, *, as_of, window_days: float | None = None,
                     window_count: int | None = None, min_count: int = DEFAULT_MIN_COUNT,
                     metric: str | None = None) -> dict:
    """Percentile (mid-rank) and z-score of ``value`` against earlier observations.

    The comparison sample holds observations strictly before ``as_of`` (the
    current observation is not compared with itself), restricted to the last
    ``window_days`` days or the last ``window_count`` observations (exactly one).
    """
    if (window_days is None) == (window_count is None):
        raise ValueError('exactly_one_window')
    if min_count < 2:
        raise ValueError('min_count_too_small')
    end = parse_time(as_of).astimezone(UTC)
    earlier = [r for r in series if r.get('value') is not None and parse_time(r['observed_at']) < end]
    if window_days is not None:
        start = end - timedelta(days=window_days)
        sample = [r for r in earlier if parse_time(r['observed_at']) >= start]
        window = {'kind': 'days', 'size': window_days, 'start': start.isoformat(), 'end': end.isoformat()}
    else:
        sample = earlier[-window_count:] if window_count > 0 else []
        window = {'kind': 'count', 'size': window_count,
                  'start': sample[0]['observed_at'] if sample else None, 'end': end.isoformat()}
    values = [r['value'] for r in sample]
    result = {'metric': metric, 'value': value, 'window': window, 'count': len(values),
              'min_count': min_count, 'status': 'ok', 'label': None,
              'percentile': None, 'zscore': None, 'mean': None, 'stdev': None,
              'method': 'mid-rank percentile and sample z-score against earlier observations only'}
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        result.update(status='value_unavailable', label='値なし')
        return result
    if len(values) < min_count:
        result.update(status='insufficient_history', label=INSUFFICIENT_LABEL)
        return result
    below = sum(1 for v in values if v < value)
    equal = sum(1 for v in values if v == value)
    mean = statistics.fmean(values)
    stdev = statistics.stdev(values)
    result.update(percentile=round((below + 0.5 * equal) / len(values) * 100, 1),
                  mean=mean, stdev=stdev)
    if stdev == 0:
        result.update(status='zero_variance', label='変動なし（Zスコア算出不可）')
    else:
        result['zscore'] = round((value - mean) / stdev, 2)
    return result


def describe_window(stats: dict) -> str:
    window = stats['window']
    span = f'過去{window["size"]:g}日' if window['kind'] == 'days' else f'直近{window["size"]}件'
    return f'{span}・n={stats["count"]}'


def describe(stats: dict) -> str:
    """Japanese label that always shows the window and count."""
    if stats['status'] == 'insufficient_history':
        return f'{INSUFFICIENT_LABEL}（{describe_window(stats)}、必要{stats["min_count"]}件）'
    if stats['status'] == 'value_unavailable':
        return f'値なし（{describe_window(stats)}）'
    text = f'百分位 {stats["percentile"]:g}'
    if stats['zscore'] is not None:
        text += f'・Z {stats["zscore"]:+.2f}'
    else:
        text += '・Z 算出不可（変動なし）'
    return f'{text}（{describe_window(stats)}）'
