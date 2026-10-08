"""Regression tests for Astra review 4, R4-01: incident closures are evaluated as of the edition's as_of.
Fixed times; no network."""
from datetime import timedelta
import json

import pytest

from btc import carry, scoring
from btc.pipeline import BtcStages
from tests.btc.test_pipeline import context, load_collection
from tests.btc.test_review_fixes import _assess, _edition_facts, _item, as_of_of

SECOND = timedelta(seconds=1)


@pytest.fixture(scope='module')
def built(tmp_path_factory):
    collection = load_collection()
    ctx = context(tmp_path_factory.mktemp('job'), collection)
    return collection, BtcStages().build_facts(collection, ctx), ctx


def _hack(t0):
    return _item('n1', 'Exchange halts bitcoin withdrawals after security breach', 'https://status.example.org/i1',
                 t0 - timedelta(hours=1), 'd' * 64)


def _critical(item):
    return _assess(item, impact='adverse', importance='critical', affected_source_ids=['binance_derivatives'])


def _opened(facts, hist):
    """Open incident recorded by the edition at the fixture as_of (2026-10-07T12:25:06.491602Z)."""
    t0 = as_of_of(facts)
    hack = _hack(t0)
    f = _edition_facts(facts, hist, t0, [hack])
    ev = scoring.evaluate(f, mode='daily', assessments=[_critical(hack)])
    carry.record_incidents(hist, f, {}, ev, edition_id='e1')
    return t0, ev['incidents_new'][0]['incident_id']


def _close(kind, facts, hist, incident_id, closed):
    if kind == 'manual':
        carry.release_manually(hist, incident_id, '社長指示', now=closed)
    else:
        f = _edition_facts(facts, hist, closed, [])
        carry.record_incidents(hist, f, {}, {'incidents_new': [], 'incidents_released': [
            {'incident_id': incident_id, 'news_id': 'n7', 'fact_ids': ['x']}]}, edition_id='e2')
    record = carry.incidents(hist)[0]
    assert carry.closed_at(record) == closed
    return record


@pytest.mark.parametrize('kind', ['manual', 'auto'])
@pytest.mark.parametrize('article', [True, False])
@pytest.mark.parametrize('offset,holds', [(-SECOND, True), (timedelta(0), False), (SECOND, False)])
def test_r4_01_closure_applies_only_from_its_time(built, tmp_path, kind, article, offset, holds):
    _, facts, _ = built
    hist = tmp_path / 'history'
    t0, incident_id = _opened(facts, hist)
    closed = t0 + timedelta(minutes=5)  # 12:30:06.491602Z
    _close(kind, facts, hist, incident_id, closed)
    hack = _hack(t0)
    f = _edition_facts(facts, hist, closed + offset, [hack] if article else [])
    ev = scoring.evaluate(f, mode='daily', assessments=[_critical(hack)] if article else [])
    incidents = f['state']['incidents']
    if holds:
        assert [i['incident_id'] for i in incidents['open']] == [incident_id] and incidents['released_ids'] == []
        assert set(incidents['open'][0]) == {'incident_id', 'news_id', 'event_cluster_id', 'url', 'title', 'published_at',
                                             'affected_source_ids', 'recorded_edition_id', 'recorded_as_of'}
        assert ev['trade_gate']['status'] == 'incident_hold'
        assert incidents['recently_released'] == []
    else:
        assert incidents['open'] == [] and incidents['released_ids'] == [incident_id]
        assert ev['trade_gate']['status'] != 'incident_hold' and ev['incidents_new'] == []
        assert [r['incident_id'] for r in incidents['recently_released']] == ([incident_id] if kind == 'manual' else [])


@pytest.mark.parametrize('offset,shown', [(-SECOND, False), (timedelta(0), True), (timedelta(days=14), True),
                                          (timedelta(days=14) + SECOND, False)])
def test_r4_01_recently_released_window_is_inclusive_at_both_ends(built, tmp_path, offset, shown):
    _, facts, _ = built
    hist = tmp_path / 'history'
    t0, incident_id = _opened(facts, hist)
    closed = t0 + timedelta(minutes=5)
    _close('manual', facts, hist, incident_id, closed)
    f = _edition_facts(facts, hist, closed + offset, [])
    assert [r['incident_id'] for r in f['state']['incidents']['recently_released']] == ([incident_id] if shown else [])


def test_r4_01_write_path_never_touches_a_closed_record(built, tmp_path):
    """An edition before the release still sees the incident open; its acceptance cannot rewrite the closed record."""
    _, facts, _ = built
    hist = tmp_path / 'history'
    t0, incident_id = _opened(facts, hist)
    closed = t0 + timedelta(minutes=5)
    record = _close('manual', facts, hist, incident_id, closed)
    f = _edition_facts(facts, hist, closed - SECOND, [_hack(t0)])
    assert [i['incident_id'] for i in f['state']['incidents']['open']] == [incident_id]
    out = carry.record_incidents(hist, f, {}, {
        'incidents_new': [dict(f['state']['incidents']['open'][0])],
        'incidents_released': [{'incident_id': incident_id, 'news_id': 'n7', 'fact_ids': ['x']}]}, edition_id='e3')
    assert out['opened'] == []
    assert carry.incidents(hist) == [record]
    assert json.loads((hist / carry.INCIDENT_FILE).read_text())['records'][0]['status'] == 'released_manually'


def test_r4_01_rebuilding_a_saved_collection_after_a_later_release_keeps_the_hold(built, tmp_path):
    collection, facts, _ = built
    ctx = context(tmp_path / 'job', collection)
    t0, incident_id = _opened(facts, ctx.history_dir)
    carry.release_manually(ctx.history_dir, incident_id, '社長指示', now=t0 + timedelta(minutes=5))
    rebuilt = BtcStages().build_facts(collection, ctx)
    assert rebuilt['as_of'] and as_of_of(rebuilt) == t0
    assert [i['incident_id'] for i in rebuilt['state']['incidents']['open']] == [incident_id]
    assert rebuilt['state']['incidents']['released_ids'] == [] and rebuilt['state']['incidents']['recently_released'] == []
    assert scoring.evaluate(rebuilt, mode='daily', assessments=[])['trade_gate']['status'] == 'incident_hold'
