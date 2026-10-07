"""btc.history: append-only JSONL, dedup by unique key, windowed statistics."""
from datetime import datetime, timedelta, timezone

import pytest

from btc.history import History, comparison_stats, describe

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def rec(day, value, revision=0, metric='funding'):
    return {'source': 'binance', 'instrument': 'BTCUSDT', 'metric': metric,
            'observed_at': (T0 + timedelta(days=day)).isoformat(), 'value': value, 'unit': '%', 'revision': revision}


def test_append_dedups_and_never_rewrites_existing_lines(tmp_path):
    h = History(tmp_path)
    assert h.append([rec(0, 1.0), rec(1, 2.0)])['added'] == 2
    path = h.path('binance', 'BTCUSDT', 'funding')
    before = path.read_text()
    result = h.append([rec(0, 1.0), rec(1, 9.0), rec(2, 3.0)])
    assert result['added'] == 1 and result['duplicates'] == 1 and result['conflicts'] == 1
    after = path.read_text()
    assert after.startswith(before) and len(after.splitlines()) == 3
    assert oct(path.stat().st_mode & 0o777) == '0o600'


def test_revision_is_part_of_the_key_and_latest_revision_wins(tmp_path):
    h = History(tmp_path)
    h.append([rec(0, 1.0)])
    assert h.append([rec(0, 1.5, revision=1)])['added'] == 1
    series = h.series('binance', 'BTCUSDT', 'funding')
    assert len(series) == 1 and series[0]['value'] == 1.5


def test_equivalent_offsets_are_one_observation(tmp_path):
    h = History(tmp_path)
    h.append([rec(0, 1.0)])
    jst = rec(0, 1.0)
    jst['observed_at'] = (T0 + timedelta(hours=9)).replace(tzinfo=None).isoformat() + '+09:00'
    assert h.append([jst])['duplicates'] == 1


def test_naive_time_and_bad_names_rejected(tmp_path):
    h = History(tmp_path)
    bad = rec(0, 1.0)
    bad['observed_at'] = '2026-09-01T00:00:00'
    with pytest.raises(ValueError):
        h.append([bad])
    bad = rec(0, 1.0)
    bad['source'] = '../x'
    with pytest.raises(ValueError):
        h.append([bad])
    nan = rec(0, float('nan'))
    with pytest.raises(ValueError):
        h.append([nan])


def test_insufficient_history_has_no_number():
    series = [rec(d, float(d)) for d in range(5)]
    stats = comparison_stats(series, 10.0, as_of=(T0 + timedelta(days=5)).isoformat(), window_days=30, min_count=20)
    assert stats['status'] == 'insufficient_history' and stats['percentile'] is None and stats['zscore'] is None
    assert stats['count'] == 5 and stats['window']['kind'] == 'days'
    assert describe(stats) == '履歴不足（過去30日・n=5、必要20件）'


def test_percentile_and_z_exclude_current_and_future():
    series = [rec(d, float(d)) for d in range(30)]  # 0..29
    as_of = (T0 + timedelta(days=20)).isoformat()   # sample: days 0..19
    stats = comparison_stats(series, 10.0, as_of=as_of, window_count=20, min_count=10)
    assert stats['count'] == 20 and stats['percentile'] == 52.5  # 10 below + half of one equal
    assert stats['zscore'] == pytest.approx(round((10 - 9.5) / 5.9160797831, 2))
    assert '直近20件・n=20' in describe(stats)
    window = comparison_stats(series, 10.0, as_of=as_of, window_days=5, min_count=2)
    assert window['count'] == 5


def test_zero_variance_and_window_choice():
    series = [rec(d, 1.0) for d in range(25)]
    stats = comparison_stats(series, 1.0, as_of=(T0 + timedelta(days=30)).isoformat(), window_days=60)
    assert stats['status'] == 'zero_variance' and stats['zscore'] is None and stats['percentile'] == 50.0
    with pytest.raises(ValueError):
        comparison_stats(series, 1.0, as_of=T0.isoformat())
