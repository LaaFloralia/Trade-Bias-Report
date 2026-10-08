"""Regression tests for the independent review findings R-01..R-09 (Astra, 2026-10-08). No network.

Each test names the finding and the boundary the review asked for.
"""
import copy
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile

import pytest

from btc import analysis as analysis_mod, carry, facts as facts_mod, machine as machine_mod, presentation, scoring
from btc import us_calendar, workflow
from btc.common import JST, parse_time, write_json
from btc.facts import Facts, calendar_events, derivatives_facts, etf_facts, options_facts, price_facts, stable_facts
from btc.history import History
from btc.news import reaction, title_key
from btc.pipeline import BtcStages
from btc.sources.calendar import fomc_coverage_end, load_cache
from btc.stages import AnalysisError
from tests.btc.test_pipeline import EDITION, claim, context, index, load_collection, valid_analysis
from tests.btc.test_workflow import full_review

UTC = timezone.utc


@pytest.fixture(scope='module')
def built(tmp_path_factory):
    collection = load_collection()
    ctx = context(tmp_path_factory.mktemp('job'), collection)
    return collection, BtcStages().build_facts(collection, ctx), ctx


def sources(collection):
    return {s['source_id']: s for s in collection['sources']}


def as_of_of(facts):
    return parse_time(facts['as_of']).astimezone(UTC)


# ------------------------------------------------------------------ R-01 ETF state -> score -> MD/HTML/machine

def _farside_row(collection, day, **changes):
    c = copy.deepcopy(collection)
    row = next(r for r in sources(c)['farside']['values']['rows'] if r['trade_date'] == day)
    row.update(changes)
    return c


def _edition(collection, tmp_path, name, history_files=None):
    ctx = context(tmp_path / name, collection)
    for file_name, data in (history_files or {}).items():
        ctx.history_dir.mkdir(parents=True, exist_ok=True)
        (ctx.history_dir / file_name).write_bytes(data)
    stages = BtcStages()
    facts = stages.build_facts(collection, ctx)
    ctx.generated_at = (as_of_of(facts) + timedelta(minutes=10)).astimezone(JST)
    a = valid_analysis(facts)
    ref = facts['state']['reference_price']['fact_id']
    a['thesis'] = claim('TEST 参照価格は{{fact:%s}}。' % ref, [ref], 'fact_interpretation')  # 5d may be unusable
    validated = stages.validate_analysis(a, facts, ctx)
    parts = stages.build_report(validated, facts, collection, ctx)
    machine = machine_mod.build(parts.machine_core, parts.machine_extensions, mode='daily', session_slot='pm',
                                collected_at=collection['collection_started_at'],
                                generated_at=ctx.generated_at.isoformat(), as_of=facts['as_of'])
    stages.validate_machine(machine, parts)
    folder = tmp_path / name / 'out'
    folder.mkdir()
    md = folder / 'BTCUSD_Daily_Report_2026-10-07_2123.md'
    md.write_text(parts.markdown)
    summary, figures = write_json(folder / 's.json', parts.summary), write_json(folder / 'f.json', parts.figures)
    html = presentation.render(md, summary_path=summary, visuals_path=figures, as_of=collection['collection_started_at'],
                               kind='daily', session_slot='pm', generated_at=ctx.generated_at.isoformat())['html_path']
    ev = scoring.evaluate(facts, mode='daily', assessments=[])
    return facts, ev, parts.markdown, Path(html).read_text(), machine


def test_r01_etf_full_partial_and_conflict_rows_from_facts_to_outputs(tmp_path):
    """Full numeric + reconciled counts; a partial row and a provider-total conflict do not, and the conflict is a
    hard invalid (data_hold) that keeps both values unscored. Traced through facts, score, MD, HTML and machine."""
    base = load_collection()
    full = _edition(base, tmp_path, 'full')
    # Expected day partial and the previous window not usable either (10-05 also partial): no lagged fallback.
    partial = _edition(_farside_row(_farside_row(base, '2026-10-05', table_complete=False, validated_total_musd=None,
                                                 known_sum_musd=-80.0, missing_funds=['GBTC']),
                                    '2026-10-06', table_complete=False, validated_total_musd=None,
                                    known_sum_musd=50.0, missing_funds=['IBIT']), tmp_path, 'partial')
    conflict = _edition(_farside_row(base, '2026-10-06', conflict=True, validated_total_musd=None,
                                     known_sum_musd=100.0), tmp_path, 'conflict')

    facts, ev, md, html, machine = full
    assert facts['state']['etf']['status'] == 'ok' and facts['state']['etf']['expected_day']['reconciled']
    assert ev['bundles']['etf'] and 'etf_total_mismatch' not in ev['hard_invalid']
    assert machine['btc']['etf']['status'] == 'provisional'
    assert '合計の照合も成立' in md and '合計の照合も成立' in html
    assert '祝日を考慮していない' not in md and 'NYSEの休場表（2028年末まで）' in md  # R-06 limitation text

    facts, ev, md, html, machine = partial
    etf = facts['state']['etf']
    assert etf['status'] == 'incomplete' and etf['expected_day'] == {'present': True, 'complete': False,
                                                                     'reconciled': False, 'conflict': False}
    assert index(facts)[etf['five_day_fact_id']]['value'] is None  # the expected day is in the 5-day window
    assert not ev['bundles']['etf'] and ev['coverage_present'] == full[1]['coverage_present'] - 1
    assert ev['groups']['spot_demand']['components']['etf']['state'] == 'unknown'
    assert machine['btc']['etf']['status'] == 'partial'
    assert machine['btc']['coverage']['present'] == ev['coverage_present'] and 'etf' in machine['btc']['coverage']['missing']
    assert '全銘柄の値が未確定' in md and '全銘柄の値が未確定' in html

    facts, ev, md, html, machine = conflict
    etf = facts['state']['etf']
    assert etf['status'] == 'conflict' and etf['conflict_dates'] == ['2026-10-06']
    kept = {f['metric']: f for f in facts['facts'] if f['observation_date'] == '2026-10-06' and f['category'] == 'etf'}
    assert kept['etf_reported_total_usd']['value'] == 118.8e6 and kept['etf_reported_total_usd']['status'] == 'conflict'
    assert kept['etf_known_partial_sum_usd']['value'] == 100.0e6 and kept['etf_known_partial_sum_usd']['status'] == 'conflict'
    assert kept['etf_netflow_usd']['value'] is None and kept['etf_netflow_usd']['status'] == 'conflict'
    assert 'etf_total_mismatch' in ev['hard_invalid'] and ev['trade_gate']['status'] == 'data_hold'
    assert not ev['bundles']['etf'] and ev['confidence'] == 0.0 and ev['bias'] == 0.0
    assert machine['btc']['etf']['status'] == 'conflict' and machine['no_trade'] is True
    for text in (md, html):
        assert 'ETFの提供元合計と銘柄合計が一致しない' in text and '提供元の合計と銘柄の合計が一致しない' in text


def test_r01_production_shape_counts_six_of_eight(built):
    """10-08 Daily shape: expected-day row present with 4 of 12 funds -> ETF not a normal bundle (6/8, -2, -1)."""
    collection, _, _ = built
    c = _farside_row(collection, '2026-10-06', table_complete=False, validated_total_musd=None, known_sum_musd=-66.9,
                     missing_funds=['F'] * 8)
    F = Facts(as_of_of({'as_of': c['collection_completed_at']}), 'a' * 64)
    st = {}
    with_history = History(Path(tempfile.mkdtemp()))
    etf_facts(F, sources(c), st, with_history, as_of_of({'as_of': c['collection_completed_at']}))
    # The previous window (09-29..10-05) is complete, so the lagged fallback applies; still not a normal bundle.
    assert st['etf']['status'] == 'lagged' and st['etf']['lag_business_days'] == 1
    assert st['etf']['expected_day']['complete'] is False
    assert scoring.bundles({'facts': F.items, 'state': st})['etf'] is False


# ------------------------------------------------------------------ R-02 stale facts are unknown for scoring

def test_r02_stale_supply_is_display_only(built):
    collection, facts, _ = built
    later = as_of_of(facts) + timedelta(days=7)
    F = Facts(later, 'a' * 64)
    st = {}
    stable_facts(F, sources(collection), st)
    f = {'facts': F.items, 'state': st}
    assert all(x['status'] == 'stale' for x in F.items)
    assert st['stable']['status'] == 'stale'
    assert scoring.supply_component(f)['state'] == 'unknown' and scoring.bundles(f)['stable_supply'] is False
    fresh = {'facts': facts['facts'], 'state': facts['state']}
    assert scoring.bundles(fresh)['stable_supply'] is True


def _make_stale(facts, fact_ids):
    f = copy.deepcopy(facts)
    for x in f['facts']:
        if x['fact_id'] in fact_ids:
            x.update(status='stale', stale=True)
    return f


