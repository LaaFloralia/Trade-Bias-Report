"""Regression tests for the third independent review R3-01..R3-05 (Astra, 2026-10-08) and the manual-release
visibility rule. No network."""
import copy
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path

import pytest

from btc import analysis as analysis_mod, carry, facts as facts_mod, scoring, workflow
from btc.common import write_json
from btc.sources import calendar as cal
from btc.pipeline import BtcStages
from tests.btc.test_pipeline import context, load_collection
from tests.btc.test_review_fixes import (_assess, _calendar_fetcher, _edition, _edition_facts, _fixed_clock, _item,
                                         _oi_fact, _open_incident, as_of_of)

UTC = timezone.utc


@pytest.fixture(scope='module')
def built(tmp_path_factory):
    collection = load_collection()
    ctx = context(tmp_path_factory.mktemp('job'), collection)
    return collection, BtcStages().build_facts(collection, ctx), ctx


# ------------------------------------------------------------------ R3-01 body match beats URL match

def test_r3_01_old_body_reposted_at_a_follow_up_url_never_scores_again(built, tmp_path):
    _, facts, _ = built
    hist = tmp_path / 'history'
    t0 = as_of_of(facts)
    url = 'https://www.sec.gov/news/a'
    first = _item('n1', 'SEC approves in-kind creations for spot bitcoin ETFs', url, t0 - timedelta(hours=2), 'a' * 64)
    carry.record_known(hist, _edition_facts(facts, hist, t0, [first]), {'news_assessments': [_assess(first)]},
                       edition_id='e1', mode='daily')
    known_id = carry.known_news(hist, t0)[0]['known_id']
    # Same URL, new primary facts: an adopted follow-up with its own record.
    follow = _item('n2', 'SEC sets effective date for in-kind ETF creations', url, t0 + timedelta(hours=39), 'c' * 64)
    a2 = _assess(follow, known_event_ids=[known_id], follow_up_new_facts=True)
    f2 = _edition_facts(facts, hist, t0 + timedelta(hours=40), [follow])
    assert scoring.evaluate(f2, mode='daily', assessments=[a2])['groups']['btc_specific_event']['direction'] == 1
    assert carry.record_known(hist, f2, {'news_assessments': [a2]}, edition_id='e2', mode='daily') == 1
    assert len(carry.known_news(hist, t0 + timedelta(hours=40))) == 2
    # t+42 h: the original body re-posted at the same URL, with and without the follow-up flag.
    repost = _item('n3', 'SEC approves in-kind creations (republished)', url, t0 + timedelta(hours=41), 'a' * 64)
    f3 = _edition_facts(facts, hist, t0 + timedelta(hours=42), [repost])
    for a3 in (_assess(repost), _assess(repost, known_event_ids=[known_id], follow_up_new_facts=True)):
        g = scoring.evaluate(f3, mode='daily', assessments=[a3])['groups']['btc_specific_event']
        assert g['direction'] == 0, a3
        assert carry.record_known(hist, f3, {'news_assessments': [a3]}, edition_id='e3', mode='daily') == 0
        assert len(carry.known_news(hist, t0 + timedelta(hours=42))) == 2
    # The body tier picks the original even though the URL also matches the newer follow-up.
    known = carry.known_news(hist, t0 + timedelta(hours=42))
    assert carry.best_match(carry.item_identifiers(repost), [], known)['known_id'] == known_id


# ------------------------------------------------------------------ R3-02 closed incidents stay closed + visibility