def test_r02_stale_derivatives_options_and_price_do_not_count(built):
    _, facts, _ = built
    st = facts['state']
    assert scoring.bundles(facts)['derivatives'] and scoring.bundles(facts)['options'] and scoring.bundles(facts)['price']
    venues = st['derivatives']['venues']
    stale_funding = _make_stale(facts, {v['funding_fact_id'] for v in venues[:2]})
    assert scoring.bundles(stale_funding)['derivatives'] is False
    lev = scoring.leverage_item(stale_funding, 1)
    assert not set(lev['fact_ids']) & {v['funding_fact_id'] for v in venues[:2]}
    stale_oi = _make_stale(facts, {v['oi_fact_id'] for v in venues[:2]})
    assert scoring.bundles(stale_oi)['derivatives'] is False
    skew = st['options']['skew']
    stale_opts = _make_stale(facts, {st['options']['total_fact_id'], skew.get('skew_25d_7d'), skew.get('skew_25d_30d')})
    assert scoring.bundles(stale_opts)['options'] is False
    assert scoring.options_item(stale_opts, 1)['reason_code'] == 'skew_unavailable'
    stale_ref = _make_stale(facts, {st['reference_price']['fact_id']})
    assert scoring.bundles(stale_ref)['price'] is False
    # ETF five-day sum that went stale (lag >= 2) is not a direction input.
    stale_etf = _make_stale(facts, {st['etf']['five_day_fact_id']})
    assert scoring.etf_component(stale_etf)['state'] == 'unknown'


def test_r02_options_state_follows_freshness(built):
    collection, facts, _ = built
    later = as_of_of(facts) + timedelta(hours=3)
    F = Facts(later, 'a' * 64)
    st = {}
    options_facts(F, sources(collection), st, History(Path(tempfile.mkdtemp())), later)
    assert st['options']['status'] == 'stale'
    assert scoring.bundles({'facts': F.items, 'state': {**st}})['options'] is False


# ------------------------------------------------------------------ R-03 calendar fails closed

def _calendar(collection, as_of, mutate=None, cache_mutate=None):
    S = copy.deepcopy(sources(collection))
    cache = load_cache()
    if mutate:
        mutate(S)
    if cache_mutate:
        cache_mutate(cache)
    return calendar_events(S, as_of, cache)


def test_r03_calendar_boundaries(built):
    collection, facts, _ = built
    as_of = as_of_of(facts)
    events, cal = _calendar(collection, as_of)
    assert cal['next_24h'] == 'ok' and cal['fed'] == 'ok' and cal['bea'] == 'ok'

    def expired(S):
        for sid in ('fed_calendar', 'bea_calendar'):
            S[sid]['values']['coverage_end'] = (as_of - timedelta(days=1)).date().isoformat()
    _, cal = _calendar(collection, as_of, expired)
    assert cal['next_24h'] == 'unknown' and cal['fed'] == cal['bea'] == 'beyond_coverage'
    assert any('fed_calendar' in r for r in cal['reasons'])

    def horizon_edge(S):  # coverage ends the day before the end of the next 24 h (New York date)
        horizon = (as_of + timedelta(hours=24)).astimezone(facts_mod.NY).date()
        S['fed_calendar']['values']['coverage_end'] = (horizon - timedelta(days=1)).isoformat()
    assert _calendar(collection, as_of, horizon_edge)[1]['next_24h'] == 'unknown'

    def partially_parsed(S):
        S['bea_calendar']['status'] = 'partial'
    _, cal = _calendar(collection, as_of, partially_parsed)
    assert cal['next_24h'] == 'unknown' and cal['bea'] == 'partial'

    def zero_events_in_range(S):
        S['fed_calendar']['values']['events'] = []
    events, cal = _calendar(collection, as_of, zero_events_in_range)
    assert cal['next_24h'] == 'ok' and not any(e['source_id'] == 'fed_calendar' for e in events)

    def missing_coverage(S):
        S['fed_calendar']['values'].pop('coverage_end')
    assert _calendar(collection, as_of, missing_coverage)[1]['fed'] == 'unverified'

    def verified_after_as_of(S):
        S['bea_calendar']['retrieved_at'] = (as_of + timedelta(minutes=5)).isoformat()
    assert _calendar(collection, as_of, verified_after_as_of)[1]['next_24h'] == 'unknown'

    def verified_too_long_ago(S):
        S['bea_calendar']['retrieved_at'] = (as_of - timedelta(hours=30)).isoformat()
    assert _calendar(collection, as_of, verified_too_long_ago)[1]['next_24h'] == 'unknown'

    def failed(S):
        S['fed_calendar'].update(status='unavailable', error_kind='timeout')
    assert _calendar(collection, as_of, failed)[1]['fed'] == 'unavailable'

    _, cal = _calendar(collection, as_of, cache_mutate=lambda c: c.update(coverage_end=as_of.date().isoformat()))
    assert cal['bls_cache'] == 'beyond_coverage' and cal['next_24h'] == 'unknown'
    _, cal = _calendar(collection, as_of, cache_mutate=lambda c: c.update(verified_at=(as_of + timedelta(hours=1)).isoformat()))
    assert cal['bls_cache'] == 'unverified' and cal['next_24h'] == 'unknown'

    f = copy.deepcopy(facts)
    f['state']['calendar'] = _calendar(collection, as_of, expired)[1]
    ev = scoring.evaluate(f, mode='daily')
    assert 'calendar_coverage_unknown' in ev['hard_invalid'] and ev['trade_gate']['status'] == 'data_hold'


def test_r03_fomc_coverage_end_is_what_the_page_lists():
    page = '<h4>2026 FOMC Meetings</h4><p>October</p><p>27-28</p><p>December</p><p>8-9*</p>'
    assert fomc_coverage_end(page, date(2026, 10, 7)) == '2026-12-31'
    page2 = page + '<h4>2027 FOMC Meetings</h4><p>January</p><p>26-27</p>'
    assert fomc_coverage_end(page2, date(2026, 10, 7)) == '2027-02-04'  # capped at the 120-day parse window
    assert fomc_coverage_end('<h4>2025 FOMC Meetings</h4>', date(2026, 10, 7)) == '2026-10-06'  # nothing covered


# ------------------------------------------------------------------ R-04 / R-05 carried state

def _item(news_id, title, url, published, sha, official=True):
    return {'id': news_id, 'event_cluster_id': 'c-' + title_key(title), 'title': title, 'url': url,
            'publisher': 'SEC' if official else 'Media', 'official': official, 'published_at': published.isoformat(),
            'first_seen_at': None,
            'code_verification': 'primary_body_retrieved' if official else 'secondary_body_retrieved',
            'body': {'status': 'retrieved', 'text_sha256': sha, 'excerpt': title}, 'excerpt': title, 'p': None,
            'reaction': None, 'fact_ids': [], 'feed_raw_sha256': None}


def _assess(item, impact='supportive', importance='high', **extra):
    return {'news_id': item['id'], 'event_cluster_id': item['event_cluster_id'], 'group_id': 'btc_specific_event',
            'impact': impact, 'importance': importance, 'verification': 'primary_confirmed',
            'source_quote': item['title'][:20], 'fact_ids': [], 'interpretation': claim('TEST 一次本文で確認。'),
            **extra}


def _edition_facts(facts, history, as_of, items):
    f = copy.deepcopy(facts)
    f['as_of'] = as_of.isoformat()
    f['state']['news']['items'] = items
    facts_mod.carry_state(f['state'], history, as_of)
    return f


def test_r05_known_news_across_editions_and_modes(built, tmp_path):
    _, facts, _ = built
    hist = tmp_path / 'history'
    t0 = as_of_of(facts)
    first = _item('n1', 'SEC approves in-kind creations for spot bitcoin ETFs', 'https://www.sec.gov/news/a',
                  t0 - timedelta(hours=2), 'a' * 64)
    f1 = _edition_facts(facts, hist, t0, [first])
    a1 = _assess(first)
    ev = scoring.evaluate(f1, mode='daily', assessments=[a1])
    assert ev['groups']['btc_specific_event']['direction'] == 1
    assert carry.record_known(hist, f1, {'news_assessments': [a1]}, edition_id='e1', mode='daily') == 1
    known_id = carry.known_news(hist, t0)[0]['known_id']

    # Next edition (+12 h): a re-report with another URL and an updated headline, linked by the parent.
    rerun = _item('n2', 'Update: SEC approval of in-kind ETF creations', 'https://www.sec.gov/news/a-update',
                  t0 + timedelta(hours=11), 'b' * 64)
    f2 = _edition_facts(facts, hist, t0 + timedelta(hours=12), [rerun])
    assert [k['known_id'] for k in f2['state']['news']['carried']] == [known_id]
    linked = _assess(rerun, known_event_ids=[known_id])
    g = scoring.evaluate(f2, mode='daily', assessments=[linked])['groups']['btc_specific_event']
    assert g['direction'] == 1 and g['components']['carry_forward'][0]['kind'] == 'carry_forward'

    # +40 h: still inside 36 h of the re-report, but the known event's period has ended -> not counted.
    f3 = _edition_facts(facts, hist, t0 + timedelta(hours=40), [dict(rerun, published_at=(t0 + timedelta(hours=39)).isoformat())])
    assert scoring.evaluate(f3, mode='daily', assessments=[linked])['groups']['btc_specific_event']['direction'] == 0
    # Same primary body under a new URL and headline is matched by the code without a parent link.
    same_body = _item('n3', 'Bitcoin ETFs gain in-kind creations', 'https://www.sec.gov/news/b',
                      t0 + timedelta(hours=39), 'a' * 64)
    f3b = _edition_facts(facts, hist, t0 + timedelta(hours=40), [same_body])
    assert scoring.evaluate(f3b, mode='daily', assessments=[_assess(same_body)])['groups']['btc_specific_event']['direction'] == 0
    # A follow-up with new primary facts counts; the same flag on an old body does not.
    follow = _item('n4', 'SEC sets effective date for in-kind ETF creations', 'https://www.sec.gov/news/c',
                   t0 + timedelta(hours=39), 'c' * 64)
    f4 = _edition_facts(facts, hist, t0 + timedelta(hours=40), [follow, same_body])
    g = scoring.evaluate(f4, mode='daily', assessments=[
        _assess(follow, known_event_ids=[known_id], follow_up_new_facts=True),
        _assess(same_body, known_event_ids=[known_id], follow_up_new_facts=True)])['groups']['btc_specific_event']
    assert g['direction'] == 1 and g['components']['news_ids'] == ['n4']
    assert g['components']['carry_forward'][0]['kind'] == 'follow_up_new_facts'

    # Across modes: a Daily-adopted event counts in Weekly only inside 7 days of its first appearance.
    fw = _edition_facts(facts, hist, t0 + timedelta(hours=40), [rerun])
    assert scoring.evaluate(fw, mode='weekly', assessments=[linked])['groups']['btc_specific_event']['direction'] == 1
    # +8 days: outside every scoring window, but still retained for matching (R2-02) -> the link holds, no points.
    fw8 = _edition_facts(facts, hist, t0 + timedelta(days=8), [dict(rerun, published_at=(t0 + timedelta(days=7, hours=23)).isoformat())])
    assert [k['known_id'] for k in carry.known_news(hist, t0 + timedelta(days=8))] == [known_id]
    assert [k['known_id'] for k in fw8['state']['news']['carried']] == [known_id]
    assert scoring.evaluate(fw8, mode='weekly', assessments=[linked])['groups']['btc_specific_event']['direction'] == 0
    # Not linked and not matched -> the code cannot tie it; the parent must link (briefing lists known_id).
    assert 'known_event_ids' in analysis_mod.briefing(f2, scoring.evaluate(f2, mode='daily'), edition_id='x',
                                                      mode='daily', session_slot='pm')

    # Legacy unit input: an explicit URL set excludes the item.
    assert scoring.eligible_news(f1, [a1], 'daily', t0, {first['url']}) == []


def test_r2_02_late_re_reports_never_count_again(built, tmp_path, monkeypatch):
    """Astra R2-02: matching retention (180 days) is separate from the scoring window."""
    _, facts, _ = built
    hist = tmp_path / 'history'
    t0 = as_of_of(facts)
    first = _item('n1', 'SEC approves in-kind creations for spot bitcoin ETFs', 'https://www.sec.gov/news/a',
                  t0 - timedelta(hours=2), 'a' * 64)
    f1 = _edition_facts(facts, hist, t0, [first])
    carry.record_known(hist, f1, {'news_assessments': [_assess(first)]}, edition_id='e1', mode='daily')
    known_id = carry.known_news(hist, t0)[0]['known_id']
    late = t0 + timedelta(days=8)
    for item in (_item('n2', 'Spot bitcoin ETF in-kind creations take effect', first['url'], late - timedelta(hours=1), 'b' * 64),
                 _item('n3', first['title'], 'https://www.sec.gov/news/z', late - timedelta(hours=1), 'c' * 64),
                 _item('n4', 'In-kind ETF creations now allowed', 'https://www.sec.gov/news/y', late - timedelta(hours=1), 'a' * 64)):
        f = _edition_facts(facts, hist, late, [item])
        for mode in ('daily', 'weekly'):
            g = scoring.evaluate(f, mode=mode, assessments=[_assess(item)])['groups']['btc_specific_event']
            assert g['direction'] == 0, (item['id'], mode)
        assert g['components']['news_ids'] == []
    # An explicit link to an event older than the 14-day briefing table is accepted (and still never counts).
    old = t0 + timedelta(days=20)
    rewrite = _item('n5', 'Regulator recap: in-kind creations', 'https://example.gov/recap', old - timedelta(hours=1), 'd' * 64)
    f = _edition_facts(facts, hist, old, [rewrite])
    a = valid_analysis(f)
    a['news_assessments'] = [_assess(rewrite, known_event_ids=[known_id])]
    analysis_mod.validate(a, f, edition_id=EDITION)
    assert scoring.evaluate(f, mode='weekly', assessments=a['news_assessments'])['groups']['btc_specific_event']['direction'] == 0
    assert known_id not in analysis_mod.briefing(f, scoring.evaluate(f, mode='daily'), edition_id='x', mode='daily',
                                                 session_slot='pm')
    f8 = _edition_facts(facts, hist, late, [])
    assert known_id in analysis_mod.briefing(f8, scoring.evaluate(f8, mode='daily'), edition_id='x', mode='daily',
                                             session_slot='pm')
    # Retention: 180 days, then at most KNOWN_MAX_RECORDS (oldest dropped).
    monkeypatch.setattr(carry, 'KNOWN_MAX_RECORDS', 3)
    for i in range(4):
        when = t0 + timedelta(days=30, hours=i)
        item = _item(f'm{i}', f'Unrelated official notice {i}', f'https://www.sec.gov/m{i}', when - timedelta(hours=1), str(i) * 64)
        carry.record_known(hist, _edition_facts(facts, hist, when, [item]), {'news_assessments': [_assess(item)]},
                           edition_id=f'e{i}', mode='daily')
    titles = [r['title'] for r in carry.known_news(hist, t0 + timedelta(days=31))]
    assert titles == ['Unrelated official notice 1', 'Unrelated official notice 2', 'Unrelated official notice 3']
    monkeypatch.setattr(carry, 'KNOWN_MAX_RECORDS', 5000)
    far = t0 + timedelta(days=30 + 181)
    item = _item('z', 'Another notice', 'https://www.sec.gov/z', far - timedelta(hours=1), 'e' * 64)
    carry.record_known(hist, _edition_facts(facts, hist, far, [item]), {'news_assessments': [_assess(item)]},
                       edition_id='ez', mode='daily')
    assert [r['title'] for r in carry.known_news(hist, far)] == ['Another notice']


def test_r2_03_adopted_follow_up_counts_from_its_own_publication(built, tmp_path):
    """Astra R2-03: an adopted follow-up is its own known record; it never falls back to the original's time."""
    _, facts, _ = built
    hist = tmp_path / 'history'
    t0 = as_of_of(facts)
    first = _item('n1', 'SEC approves in-kind creations for spot bitcoin ETFs', 'https://www.sec.gov/news/a',
                  t0 - timedelta(hours=2), 'a' * 64)
    carry.record_known(hist, _edition_facts(facts, hist, t0, [first]), {'news_assessments': [_assess(first)]},
                       edition_id='e1', mode='daily')
    known_id = carry.known_news(hist, t0)[0]['known_id']
    t1 = t0 + timedelta(hours=41)
    follow = _item('n4', 'SEC sets effective date for in-kind ETF creations', 'https://www.sec.gov/news/c',
                   t1 - timedelta(hours=1), 'c' * 64)
    a4 = _assess(follow, known_event_ids=[known_id], follow_up_new_facts=True)
    f1 = _edition_facts(facts, hist, t1, [follow])
    g = scoring.evaluate(f1, mode='daily', assessments=[a4])['groups']['btc_specific_event']
    assert g['direction'] == 1 and g['components']['carry_forward'][0]['kind'] == 'follow_up_new_facts'
    assert carry.record_known(hist, f1, {'news_assessments': [a4]}, edition_id='e2', mode='daily') == 1
    records = carry.known_news(hist, t1)
    original, own = records
    assert own['follow_up_of'] == known_id and own['known_id'] != known_id
    assert own['first_known_at'] == (t1 - timedelta(hours=1)).isoformat() and own['body_hashes'] == ['c' * 64]
    assert original['body_hashes'] == ['a' * 64]  # not folded into the original
    # +2 h: the same follow-up (URL match) is carried from its own first_known_at, not the original's (43 h old).
    for assessment in (a4, _assess(follow), _assess(follow, known_event_ids=[known_id])):
        f2 = _edition_facts(facts, hist, t1 + timedelta(hours=2), [follow])
        g = scoring.evaluate(f2, mode='daily', assessments=[assessment])['groups']['btc_specific_event']
        assert g['direction'] == 1, assessment
        assert g['components']['carry_forward'][0] == {'news_id': 'n4', 'known_id': own['known_id'], 'kind': 'carry_forward',
                                                       'first_known_at': own['first_known_at']}
    # ... until its own first_known_at + the mode window.
    f3 = _edition_facts(facts, hist, t1 + timedelta(hours=36), [follow])
    assert scoring.evaluate(f3, mode='daily', assessments=[a4])['groups']['btc_specific_event']['direction'] == 0
    # Re-adopting it merges into the follow-up record, not the original.
    assert carry.record_known(hist, f1, {'news_assessments': [a4]}, edition_id='e3', mode='daily') == 0
    assert len(carry.known_news(hist, t1)) == 2