def test_r3_02_manual_release_then_same_article_stays_clear_and_is_visible(built, tmp_path, capsys, monkeypatch):
    collection, facts, _ = built
    root = tmp_path / 'job'
    hist = root / 'history'
    _, incident_id = _open_incident(built, hist)
    t0 = as_of_of(facts)
    # Released at the fixture as_of itself, so the rebuilt fixture edition below is at/after the release (R4-01).
    _fixed_clock(monkeypatch, t0)
    assert carry.main(['release', '--root', str(root), '--incident', incident_id, '--reason', '社長指示: 取引所の復旧を確認']) == 0
    capsys.readouterr()
    assert carry.main(['list', '--root', str(root)]) == 0
    assert json.loads(capsys.readouterr().out)['open_incidents'] == []
    # Next edition: the same incident article is still selected and assessed critical -> no new hold.
    hack = _item('n1', 'Exchange halts bitcoin withdrawals after security breach', 'https://status.example.org/i1',
                 t0 - timedelta(hours=1), 'd' * 64)
    f = _edition_facts(facts, hist, t0 + timedelta(hours=6), [hack])
    assert f['state']['incidents']['released_ids'] == [incident_id]
    a = _assess(hack, impact='adverse', importance='critical', affected_source_ids=['binance_derivatives'])
    ev = scoring.evaluate(f, mode='daily', assessments=[a])
    assert ev['trade_gate']['status'] != 'incident_hold' and ev['incidents_new'] == []
    assert carry.record_incidents(hist, f, {'news_assessments': [a]}, ev, edition_id='e2')['opened'] == []
    assert carry.main(['list', '--root', str(root)]) == 0
    assert json.loads(capsys.readouterr().out)['open_incidents'] == []
    assert carry.main(['release', '--root', str(root), '--incident', incident_id, '--reason', 'again']) == 2
    assert 'incident_not_open:released_manually' in capsys.readouterr().out
    # A recurrence is a new article -> a new incident_id -> hold.
    again = _item('n2', 'Exchange halts withdrawals again', 'https://status.example.org/i2', t0 + timedelta(hours=5), 'e' * 64)
    f2 = _edition_facts(facts, hist, t0 + timedelta(hours=6), [hack, again])
    ev2 = scoring.evaluate(f2, mode='daily', assessments=[
        a, _assess(again, impact='adverse', importance='critical', affected_source_ids=['binance_derivatives'])])
    assert ev2['trade_gate']['status'] == 'incident_hold'
    assert [i['incident_id'] for i in ev2['incidents_new']] == [scoring.incident_id(again)]

    # Visibility: the next edition's MD and HTML, and the parent briefing, show the release for 14 days.
    files = {name: (hist / name).read_bytes() for name in (carry.INCIDENT_FILE,) if (hist / name).is_file()}
    built_facts, ev3, md, html, _ = _edition(collection, tmp_path, 'next', history_files=files)
    shown = built_facts['state']['incidents']['recently_released']
    assert [r['incident_id'] for r in shown] == [incident_id] and shown[0]['released_by'] == 'owner_instruction'
    for text in (md, html):
        assert '障害の手動解除（解除から14日間表示）' in text and incident_id in text
        assert '社長指示: 取引所の復旧を確認' in text and 'owner_instruction' in text
    briefing = analysis_mod.briefing(built_facts, ev3, edition_id='x', mode='daily', session_slot='pm')
    assert '手動解除された障害' in briefing and incident_id in briefing
    released_at = datetime.fromisoformat(shown[0]['released_at'])
    state = {'news': {'items': []}}
    facts_mod.carry_state(state, hist, released_at + timedelta(days=15))
    assert state['incidents']['recently_released'] == [] and state['incidents']['released_ids'] == [incident_id]


# ------------------------------------------------------------------ R3-02b identity includes the publication time

HACK_URL = 'https://status.example.org/i1'
HACK_TITLE = 'Exchange halts bitcoin withdrawals after security breach'


def _hack(published, news_id='n1'):
    return _item(news_id, HACK_TITLE, HACK_URL, published, 'd' * 64)


def _critical(item):
    return _assess(item, impact='adverse', importance='critical', affected_source_ids=['binance_derivatives'])


def test_r3_02b_incident_id_is_stable_for_the_same_publication(built, tmp_path):
    _, facts, _ = built
    hist = tmp_path / 'history'
    t0 = as_of_of(facts)
    item = _hack(t0 - timedelta(hours=1))
    ids = []
    for hours in (0, 6, 30):  # the same item seen by three editions
        f = _edition_facts(facts, hist, t0 + timedelta(hours=hours), [copy.deepcopy(item)])
        ev = scoring.evaluate(f, mode='daily', assessments=[_critical(item)])
        ids.append([i['incident_id'] for i in ev['incidents_open']])
        carry.record_incidents(hist, f, {}, ev, edition_id=f'e{hours}')
    assert ids[0] == ids[1] == ids[2] == [scoring.incident_id(item)]
    assert len(carry.incidents(hist)) == 1