def test_r05_report_build_uses_carried_state(built, tmp_path):
    collection, facts, ctx = built
    hist = tmp_path / 'history'
    t0 = as_of_of(facts)
    first = _item('n1', 'SEC approves in-kind creations for spot bitcoin ETFs', 'https://www.sec.gov/news/a',
                  t0 - timedelta(hours=40), 'a' * 64)
    f0 = _edition_facts(facts, hist, t0 - timedelta(hours=39), [first])
    carry.record_known(hist, f0, {'news_assessments': [_assess(first)]}, edition_id='e0', mode='weekly')
    rerun = _item('n2', 'Bitcoin ETF in-kind creations approved', 'https://www.sec.gov/news/a',
                  t0 - timedelta(hours=1), 'b' * 64)  # same URL, new headline
    f = _edition_facts(facts, hist, t0, [rerun])
    a = valid_analysis(f)
    a['news_assessments'] = [_assess(rerun)]
    stages = BtcStages()
    c = copy.copy(ctx)
    c.generated_at = (t0 + timedelta(minutes=5)).astimezone(JST)
    parts = stages.build_report(stages.validate_analysis(a, f, c), f, collection, c)
    group = parts.machine_extensions['btc']['groups']['btc_specific_event']
    assert group['direction'] == 0 and group['components']['carry_forward'] == []
    assert group['components']['news_ids'] == []


def test_r05_analysis_validates_known_links(built, tmp_path):
    _, facts, _ = built
    t0 = as_of_of(facts)
    item = _item('n9', 'SEC approves in-kind creations for spot bitcoin ETFs', 'https://www.sec.gov/news/a', t0, 'a' * 64)
    f = _edition_facts(facts, tmp_path / 'h', t0, [item])
    a = valid_analysis(f)
    a['news_assessments'] = [_assess(item, known_event_ids=['k-000000000000'], follow_up_new_facts=True)]
    with pytest.raises(AnalysisError) as caught:
        analysis_mod.validate(a, f, edition_id=EDITION)
    assert any('unknown_known_event:k-000000000000' in p for p in caught.value.problems)
    a['news_assessments'] = [_assess(item, follow_up_new_facts=True)]
    with pytest.raises(AnalysisError) as caught:
        analysis_mod.validate(a, f, edition_id=EDITION)
    assert any('follow_up_requires_known_event_ids' in p for p in caught.value.problems)


def _fresh_fact(facts, source_id):
    return next(x['fact_id'] for x in facts['facts'] if x['source_id'] == source_id and scoring.scorable(x))


def _oi_fact(facts, source_id):
    """Open interest observed at ~12:23:4x UTC in the fixture (after a recovery notice at as_of - 5 min)."""
    return next(x['fact_id'] for x in facts['facts']
                if x['source_id'] == source_id and '.oi_btc.' in x['fact_id'] and scoring.scorable(x))


def _fact_id(facts, part):
    return next(x['fact_id'] for x in facts['facts'] if part in x['fact_id'])


def test_r04_incident_persists_until_official_recovery_and_fresh_data(built, tmp_path):
    _, facts, _ = built
    hist = tmp_path / 'history'
    t0 = as_of_of(facts)
    hack = _item('n1', 'Exchange halts bitcoin withdrawals after security breach', 'https://status.example.org/i1',
                 t0 - timedelta(hours=1), 'd' * 64)
    f1 = _edition_facts(facts, hist, t0, [hack])
    a1 = _assess(hack, impact='adverse', importance='critical', affected_source_ids=['binance_derivatives'])
    ev1 = scoring.evaluate(f1, mode='daily', assessments=[a1])
    assert ev1['trade_gate']['status'] == 'incident_hold' and len(ev1['incidents_new']) == 1
    incident_id = ev1['incidents_new'][0]['incident_id']
    assert carry.record_incidents(hist, f1, {'news_assessments': [a1]}, ev1, edition_id='e1')['opened'] == [incident_id]

    # Next edition: the article dropped out of selection -> still on hold (inherited, parent sees it).
    f2 = _edition_facts(facts, hist, t0 + timedelta(hours=6), [])
    assert [i['incident_id'] for i in f2['state']['incidents']['open']] == [incident_id]
    ev2 = scoring.evaluate(f2, mode='daily', assessments=[])
    assert ev2['trade_gate']['status'] == 'incident_hold' and ev2['trade_gate']['reason_codes'] == ['incident_unresolved']
    assert scoring.evaluate(f2, mode='daily')['trade_gate']['status'] == 'incident_hold'  # preview too
    briefing = analysis_mod.briefing(f2, scoring.evaluate(f2, mode='daily'), edition_id='x', mode='daily', session_slot='pm')
    assert incident_id in briefing

    # Restart: a brand-new state read only from the job history still holds.
    state = {'news': {'items': []}}
    facts_mod.carry_state(state, hist, t0 + timedelta(hours=7))
    assert [i['incident_id'] for i in state['incidents']['open']] == [incident_id]

    # Release attempts.
    media = _item('n5', 'Exchange says withdrawals resumed', 'https://media.example.com/r', t0 - timedelta(minutes=5),
                  'e' * 64, official=False)
    stale_official = _item('n6', 'Exchange resumes withdrawals', 'https://status.example.org/r0',
                           t0 - timedelta(hours=2), 'f' * 64)
    official = _item('n7', 'Exchange resumes bitcoin withdrawals', 'https://status.example.org/r1',
                     t0 - timedelta(minutes=5), '1' * 64)
    f3 = _edition_facts(facts, hist, t0, [media, stale_official, official])
    binance = _oi_fact(f3, 'binance_derivatives')  # observed 12:23:43, after the notice (12:20:06)
    bybit = _oi_fact(f3, 'bybit_derivatives')
    stale = copy.deepcopy(f3)
    next(x for x in stale['facts'] if x['fact_id'] == binance).update(status='stale', stale=True)
    cases = [
        ([], f3, None),
        ([{'incident_id': incident_id, 'news_id': 'n5', 'fact_ids': [binance]}], f3, 'recovery_not_official_body'),
        ([{'incident_id': incident_id, 'news_id': 'n6', 'fact_ids': [binance]}], f3, 'recovery_not_after_incident'),
        ([{'incident_id': incident_id, 'news_id': 'n7', 'fact_ids': [bybit]}], f3,
         'recovery_affected_source_not_fresh:binance_derivatives'),
        ([{'incident_id': incident_id, 'news_id': 'n7', 'fact_ids': [binance]}], stale, 'recovery_data_not_fresh'),
    ]
    for recoveries, f, problem in cases:
        ev = scoring.evaluate(f, mode='daily', assessments=[], recoveries=recoveries)
        assert ev['trade_gate']['status'] == 'incident_hold', recoveries
        if problem:
            assert any(problem in p for p in ev['incidents_rejected'][0]['problems']), ev['incidents_rejected']
    good = [{'incident_id': incident_id, 'news_id': 'n7', 'fact_ids': [binance]}]
    ev = scoring.evaluate(f3, mode='daily', assessments=[], recoveries=good)
    assert ev['trade_gate']['status'] != 'incident_hold' and [r['incident_id'] for r in ev['incidents_released']] == [incident_id]
    assert carry.record_incidents(hist, f3, {'news_assessments': []}, ev, edition_id='e3')['released'] == [incident_id]
    f4 = _edition_facts(facts, hist, t0 + timedelta(hours=12), [])
    assert f4['state']['incidents']['open'] == []
    assert scoring.evaluate(f4, mode='daily', assessments=[])['trade_gate']['status'] != 'incident_hold'


def test_r2_01_recovery_needs_observations_after_the_notice(built, tmp_path):
    """Astra R2-01: pre-incident data never proves a recovery; derived facts need every input after the notice."""
    _, facts, _ = built
    hist = tmp_path / 'history'
    t0 = as_of_of(facts)
    hack = _item('n1', 'Exchange halts bitcoin withdrawals after security breach', 'https://status.example.org/i1',
                 t0 - timedelta(hours=1), 'd' * 64)
    f1 = _edition_facts(facts, hist, t0, [hack])
    a1 = _assess(hack, impact='adverse', importance='critical', affected_source_ids=['binance_derivatives'])
    ev1 = scoring.evaluate(f1, mode='daily', assessments=[a1])
    incident_id = ev1['incidents_new'][0]['incident_id']
    carry.record_incidents(hist, f1, {}, ev1, edition_id='e1')
    official = _item('n7', 'Exchange resumes bitcoin withdrawals', 'https://status.example.org/r1',
                     t0 - timedelta(minutes=5), '1' * 64)
    f = _edition_facts(facts, hist, t0, [official])
    funding = _fact_id(f, 'binance.btcusdt.funding_settled.')  # observed 08:00, before the incident
    oi = _oi_fact(f, 'binance_derivatives')
    derived_new = _fact_id(f, 'binance.btcusdt.oi_notional_usdt.')  # inputs 12:23:43 and 12:23:47
    derived_old = _fact_id(f, 'binance.btcusdt.funding_equiv_8h.snapshot')  # input: the 08:00 settlement
    date_only = copy.deepcopy(f)
    next(x for x in date_only['facts'] if x['fact_id'] == oi)['timestamp_quality'] = 'source_date'
    no_time = copy.deepcopy(f)
    next(x for x in no_time['facts'] if x['fact_id'] == oi)['observed_at'] = None

    def outcome(fact_ids, ff=f):
        return scoring.evaluate(ff, mode='daily', assessments=[], recoveries=[
            {'incident_id': incident_id, 'news_id': 'n7', 'fact_ids': fact_ids}])

    for fact_ids, ff in (([funding], f), ([derived_old], f), ([oi, derived_old], f), ([oi], date_only), ([oi], no_time)):
        ev = outcome(fact_ids, ff)
        assert ev['trade_gate']['status'] == 'incident_hold', fact_ids
        assert 'recovery_data_not_observed_after_notice' in ev['incidents_rejected'][0]['problems']
    for fact_ids in ([oi], [derived_new]):
        ev = outcome(fact_ids)
        assert [r['incident_id'] for r in ev['incidents_released']] == [incident_id], ev['incidents_rejected']
    assert carry.recovery_problems(carry.open_incidents(hist)[0], {'news_id': 'n7', 'fact_ids': [oi]}, f) == []


def test_r2_01_incident_without_affected_sources_is_not_released_by_the_parent(built, tmp_path):
    _, facts, _ = built
    hist = tmp_path / 'history'
    t0 = as_of_of(facts)
    hack = _item('n1', 'Exchange halts bitcoin withdrawals after security breach', 'https://status.example.org/i1',
                 t0 - timedelta(hours=1), 'd' * 64)
    f1 = _edition_facts(facts, hist, t0, [hack])
    ev1 = scoring.evaluate(f1, mode='daily', assessments=[_assess(hack, impact='adverse', importance='critical')])
    incident_id = ev1['incidents_new'][0]['incident_id']
    carry.record_incidents(hist, f1, {}, ev1, edition_id='e1')
    official = _item('n7', 'Exchange resumes bitcoin withdrawals', 'https://status.example.org/r1',
                     t0 - timedelta(minutes=5), '1' * 64)
    f = _edition_facts(facts, hist, t0, [official])
    reference = f['state']['reference_price']['fact_id']
    ev = scoring.evaluate(f, mode='daily', assessments=[], recoveries=[
        {'incident_id': incident_id, 'news_id': 'n7', 'fact_ids': [reference, _oi_fact(f, 'binance_derivatives')]}])
    assert ev['trade_gate']['status'] == 'incident_hold'
    assert 'recovery_affected_sources_unspecified' in ev['incidents_rejected'][0]['problems']
    briefing = analysis_mod.briefing(f, scoring.evaluate(f, mode='daily'), edition_id='x', mode='daily', session_slot='pm')
    assert '親では解除できない' in briefing and '参照価格の新鮮さ' not in briefing
    # Only the owner's manual release clears it.
    assert carry.main(['release', '--root', str(tmp_path), '--incident', incident_id, '--reason', '社長指示']) == 0
    f4 = _edition_facts(facts, hist, t0 + timedelta(hours=12), [])
    assert scoring.evaluate(f4, mode='daily', assessments=[])['trade_gate']['status'] != 'incident_hold'


def test_r04_analysis_validates_recovery_references(built, tmp_path):
    _, facts, _ = built
    hist = tmp_path / 'history'
    t0 = as_of_of(facts)
    hack = _item('n1', 'Exchange halts bitcoin withdrawals after security breach', 'https://status.example.org/i1',
                 t0 - timedelta(hours=1), 'd' * 64)
    f1 = _edition_facts(facts, hist, t0, [hack])
    a1 = _assess(hack, impact='adverse', importance='critical', affected_source_ids=['binance_derivatives'])
    carry.record_incidents(hist, f1, {}, scoring.evaluate(f1, mode='daily', assessments=[a1]), edition_id='e1')
    incident_id = carry.open_incidents(hist)[0]['incident_id']
    official = _item('n7', 'Exchange resumes bitcoin withdrawals', 'https://status.example.org/r1', t0 - timedelta(minutes=5),
                     '1' * 64)
    f = _edition_facts(facts, hist, t0, [official])
    a = valid_analysis(f)
    for recoveries, problem in (
            ([{'incident_id': 'inc-unknown', 'news_id': 'n7', 'fact_ids': ['x']}], 'unknown_open_incident:inc-unknown'),
            ([{'incident_id': incident_id, 'news_id': 'n404', 'fact_ids': ['x']}], 'unknown_news:n404'),
            ([{'incident_id': incident_id, 'news_id': 'n7', 'fact_ids': ['btc.nope']}], 'unknown_fact:btc.nope'),
            ([{'incident_id': incident_id, 'news_id': 'n7', 'fact_ids': [_oi_fact(f, 'bybit_derivatives')]}],
             'recovery_affected_source_not_fresh'),
            ([{'incident_id': incident_id, 'news_id': 'n7', 'fact_ids': [_fresh_fact(f, 'binance_derivatives')]}],
             'recovery_data_not_observed_after_notice')):
        a['incident_recoveries'] = recoveries
        with pytest.raises(AnalysisError) as caught:
            analysis_mod.validate(a, f, edition_id=EDITION)
        assert any(problem in p for p in caught.value.problems), caught.value.problems
    a['incident_recoveries'] = [{'incident_id': incident_id, 'news_id': 'n7',
                                 'fact_ids': [_oi_fact(f, 'binance_derivatives')]}]
    analysis_mod.validate(a, f, edition_id=EDITION)
    a['news_assessments'] = [_assess(official, affected_source_ids=['binance_derivatives'])]
    with pytest.raises(AnalysisError) as caught:
        analysis_mod.validate(a, f, edition_id=EDITION)
    assert any('affected_sources_only_for_critical_incident' in p for p in caught.value.problems)


def test_r04_r05_acceptance_writes_job_history(built, tmp_path):
    """workflow.carry_forward runs on acceptance and writes both files in the job's history."""
    _, facts, _ = built
    job = workflow.Job(root=tmp_path / 'job', python=Path('/usr/bin/python3'))
    t0 = as_of_of(facts)
    hack = _item('n1', 'Exchange halts bitcoin withdrawals after security breach', 'https://status.example.org/i1',
                 t0 - timedelta(hours=1), 'd' * 64)
    f = _edition_facts(facts, job.history, t0, [hack])
    folder = job.reports / 'daily' / 'editions' / 'parent-daily-20261007T212342-0000beef'
    folder.mkdir(parents=True)
    a = {'news_assessments': [_assess(hack, impact='adverse', importance='critical')]}
    record = {'outputs': {'facts_path': str(write_json(folder / 'x.facts.json', f)),
                          'analysis_path': str(write_json(folder / 'x.analysis.json', a)),
                          'md_path': str(folder / 'x.md')}}
    out = workflow.carry_forward(job, record, 'daily')
    assert out['known_news_added'] == 1 and len(out['incidents_opened']) == 1
    assert out['incidents_open_after'] == out['incidents_opened']
    assert (job.history / carry.INCIDENT_FILE).is_file() and (job.history / carry.KNOWN_FILE).is_file()
    assert json.loads((job.history / carry.INCIDENT_FILE).read_text())['records'][0]['recorded_edition_id'] == folder.name


# ------------------------------------------------------------------ R-06 five expected US trading days

def _rows(days, total=100.0):
    return [{'trade_date': d, 'funds': ['A', 'B'], 'reported_total_musd': total, 'known_sum_musd': total,
             'table_complete': True, 'validated_total_musd': total, 'conflict': False, 'missing_funds': []} for d in days]


def _etf(collection, rows, as_of, history=None):
    S = copy.deepcopy(sources(collection))
    S['farside']['values']['rows'] = rows
    S['farside']['values']['universe'] = ['A', 'B']
    S['farside']['retrieved_at'] = as_of.isoformat()
    F = Facts(as_of, 'a' * 64)
    st = {}
    etf_facts(F, S, st, history or History(Path(tempfile.mkdtemp())), as_of)
    return F, st


def test_r06_missing_session_is_never_filled_by_an_older_row(built):
    collection, _, _ = built
    days = ['2026-09-28', '2026-09-29', '2026-09-30', '2026-10-01', '2026-10-02', '2026-10-06']  # 10-05 missing
    F, st = _etf(collection, _rows(days), datetime(2026, 10, 6, 23, 0, tzinfo=UTC))
    assert st['etf']['five_day_window'] == ['2026-09-30', '2026-10-01', '2026-10-02', '2026-10-05', '2026-10-06']
    five = F.index[st['etf']['five_day_fact_id']]
    assert five['value'] is None and five['missing_reason'] == 'five_day_window_incomplete'
    assert '2026-10-05' in five['coverage']['note']
    F, st = _etf(collection, _rows(days[:5] + ['2026-10-05', '2026-10-06']), datetime(2026, 10, 6, 23, 0, tzinfo=UTC))
    assert F.index[st['etf']['five_day_fact_id']]['value'] == 500e6


def test_r06_holidays_are_not_missing_days_and_coverage_is_explicit(built):
    collection, _, _ = built
    assert us_calendar.is_trading_day(date(2026, 9, 7)) is False  # Labor Day
    assert us_calendar.trading_days_ending(date(2026, 9, 11), 5) == [
        date(2026, 9, 4), date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10), date(2026, 9, 11)]
    days = ['2026-09-03', '2026-09-04', '2026-09-08', '2026-09-09', '2026-09-10', '2026-09-11']
    F, st = _etf(collection, _rows(days), datetime(2026, 9, 11, 21, 0, tzinfo=UTC))
    assert st['etf']['status'] == 'ok' and F.index[st['etf']['five_day_fact_id']]['value'] == 500e6
    # Tuesday morning after Labor Day: the expected session is Friday 09-04, not Monday.
    assert facts_mod.expected_etf_date(datetime(2026, 9, 8, 12, 0, tzinfo=UTC)) == date(2026, 9, 4)
    # Beyond the table's coverage end: unknown, never plain weekdays.
    assert us_calendar.is_trading_day(date(2029, 1, 3)) is None
    assert facts_mod.expected_etf_date(datetime(2029, 1, 3, 22, 0, tzinfo=UTC)) is None
    F, st = _etf(collection, _rows(['2028-12-28', '2028-12-29']), datetime(2029, 1, 3, 22, 0, tzinfo=UTC))
    assert st['etf']['status'] == 'unknown' and not scoring.bundles({'facts': F.items, 'state': st})['etf']
    assert st['etf']['trading_calendar']['coverage_end'] == us_calendar.COVERAGE_END.isoformat()