def test_r3_02b_identical_publication_stays_closed_after_a_recovery_release(built, tmp_path):
    _, facts, _ = built
    hist = tmp_path / 'history'
    t0 = as_of_of(facts)
    hack = _hack(t0 - timedelta(hours=1))
    f1 = _edition_facts(facts, hist, t0, [hack])
    ev1 = scoring.evaluate(f1, mode='daily', assessments=[_critical(hack)])
    incident_id = ev1['incidents_new'][0]['incident_id']
    carry.record_incidents(hist, f1, {}, ev1, edition_id='e1')
    official = _item('n7', 'Exchange resumes bitcoin withdrawals', 'https://status.example.org/r1',
                     t0 - timedelta(minutes=5), '1' * 64)
    f2 = _edition_facts(facts, hist, t0, [official])
    ev2 = scoring.evaluate(f2, mode='daily', assessments=[], recoveries=[
        {'incident_id': incident_id, 'news_id': 'n7', 'fact_ids': [_oi_fact(f2, 'binance_derivatives')]}])
    assert carry.record_incidents(hist, f2, {}, ev2, edition_id='e2')['released'] == [incident_id]
    f3 = _edition_facts(facts, hist, t0 + timedelta(hours=6), [hack])
    ev3 = scoring.evaluate(f3, mode='daily', assessments=[_critical(hack)])
    assert ev3['trade_gate']['status'] != 'incident_hold' and ev3['incidents_new'] == []


def test_r3_02b_same_title_and_url_published_later_is_a_new_hold(built, tmp_path, capsys):
    """A publisher updating the article in place (or re-posting it) after a release opens a new incident."""
    _, facts, _ = built
    job = workflow.Job(root=tmp_path / 'job', python=Path('/usr/bin/python3'))
    root = job.root
    _, old_id = _open_incident(built, job.history)
    assert carry.main(['release', '--root', str(root), '--incident', old_id, '--reason', '社長指示']) == 0
    released_at = datetime.fromisoformat(carry.incidents(job.history)[0]['released_at'])
    capsys.readouterr()
    t0 = as_of_of(facts)
    later = _hack(released_at + timedelta(hours=2), news_id='n5')
    as_of = released_at + timedelta(hours=3)
    f = _edition_facts(facts, job.history, as_of, [later])
    a = _critical(later)
    ev = scoring.evaluate(f, mode='daily', assessments=[a])
    new_id = scoring.incident_id(later)
    assert new_id != old_id and later['event_cluster_id'] == _hack(t0 - timedelta(hours=1))['event_cluster_id']
    assert ev['trade_gate']['status'] == 'incident_hold' and [i['incident_id'] for i in ev['incidents_new']] == [new_id]
    folder = job.reports / 'daily' / 'editions' / 'parent-daily-20261008T120000-0000beef'
    folder.mkdir(parents=True)
    record = {'outputs': {'facts_path': str(write_json(folder / 'x.facts.json', f)),
                          'analysis_path': str(write_json(folder / 'x.analysis.json', {'news_assessments': [a]})),
                          'md_path': str(folder / 'x.md')}}
    out = workflow.carry_forward(job, record, 'daily')
    assert out['incidents_opened'] == [new_id] and out['incidents_open_after'] == [new_id]
    assert {r['incident_id']: r['status'] for r in carry.incidents(job.history)} == {
        old_id: 'released_manually', new_id: 'open'}
    assert carry.main(['list', '--root', str(root)]) == 0
    assert [i['incident_id'] for i in json.loads(capsys.readouterr().out)['open_incidents']] == [new_id]


# ------------------------------------------------------------------ R3-03 status-specific evidence

def _incident_records(built, tmp_path):
    """open, auto-released and manually released records, all written by the current code."""
    _, facts, _ = built
    hist = tmp_path / 'seed'
    _, manual_id = _open_incident(built, hist)
    carry.release_manually(hist, manual_id, 'owner')
    t0 = as_of_of(facts)
    items = [_item(n, f'Exchange outage {n}', f'https://status.example.org/{n}', t0 - timedelta(hours=1), n * 32)
             for n in ('n2', 'n3')]
    for item in items:
        f = _edition_facts(facts, hist, t0, [item])
        ev = scoring.evaluate(f, mode='daily', assessments=[_assess(item, impact='adverse', importance='critical')])
        carry.record_incidents(hist, f, {}, ev, edition_id='e-' + item['id'])
    auto_id = scoring.incident_id(items[0])
    carry.record_incidents(hist, f, {}, {'incidents_new': [], 'incidents_released': [
        {'incident_id': auto_id, 'news_id': 'n9', 'fact_ids': ['x']}]}, edition_id='e-r')
    data = json.loads((hist / carry.INCIDENT_FILE).read_text())
    return data, {r['status']: r for r in data['records']}