# ------------------------------------------------------------------ R-07 contiguous 1 m bars

class _Klines:
    raw_sha256 = 'b' * 64

    def __init__(self, rows):
        self.rows = rows

    def json(self):
        return self.rows


class _Fetcher:
    def __init__(self, rows):
        self.rows = rows

    def get(self, *args, **kwargs):
        return _Klines(self.rows)


def _bars(event, offsets, close_shift=0):
    out = []
    for k in offsets:
        t = int((event + timedelta(minutes=k)).timestamp() * 1000)
        out.append([t, '100', '104', '99', str(100 + k), '1', t + 59_999 + close_shift])
    return out


def test_r07_reaction_requires_contiguous_closed_bars():
    event = datetime(2026, 10, 7, 11, 0, tzinfo=UTC)
    now = event + timedelta(hours=1)
    full = reaction(_Fetcher(_bars(event, range(-6, 6))), event.isoformat(), now, [])
    assert full['status'] == 'ok' and full['bars_missing'] == [] and full['bars_required'] == 11
    assert full['return_after_5m_bps'] == round(10000 * (104 / 99 - 1), 1)
    assert full['bars'][0] == ['2026-10-07T10:54:00Z', 94.0] and full['raw_sha256'] == 'b' * 64
    # Recompute every bp value from the saved bars only.
    closes = dict((t, c) for t, c in full['bars'])
    assert full['return_before_5m_bps'] == round(10000 * (closes['2026-10-07T10:59:00Z'] / closes['2026-10-07T10:54:00Z'] - 1), 1)

    sparse = reaction(_Fetcher(_bars(event, (-6, -1, 0, 4))), event.isoformat(), now, [])  # the review's case
    # Only the -1..0 pair is contiguous: after_1m is computable, the other two segments are not.
    assert sparse['status'] == 'partial' and sparse['return_after_5m_bps'] is None
    assert sparse['return_before_5m_bps'] is None and sparse['return_after_1m_bps'] is not None
    assert len(sparse['bars_missing']) == 7

    interior = reaction(_Fetcher(_bars(event, [k for k in range(-6, 6) if k != 2])), event.isoformat(), now, [])
    assert interior['status'] == 'partial' and interior['return_after_5m_bps'] is None
    assert interior['return_before_5m_bps'] is not None and interior['return_after_1m_bps'] is not None

    rows = _bars(event, range(-6, 6))
    rows.append([rows[3][0], '100', '104', '99', '777', '1', rows[3][6]])  # same minute, different close
    dup = reaction(_Fetcher(rows), event.isoformat(), now, [])
    assert dup['status'] == 'partial' and dup['return_before_5m_bps'] is None and dup['bars_duplicated']
    rows = _bars(event, range(-6, 6))
    rows.append(list(rows[3]))  # exact duplicate is harmless
    assert reaction(_Fetcher(rows), event.isoformat(), now, [])['status'] == 'ok'

    open_bar = _bars(event, range(-6, 6))
    open_bar[10][6] = int((now + timedelta(minutes=1)).timestamp() * 1000)  # +4 min bar not closed yet
    assert reaction(_Fetcher(open_bar), event.isoformat(), now, [])['status'] == 'partial'


# ------------------------------------------------------------------ R-08 ETF first-seen per trade date

def test_r08_multi_day_append_keeps_each_trade_date(built, tmp_path):
    collection, _, _ = built
    H = History(tmp_path / 'h')
    stamp = '2026-10-07T12:00:00Z'
    rows = [dict(source='farside', instrument='all', metric='etf_row_seen', observed_at=stamp, value=1, trade_date=d)
            for d in ('2026-10-05', '2026-10-06', '2026-10-07')]
    assert H.append(rows)['added'] == 3
    assert [r['trade_date'] for r in H.read('farside', 'all', 'etf_row_seen')] == ['2026-10-05', '2026-10-06', '2026-10-07']
    assert H.append(rows)['added'] == 0

    H2 = History(tmp_path / 'h2')
    days = ['2026-09-30', '2026-10-01', '2026-10-02', '2026-10-05', '2026-10-06']
    first_at = datetime(2026, 10, 6, 23, 0, tzinfo=UTC)
    _etf(collection, _rows(days), first_at, H2)
    F, st = _etf(collection, _rows(days + ['2026-10-07']), first_at + timedelta(days=1), H2)
    seen = {f['observation_date']: f['first_seen_at'] for f in F.items if f['metric'] == 'etf_netflow_usd'}
    assert all(seen[d] == '2026-10-06T23:00:00Z' for d in days)
    assert seen['2026-10-07'] == '2026-10-07T23:00:00Z'


# ------------------------------------------------------------------ R-09 OI quote currency

def _derivatives(collection, as_of, mutate=None):
    S = copy.deepcopy(sources(collection))
    if mutate:
        mutate(S)
    F = Facts(as_of, 'a' * 64)
    st = {}
    price_facts(F, S, st)
    derivatives_facts(F, S, st, History(Path(tempfile.mkdtemp())), as_of)
    return F, S


def test_r09_oi_keeps_usdt_and_converts_only_with_fresh_usdtusd(built):
    collection, facts, _ = built
    as_of = as_of_of(facts)

    def depeg(S):
        S['kraken_usdt']['values']['mid'] = 0.98
    F, S = _derivatives(collection, as_of, depeg)
    for venue in ('binance', 'bybit', 'okx'):
        v = S[f'{venue}_derivatives']['values']
        native = next(f for f in F.items if f['metric'] == 'oi_notional_usdt' and f['venue'] == venue)
        usd = next(f for f in F.items if f['metric'] == 'oi_usd' and f['venue'] == venue)
        mark = next(f for f in F.items if f['metric'] == 'mark_price' and f['venue'] == venue)
        assert native['unit'] == 'USDT' and native['value'] == round(v['oi_btc'] * v['mark_price'], 0)
        assert usd['unit'] == 'USD' and usd['value'] == round(v['oi_btc'] * v['mark_price'] * 0.98, 0)
        assert mark['unit'] == 'USDT' and mark['fact_id'] in usd['source_fact_ids']
        assert F.index[usd['source_fact_ids'][-1]]['instrument'] == 'usdtusd'

    def no_usdt(S):
        S['kraken_usdt'].update(status='unavailable', error_kind='timeout')
    F, _ = _derivatives(collection, as_of, no_usdt)
    usd = [f for f in F.items if f['metric'] == 'oi_usd']
    assert len(usd) == 3 and all(f['value'] is None and f['missing_reason'] == 'usdtusd_conversion_missing' for f in usd)
    assert all(f['value'] is not None for f in F.items if f['metric'] == 'oi_notional_usdt')
    assert any(f['metric'] == 'funding_weighted_8h' and f['value'] is not None for f in F.items)  # weights use USDT

    def stale_usdt(S):
        S['kraken_usdt']['retrieved_at'] = (as_of - timedelta(minutes=5)).isoformat()
    F, _ = _derivatives(collection, as_of, stale_usdt)
    assert all(f['value'] is None for f in F.items if f['metric'] == 'oi_usd')


# ------------------------------------------------------------------ follow-up A: lagged fallback (design 5, 7.3)

def _partial(collection, day='2026-10-06'):
    return _farside_row(collection, day, table_complete=False, validated_total_musd=None, known_sum_musd=-66.9,
                        missing_funds=['F'] * 8)


def _etf_state(collection):
    as_of = as_of_of({'as_of': collection['collection_completed_at']})
    F = Facts(as_of, 'a' * 64)
    st = {}
    etf_facts(F, sources(collection), st, History(Path(tempfile.mkdtemp())), as_of)
    return F, st


def test_lagged_fallback_uses_the_previous_complete_window(built):
    collection, _, _ = built
    F, st = _etf_state(_partial(collection))
    etf = st['etf']
    window = ['2026-09-29', '2026-09-30', '2026-10-01', '2026-10-02', '2026-10-05']
    assert etf['status'] == 'lagged' and etf['lag_business_days'] == 1
    assert etf['trade_date'] == '2026-10-05' and etf['expected_trade_date'] == '2026-10-06'
    assert etf['five_day_window'] == window
    assert etf['lagged_fallback'] == {'reason': 'expected_day_incomplete', 'expected_trade_date': '2026-10-06',
                                      'used_trade_date': '2026-10-05'}
    assert etf['expected_day'] == {'present': True, 'complete': False, 'reconciled': False, 'conflict': False}
    five = F.index[etf['five_day_fact_id']]
    rows = {r['trade_date']: r for r in sources(collection)['farside']['values']['rows']}
    assert five['value'] == pytest.approx(sum(rows[d]['validated_total_musd'] for d in window) * 1e6)
    assert five['status'] == 'provisional' and five['observation_date'] == '2026-10-05'
    assert '1営業日遅れ' in five['coverage']['note'] and '2026-09-29〜2026-10-05' in five['coverage']['note']
    assert F.index[etf['last_day_fact_id']]['observation_date'] == '2026-10-05'
    partial_day = next(f for f in F.items if f['metric'] == 'etf_netflow_usd' and f['observation_date'] == '2026-10-06')
    assert partial_day['missing_reason'] == 'incomplete_row' and partial_day['value'] is None
    f = {'facts': F.items, 'state': st}
    assert scoring.bundles(f)['etf'] is False
    comp = scoring.etf_component(f)
    assert comp['state'] != 'unknown' and comp['lagged'] is True


def test_lagged_fallback_needs_every_previous_day(built):
    collection, _, _ = built
    c = _partial(collection)
    rows = sources(c)['farside']['values']['rows']
    rows[:] = [r for r in rows if r['trade_date'] != '2026-10-01']  # gap in the previous window
    F, st = _etf_state(c)
    assert st['etf']['status'] == 'incomplete' and st['etf']['lagged_fallback'] is None
    assert st['etf']['trade_date'] == '2026-10-06'
    assert F.index[st['etf']['five_day_fact_id']]['value'] is None
    # Conflict and complete targets are unchanged.
    F, st = _etf_state(_farside_row(collection, '2026-10-06', conflict=True, validated_total_musd=None))
    assert st['etf']['status'] == 'conflict' and st['etf']['lagged_fallback'] is None
    assert 'etf_total_mismatch' in scoring.hard_invalid({'facts': F.items, 'state': st}, as_of_of(
        {'as_of': collection['collection_completed_at']}))
    F, st = _etf_state(collection)
    assert st['etf']['status'] == 'ok' and st['etf']['lagged_fallback'] is None and st['etf']['trade_date'] == '2026-10-06'


def test_lagged_fallback_facts_to_md_html_machine(tmp_path):
    facts, ev, md, html, machine = _edition(_partial(load_collection()), tmp_path, 'lagged')
    assert not ev['bundles']['etf'] and 'etf' in machine['btc']['coverage']['missing']
    assert ev['groups']['spot_demand']['components']['etf']['lagged'] is True
    assert machine['btc']['etf']['status'] == 'partial' and machine['btc']['etf']['trade_date'] == '2026-10-05'
    line = ('期待日 2026-10-06 の行は全銘柄の値が未確定。5営業日合計は前営業日 2026-10-05 までの窓'
            '（2026-09-29〜2026-10-05）で算出（1営業日遅れ・正常な充足に数えない）')
    assert line in md and line in html
    assert 'ETF 5営業日合計（2026-09-29〜2026-10-05（NYSE営業日）・1営業日遅れ' in md


# ------------------------------------------------------------------ follow-up B: owner-only manual release

def _open_incident(built, hist):
    _, facts, _ = built
    t0 = as_of_of(facts)
    hack = _item('n1', 'Exchange halts bitcoin withdrawals after security breach', 'https://status.example.org/i1',
                 t0 - timedelta(hours=1), 'd' * 64)
    f = _edition_facts(facts, hist, t0, [hack])
    ev = scoring.evaluate(f, mode='daily', assessments=[_assess(hack, impact='adverse', importance='critical')])
    carry.record_incidents(hist, f, {}, ev, edition_id='e1')
    return facts, ev['incidents_new'][0]['incident_id']