def test_r3_03_records_written_by_the_code_and_legacy_known_records_stay_valid(built, tmp_path):
    data, by_status = _incident_records(built, tmp_path)
    assert set(by_status) == {'open', 'released', 'released_manually'}
    carry.check(tmp_path / 'seed')
    known = {'schema_version': 1, 'records': [{
        'known_id': 'k-000000000001', 'ids': ['url:https://x.example/a'], 'first_known_at': '2026-10-07T10:00:00+00:00',
        'adopted_as_of': '2026-10-07T12:00:00+00:00', 'edition_id': 'e', 'mode': 'daily', 'title': 't',
        'news_ids': ['n1'], 'body_hashes': []}]}  # written before follow_up_of existed
    hist = tmp_path / 'legacy'
    hist.mkdir()
    (hist / carry.KNOWN_FILE).write_text(json.dumps(known))
    carry.check(hist)


@pytest.mark.parametrize('status,mutate', [
    ('released', lambda r: r.pop('recovery')),
    ('released', lambda r: r.pop('released_edition_id')),
    ('released', lambda r: r.update(released_as_of='yesterday')),
    ('released', lambda r: r.update(recovery='ok')),
    ('released_manually', lambda r: r.pop('release_reason')),
    ('released_manually', lambda r: r.update(release_reason='  ')),
    ('released_manually', lambda r: r.pop('released_at')),
    ('released_manually', lambda r: r.update(released_by='parent')),
    ('open', lambda r: r.update(affected_source_ids=[None])),
    ('open', lambda r: r.update(status='released')),  # a bare status flip without evidence
    ('open', lambda r: r.update(status='released_manually')),
])
def test_r3_03_closed_status_without_evidence_is_invalid(built, tmp_path, capsys, status, mutate):
    data, by_status = _incident_records(built, tmp_path)
    mutate(by_status[status])
    hist = tmp_path / 'bad' / 'history'
    hist.mkdir(parents=True)
    raw = json.dumps(data).encode()
    (hist / carry.INCIDENT_FILE).write_bytes(raw)
    with pytest.raises(carry.CarryStateError) as caught:
        carry.check(hist)
    assert caught.value.code == 'incidents_record_invalid'
    assert carry.main(['list', '--root', str(tmp_path / 'bad')]) == 2
    assert json.loads(capsys.readouterr().out) == {'status': 'invalid', 'error': 'incidents_record_invalid'}
    state = {'news': {'items': []}}
    facts_mod.carry_state(state, hist, datetime(2026, 10, 7, 12, tzinfo=UTC))
    assert state['carry'] == {'status': 'invalid', 'error': 'incidents_record_invalid'}
    assert (hist / carry.INCIDENT_FILE).read_bytes() == raw


def test_r3_03_schema_version_bool_and_non_file_paths(tmp_path, capsys):
    root = tmp_path / 'job'
    hist = root / 'history'
    hist.mkdir(parents=True)
    (hist / carry.INCIDENT_FILE).write_text(json.dumps({'schema_version': True, 'records': []}))
    with pytest.raises(carry.CarryStateError) as caught:
        carry.check(hist)
    assert caught.value.code == 'incidents_schema_version_unknown'
    (hist / carry.INCIDENT_FILE).unlink()
    (hist / carry.KNOWN_FILE).mkdir()
    with pytest.raises(carry.CarryStateError) as caught:
        carry.check(hist)
    assert caught.value.code == 'known_news_not_a_file'
    (hist / carry.KNOWN_FILE).rmdir()
    os.symlink(tmp_path / 'nowhere', hist / carry.INCIDENT_FILE)  # dangling link: exists as a path, not a file
    assert carry.main(['list', '--root', str(root)]) == 2
    assert json.loads(capsys.readouterr().out) == {'status': 'invalid', 'error': 'incidents_not_a_file'}


# ------------------------------------------------------------------ R3-04 candidates before strict parsing