def test_manual_release_cli(built, tmp_path, capsys):
    root = tmp_path / 'job'
    hist = root / 'history'
    facts, incident_id = _open_incident(built, hist)
    assert carry.main(['release', '--root', str(root), '--incident', 'inc-nope', '--reason', 'x']) == 2
    assert 'incident_unknown' in capsys.readouterr().out
    assert carry.main(['release', '--root', str(root), '--incident', incident_id, '--reason', '  ']) == 2
    assert 'reason_invalid' in capsys.readouterr().out
    assert carry.main(['release', '--root', str(tmp_path / 'nojob'), '--incident', incident_id, '--reason', 'x']) == 2
    assert 'history_missing' in capsys.readouterr().out
    assert carry.main(['release', '--root', str(root), '--incident', incident_id,
                       '--reason', '社長指示: 取引所の公式復旧を電話で確認']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['status'] == 'released_manually' and out['incident_id'] == incident_id
    records = json.loads((hist / carry.INCIDENT_FILE).read_text())['records']
    assert len(records) == 1  # never deleted
    assert records[0]['status'] == 'released_manually' and records[0]['released_by'] == 'owner_instruction'
    assert records[0]['release_reason'] == '社長指示: 取引所の公式復旧を電話で確認' and records[0]['released_at']
    assert carry.open_incidents(hist) == []
    f = _edition_facts(facts, hist, as_of_of(facts) + timedelta(hours=6), [])
    assert scoring.evaluate(f, mode='daily', assessments=[])['trade_gate']['status'] != 'incident_hold'
    # Not open any more: refused, file unchanged.
    before = (hist / carry.INCIDENT_FILE).read_bytes()
    assert carry.main(['release', '--root', str(root), '--incident', incident_id, '--reason', 'again']) == 2
    assert 'incident_not_open:released_manually' in capsys.readouterr().out
    assert (hist / carry.INCIDENT_FILE).read_bytes() == before


def test_manual_release_is_not_in_the_parent_workflow():
    workflow_text = (Path(__file__).parents[2] / 'btc' / 'job_template' / 'PARENT-WORKFLOW.md').read_text()
    assert 'btc.carry release' not in workflow_text and 'released_manually' not in workflow_text
    readme = (Path(__file__).parents[2] / 'docs' / 'btc' / 'README.md').read_text()
    assert '-B -m btc.carry release --root' in readme


# ------------------------------------------------------------------ R2-04 strict carried-state validation

def _carry_files(built, tmp_path):
    """A valid known-news file and a valid incidents file written by the code."""
    _, facts, _ = built
    hist = tmp_path / 'seed'
    t0 = as_of_of(facts)
    first = _item('n1', 'SEC approves in-kind creations for spot bitcoin ETFs', 'https://www.sec.gov/news/a',
                  t0 - timedelta(hours=30), 'a' * 64)
    f = _edition_facts(facts, hist, t0 - timedelta(hours=29), [first])
    carry.record_known(hist, f, {'news_assessments': [_assess(first)]}, edition_id='e0', mode='daily')
    _open_incident(built, hist)
    return {name: (hist / name).read_bytes() for name in (carry.KNOWN_FILE, carry.INCIDENT_FILE)}


def _corrupt(files, which, how):
    data = json.loads(files[which])
    if how == 'unknown_status':
        data['records'][0]['status'] = 'closed'
    elif how == 'missing_key':
        del data['records'][0]['first_known_at' if which == carry.KNOWN_FILE else 'published_at']
    elif how == 'schema_version':
        data['schema_version'] = 2
    out = dict(files)
    out[which] = b'{"schema_version": 1, "records": [' if how == 'broken_json' else json.dumps(data).encode()
    return out


@pytest.mark.parametrize('which,how,code', [
    (carry.INCIDENT_FILE, 'unknown_status', 'incidents_record_invalid'),
    (carry.INCIDENT_FILE, 'missing_key', 'incidents_record_invalid'),
    (carry.KNOWN_FILE, 'missing_key', 'known_news_record_invalid'),
    (carry.INCIDENT_FILE, 'schema_version', 'incidents_schema_version_unknown'),
    (carry.KNOWN_FILE, 'schema_version', 'known_news_schema_version_unknown'),
    (carry.INCIDENT_FILE, 'broken_json', 'incidents_json_invalid'),
    (carry.KNOWN_FILE, 'broken_json', 'known_news_json_invalid'),
])
def test_r2_04_invalid_carried_state_is_data_hold_and_never_overwritten(built, tmp_path, capsys, which, how, code):
    collection, _, _ = built
    files = _corrupt(_carry_files(built, tmp_path), which, how)
    facts, ev, md, html, machine = _edition(collection, tmp_path, 'bad', history_files=files)
    hist = tmp_path / 'bad' / 'history'
    assert facts['state']['carry'] == {'status': 'invalid', 'error': code}
    assert facts['state']['news']['carried'] == [] and facts['state']['incidents'] == {'open': []}
    assert 'carry_state_invalid' in ev['hard_invalid'] and ev['trade_gate']['status'] == 'data_hold'
    assert machine['no_trade'] is True
    for text in (md, html):
        assert '採用済みの既知ニュース・未解決障害の記録が読めない' in text
    # Acceptance skips every carry write; the CLI refuses; the bytes never change.
    job = workflow.Job(root=tmp_path / 'bad', python=Path('/usr/bin/python3'))
    folder = job.reports / 'daily' / 'editions' / 'parent-daily-20261007T212342-0000beef'
    folder.mkdir(parents=True)
    item = _item('n2', 'Exchange halts withdrawals', 'https://status.example.org/i2', as_of_of(facts), 'f' * 64)
    f = copy.deepcopy(facts)
    f['state']['news']['items'] = [item]
    a = {'news_assessments': [_assess(item, impact='adverse', importance='critical')]}
    record = {'outputs': {'facts_path': str(write_json(folder / 'x.facts.json', f)),
                          'analysis_path': str(write_json(folder / 'x.analysis.json', a)), 'md_path': str(folder / 'x.md')}}
    assert workflow.carry_forward(job, record, 'daily') == {'skipped': 'carry_state_invalid', 'error': code}
    f['state']['carry'] = {'status': 'ok'}  # even if the facts were built before the file broke
    write_json(folder / 'x.facts.json', f)
    assert workflow.carry_forward(job, record, 'daily') == {'skipped': 'carry_state_invalid', 'error': code}
    assert carry.main(['list', '--root', str(job.root)]) == 2
    assert json.loads(capsys.readouterr().out) == {'status': 'invalid', 'error': code}
    assert carry.main(['release', '--root', str(job.root), '--incident', 'inc-x', '--reason', 'owner']) == 2
    assert json.loads(capsys.readouterr().out) == {'status': 'invalid', 'error': code}
    for name, data in files.items():
        assert (hist / name).read_bytes() == data
    assert not [x for x in hist.iterdir() if x.name.endswith('.tmp')]


def test_r2_04_failed_replace_leaves_no_temporary_file(tmp_path, monkeypatch):
    hist = tmp_path / 'history'
    hist.mkdir()

    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(carry.os, 'replace', fail)
    with pytest.raises(OSError):
        carry._write(hist / carry.INCIDENT_FILE, [])
    assert list(hist.iterdir()) == []


def test_carry_list_cli_is_read_only(built, tmp_path, capsys):
    root = tmp_path / 'job'
    hist = root / 'history'
    facts, incident_id = _open_incident(built, hist)
    before = (hist / carry.INCIDENT_FILE).read_bytes()
    assert carry.main(['list', '--root', str(root)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['status'] == 'ok' and [i['incident_id'] for i in out['open_incidents']] == [incident_id]
    assert set(out['open_incidents'][0]) == {'incident_id', 'published_at', 'title', 'affected_source_ids',
                                             'recorded_edition_id', 'recorded_as_of'}
    assert (hist / carry.INCIDENT_FILE).read_bytes() == before
    readme = (Path(__file__).parents[2] / 'docs' / 'btc' / 'README.md').read_text()
    workflow_text = (Path(__file__).parents[2] / 'btc' / 'job_template' / 'PARENT-WORKFLOW.md').read_text()
    assert '-B -m btc.carry list --root' in readme and 'btc.carry list' not in workflow_text


# ------------------------------------------------------------------ R2-05 partial calendar parses

FOMC_MIXED = ('<h4>2026 FOMC Meetings</h4>'
              '<p>March</p><p>17\u201318</p>'      # broken but outside the range: ignored
              '<p>September</p><p>15-16</p>'
              '<p>October</p><p>27-28</p>'
              '<p>December</p><p>8\u20139*</p>')  # en dash: an in-range candidate that does not parse
BEA_MIXED = ('<table>'
             '<tr><td>October 29 8:30 AM</td><td>News</td><td>GDP (Advance Estimate), 3rd Quarter 2026</td></tr>'
             '<tr><td>Oct 30 8:30</td><td>News</td><td>Personal Income and Outlays, September 2026</td></tr>'
             '<tr><td>To Be Announced</td><td>News</td><td>GDP (Second Estimate), 3rd Quarter 2026</td></tr>'
             '<tr><td>sometime</td><td>News</td><td>U.S. International Trade in Goods and Services</td></tr>'
             '<tr><td>November 5 8:30 AM</td><td>News</td><td>U.S. International Trade in Goods and Services</td></tr>'
             '</table>')


def _calendar_fetcher(body):
    from btc.fetch import Deadline, Fetcher
    from tests.btc.test_fetch import FakeResponse, FakeSession
    return Fetcher(Deadline(60), session=FakeSession([FakeResponse(200, body.encode())]), sleep=lambda s: None)


def test_r2_05_fomc_partial_parse_is_partial():
    from btc.sources import calendar as cal
    today = date(2026, 10, 7)
    events, stats = cal.parse_fomc_detail(FOMC_MIXED, today)
    assert stats['candidates_in_range'] == 2 and stats['parsed_in_range'] == 1
    assert stats['unparsed'] == ['2026 December 8\u20139*']
    assert sorted({e['start_at'][:10] for e in events}) == ['2026-10-28']
    result = cal.fed_calendar(_calendar_fetcher(FOMC_MIXED), today)
    assert result.status == 'partial' and result.values['parse']['unparsed'] == stats['unparsed']
    clean = FOMC_MIXED.replace('8\u20139*', '8-9*')
    assert cal.fed_calendar(_calendar_fetcher(clean), today).status == 'ok'
    # Coverage comes only from parsed structure: a year whose meetings all fail never extends coverage.
    _, broken = cal.parse_fomc_detail('<h4>2026 FOMC Meetings</h4><p>October</p><p>27\u201328</p>', today)
    assert broken['coverage_end'] == '2026-10-06' and broken['unparsed']


def test_r2_05_bea_partial_parse_is_partial():
    from btc.sources import calendar as cal
    today = date(2026, 10, 7)
    events, stats = cal.parse_bea_detail(BEA_MIXED, today)
    assert stats['candidates'] == 3 and stats['parsed'] == 1 and stats['to_be_announced'] == 1
    assert stats['unparsed'] == ['Oct 30 8:30 | Personal Income and Outlays, September 2026']
    assert [e['id'] for e in events] == ['bea.gdp.2026-10-29']
    assert stats['coverage_end'] == '2026-11-05'  # latest parsed row of any title
    result = cal.bea_calendar(_calendar_fetcher(BEA_MIXED), today)
    assert result.status == 'partial'
    clean = BEA_MIXED.replace('Oct 30 8:30', 'October 30 8:30 AM')
    result = cal.bea_calendar(_calendar_fetcher(clean), today)
    assert result.status == 'ok' and result.values['parse']['unparsed'] == []


# ------------------------------------------------------------------ acceptance path with the production schema

def test_acceptance_writes_carry_with_a_production_schema_analysis(tmp_path, real_browser):
    """prepare -> parent analysis (schema-valid, not a stub) -> render -> accept: carry files are written strictly."""
    collection = load_collection()
    news = next(s for s in collection['sources'] if s['source_id'] == 'news')
    official = next(k for k in news['values']['kept'] if k['body'].get('status') == 'retrieved')
    official.update(official=True, publisher='Binance', code_verification='primary_body_retrieved',
                    url='https://www.binance.com/synthetic/incident-01')
    started = parse_time(collection['collection_started_at']).astimezone(JST)
    job = workflow.Job(root=tmp_path / 'job', python=Path('/usr/bin/python3'))

    def collect(job_, ctx, output):
        write_json(output, collection)
    assert workflow.prepare(job, 'daily', BtcStages(), collect, clock=lambda: started + timedelta(minutes=5),
                            started=started) == 0
    request_path = Path(json.loads((job.root / 'latest-daily.json').read_text())['request_path'])
    request = json.loads(request_path.read_text())
    facts = json.loads(Path(request['facts_path']).read_text())
    item = next(n for n in facts['state']['news']['items'] if n['id'] == official['id'])
    assert item['code_verification'] == 'primary_body_retrieved' and facts['state']['carry'] == {'status': 'ok'}
    a = valid_analysis(facts, edition_id=request_path.parent.name)
    a['news_assessments'] = [{'news_id': item['id'], 'event_cluster_id': item['event_cluster_id'],
                              'group_id': 'btc_specific_event', 'impact': 'adverse', 'importance': 'critical',
                              'verification': 'primary_confirmed', 'source_quote': item['title'][:20], 'fact_ids': [],
                              'interpretation': claim('TEST 一次本文で確認した障害。'),
                              'affected_source_ids': ['binance_derivatives']}]
    analysis_mod.validate(a, facts, edition_id=request_path.parent.name)  # the production schema, not a stub
    analysis_path = write_json(request_path.parent / 'analysis.json', a)
    package = write_json(request_path.parent / 'package.json',
                         {'request_path': str(request_path), 'analysis_path': str(analysis_path)})
    now = started + timedelta(minutes=12)
    assert workflow.render_package(job, package, 'daily', BtcStages(), now=now) == 0
    edition_path = Path(json.loads((job.root / 'latest-daily.json').read_text())['edition_path'])
    machine = json.loads(Path(json.loads(edition_path.read_text())['outputs']['json_path']).read_text())
    assert machine['no_trade'] is True
    review_path = write_json(job.work / 'review.json', full_review(edition_path))
    assert workflow.accept_review(job, review_path, 'daily', now=now) == 0
    latest = json.loads((job.root / 'latest-daily.json').read_text())
    incidents = carry.open_incidents(job.history)
    assert len(incidents) == 1 and incidents[0]['affected_source_ids'] == ['binance_derivatives']
    assert latest['carry'] == {'known_news_added': 1, 'incidents_opened': [incidents[0]['incident_id']],
                               'incidents_released': [], 'incidents_open_after': [incidents[0]['incident_id']]}
    carry.check(job.history)
    known = carry.known_news(job.history, parse_time(facts['as_of']))
    assert [k['ids'][0] for k in known] == ['url:https://www.binance.com/synthetic/incident-01']
    assert known[0]['follow_up_of'] is None