ASTRA_CALENDAR = {
    'fomc_combined_token': '<h2>2026 FOMC Meetings</h2><p>October 6 – 7*</p><p>December</p><p>8-9</p>',
    'fomc_invalid_start_day': '<h2>2026 FOMC Meetings</h2><p>October</p><p>99-7*</p><p>December</p><p>8-9</p>',
    'bea_short_row': '<table><tr><td>October 7 8:30 AM</td><td>GDP (Advance Estimate)</td></tr><tr><td>December 23 '
                     '8:30 AM</td><td>News</td><td>Personal Income and Outlays, November 2026</td></tr></table>',
    'bea_bad_hour': '<table><tr><td>October 7 99:30 AM</td><td>News</td><td>GDP (Advance Estimate)</td></tr><tr><td>'
                    'December 23 8:30 AM</td><td>News</td><td>Personal Income and Outlays, November 2026</td></tr></table>',
}


@pytest.mark.parametrize('label,unparsed', [
    ('fomc_combined_token', ['2026 October 6 – 7*']),
    ('fomc_invalid_start_day', ['2026 October 99-7*']),
    ('bea_short_row', ['October 7 8:30 AM | GDP (Advance Estimate)']),
    ('bea_bad_hour', ['October 7 99:30 AM | GDP (Advance Estimate)']),
])
def test_r3_04_astra_calendar_cases_are_partial(label, unparsed):
    fetch = cal.fed_calendar if label.startswith('fomc') else cal.bea_calendar
    result = fetch(_calendar_fetcher(ASTRA_CALENDAR[label]), date(2026, 10, 7))
    assert result.status == 'partial' and result.values['parse']['unparsed'] == unparsed
    assert not any(e['start_at'].startswith('2026-10-07') for e in result.values['events'])


def test_r3_04_non_meetings_are_excluded_and_spans_parse():
    page = ('<h4>2026 FOMC Meetings</h4><p>October</p><p>9 (unscheduled)</p><p>October/November</p><p>31-1</p>'
            '<p>December</p><p>8-9*</p>')
    events, stats = cal.parse_fomc_detail(page, date(2026, 10, 7))
    assert stats['unparsed'] == [] and stats['excluded_non_meetings'] == ['2026 October 9 (unscheduled)']
    assert sorted({e['start_at'][:10] for e in events}) == ['2026-11-01', '2026-12-09']
    _, bad = cal.parse_fomc_detail('<h4>2026 FOMC Meetings</h4><p>October</p><p>28-27</p>', date(2026, 10, 7))
    assert bad['unparsed'] == ['2026 October 28-27']
    rows = ('<table><tr><td>October 29 12:00 PM</td><td>News</td><td>GDP (Advance Estimate)</td></tr>'
            '<tr><td>October 30 8:60 AM</td><td>News</td><td>Personal Income and Outlays, September 2026</td></tr></table>')
    events, stats = cal.parse_bea_detail(rows, date(2026, 10, 7))
    assert [e['start_at'] for e in events] == ['2026-10-29T16:00:00Z']
    assert stats['unparsed'] == ['October 30 8:60 AM | Personal Income and Outlays, September 2026']


# ------------------------------------------------------------------ R3-05 raw control characters

@pytest.mark.parametrize('reason', ['x\ty', 'x\ny', 'x\ry', 'x\x00y', 'x\x85y', 'x y'])
def test_r3_05_control_characters_in_the_raw_reason_are_refused(built, tmp_path, capsys, reason):
    root = tmp_path / 'job'
    hist = root / 'history'
    _, incident_id = _open_incident(built, hist)
    before = (hist / carry.INCIDENT_FILE).read_bytes()
    assert carry.main(['release', '--root', str(root), '--incident', incident_id, '--reason', reason]) == 2
    assert 'reason_invalid' in capsys.readouterr().out
    assert (hist / carry.INCIDENT_FILE).read_bytes() == before


def test_r3_05_spaces_are_normalised_after_the_check(built, tmp_path):
    hist = tmp_path / 'history'
    _, incident_id = _open_incident(built, hist)
    assert carry.release_manually(hist, incident_id, '  社長指示   電話で確認 ')['release_reason'] == '社長指示 電話で確認'


def test_trust_model_is_documented_and_not_in_the_parent_workflow():
    root = Path(__file__).parents[2]
    readme = (root / 'docs' / 'btc' / 'README.md').read_text()
    assert 'operator assertion' in readme and 'tab' in readme and 'newline' in readme
    workflow_text = (root / 'btc' / 'job_template' / 'PARENT-WORKFLOW.md').read_text()
    assert 'btc.carry' not in workflow_text and 'released_manually' not in workflow_text
