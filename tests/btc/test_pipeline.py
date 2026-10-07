"""Phase B pipeline on a trimmed live collection (2026-10-07 21:23 JST). No network.

Covers facts and provenance (B1/B2), settled funding (B3), confidence caps
(B4), two-stage freshness (B5), parent scenarios (B6), unused liquidation
feeds (B7), CME expiry status (B8), schemas (B9), parent-analysis rejection,
MD/machine consistency, and an end-to-end edition whose news headline is
``<img src=x onerror=alert(1)>``.
"""
import copy
from datetime import timedelta
import gzip
import json
from pathlib import Path
import re

import pytest

from btc import analysis as analysis_mod, machine as machine_mod, report, scoring, workflow
from btc.acceptance import static_html_problems
from btc.collect import SOURCE_CATALOG
from btc.common import JST, JobError, parse_time, read_json, write_json
from btc.jsonschema_lite import SchemaError, validate as schema_validate
from btc.pipeline import FACTS_SCHEMA, MACHINE_SCHEMA, BtcStages
from btc.stages import AnalysisError, RunContext
from tests.btc.test_workflow import full_review, journal_contract

FIXTURE = Path(__file__).parent / 'fixtures' / 'collection-20261007.json.gz'
EDITION = 'parent-daily-20261007T212342-0000beef'
XSS = '<img src=x onerror=alert(1)>'


def load_collection() -> dict:
    return json.loads(gzip.decompress(FIXTURE.read_bytes()))


def context(root: Path, collection: dict, name=EDITION) -> RunContext:
    work = root / 'work' / name
    work.mkdir(parents=True, exist_ok=True)
    write_json(work / 'collection.json', collection)
    return RunContext(root=root, mode='daily', session_slot='pm', work_dir=work, history_dir=root / 'history',
                      started_at=parse_time(collection['collection_started_at']))


@pytest.fixture(scope='module')
def built(tmp_path_factory):
    collection = load_collection()
    ctx = context(tmp_path_factory.mktemp('job'), collection)
    facts = BtcStages().build_facts(collection, ctx)
    return collection, facts, ctx


def claim(text, ids=(), kind='hypothesis'):
    return {'text': text, 'fact_ids': list(ids), 'claim_kind': kind}


def valid_analysis(facts, edition_id=EDITION):
    ref = facts['state']['reference_price']['fact_id']
    etf5 = facts['state']['etf']['five_day_fact_id']
    return {'schema_version': analysis_mod.SCHEMA_VERSION, 'edition_id': edition_id,
            'input_manifest_sha256': facts['input_manifest_sha256'], 'requested_status': 'ready_for_validation',
            'thesis': claim('TEST 参照価格は{{fact:%s}}、ETFの5営業日合計は{{fact:%s}}。' % (ref, etf5), [ref, etf5],
                            'fact_interpretation'),
            'three_domains': {'liquidity': claim('TEST 観測板の厚い帯は短時間で消えうる。'),
                              'positioning': claim('TEST 比率は極端域にない。'),
                              'bias': claim('TEST 方向の根拠は弱い。')},
            'positioning_summary': claim('TEST 建玉の偏りは目立たない。'),
            'news_assessments': [],
            'scenarios': [{'id': 'base', 'condition_rule_ids': ['etf_direction_flip'], 'fact_ids': [etf5],
                           'expected_effect': claim('TEST ETFの向きが続けば支えになる。'),
                           'counter_case': claim('TEST 流出に転じれば支えは消える。'),
                           'invalidation_rule_ids': ['etf_direction_flip']}],
            'destination_choice': {'side': 'none', 'kind': 'none', 'level_fact_ids': [],
                                   'rationale': claim('TEST 目的地は置かない。'), 'counter_case': claim('TEST なし。'),
                                   'invalidation_rule_ids': []},
            'counter_cases': [claim('TEST マクロが逆風なら保留のまま。')],
            'reevaluation_conditions': [{'rule_id': 'source_recovers', 'fact_ids': [],
                                         'explanation': claim('TEST 欠測が回復したら再評価。')}],
            'limitations': [claim('TEST 合成した分析で、内容の判断ではない。', kind='limitation')], 'issues': []}


def index(facts):
    return {f['fact_id']: f for f in facts['facts']}


# ------------------------------------------------------------------ facts

def test_facts_schema_and_provenance(built):
    """B1 retrieval_started_at <= retrieved_at; B2 observed facts carry raw_sha256, derived facts are 'computed'."""
    _, facts, _ = built
    assert schema_validate(facts, json.loads(FACTS_SCHEMA.read_text())) == []
    ids = index(facts)
    observed = [f for f in facts['facts'] if f['evidence_kind'] == 'observed' and f['value'] is not None]
    derived = [f for f in facts['facts'] if f['evidence_kind'] == 'derived' and f['value'] is not None]
    assert observed and derived
    for f in observed:
        assert f['source_id'] != 'computed' and re.fullmatch(r'[0-9a-f]{64}', f['raw_sha256'] or ''), f['fact_id']
    for f in derived:
        assert f['source_id'] == 'computed' and f['raw_sha256'] is None, f['fact_id']
        assert re.fullmatch(r'[0-9a-f]{64}', f['input_manifest_sha256'] or '') and f['source_fact_ids'], f['fact_id']
        assert all(s in ids for s in f['source_fact_ids']), f['fact_id']
    for f in facts['facts']:
        if f['retrieval_started_at'] and f['retrieved_at']:
            assert parse_time(f['retrieval_started_at']) <= parse_time(f['retrieved_at']), f['fact_id']


def test_settled_funding_comes_from_history(built):
    """B3: settled funding uses Binance/Bybit fundingRate history and OKX realized rates, not the current rate."""
    _, facts, _ = built
    settled = [f for f in facts['facts'] if f['metric'] == 'funding_settled' and f['value'] is not None]
    assert {f['venue'] for f in settled} == {'binance', 'bybit', 'okx'}
    assert all(f['method_id'] == 'settled_funding_history' for f in settled)
    current = [f for f in facts['facts'] if f['metric'] == 'funding_current_predicted']
    assert current and not set(f['fact_id'] for f in current) & set(f['fact_id'] for f in settled)


def test_etf_in_progress_row_is_not_the_latest_day(built):
    """Farside shows today's row while the US session runs; it is excluded from the latest day and the 5-day sum."""
    _, facts, _ = built
    etf = facts['state']['etf']
    assert etf['expected_trade_date'] == '2026-10-06' and etf['trade_date'] == '2026-10-06'
    assert etf['in_progress_rows'] == ['2026-10-07'] and etf['lag_business_days'] == 0
    five = index(facts)[etf['five_day_fact_id']]
    assert five['value'] is not None and five['observation_date'] == '2026-10-06'
    assert not any('20261007' in fid for fid in etf['flow_fact_ids'])


def test_unused_and_unconfirmed_sources(built):
    """B7 no liquidation feed / CoinGlass; B8 CME expiry stays unconfirmed; Coinbase unused by terms."""
    _, facts, _ = built
    ids = {sid for sid, _ in SOURCE_CATALOG}
    assert not any('liquidation' in sid or 'forceorder' in sid.lower() for sid in ids)
    health = {h['source_id']: h for h in facts['state']['source_health']}
    assert health['coinglass']['status'] == 'not_applicable'
    assert facts['state']['calendar']['cme_expiry'] == 'unconfirmed'
    assert {x['source_id'] for x in facts['state']['terms_restricted']} == {'coinbase'}


def test_confidence_cap_lowers_the_band(built, monkeypatch):
    """B4: three supportive groups would be High; the manual calendar cap (0.60) lowers it to Med."""
    _, facts, _ = built
    f = copy.deepcopy(facts)
    f['state']['macro']['status'] = 'supportive'
    ids = index(f)
    ids[f['state']['etf']['five_day_fact_id']]['value'] = 900e6
    ids[f['state']['etf']['last_day_fact_id']]['value'] = 100e6
    monkeypatch.setattr(scoring, 'event_group', lambda *a, **k: {'state': 'supportive', 'direction': 1,
                                                                 'reason_code': 'test', 'fact_ids': [],
                                                                 'components': {}})
    ev = scoring.evaluate(f, mode='daily')
    assert ev['direction_strength'] == '高' and ev['total'] >= 5
    assert 'manual_calendar_cache_max_0.60' in ev['caps']
    assert ev['band'] == 'Med' and ev['confidence'] == 0.6


def test_live_preview_is_withheld_or_low(built):
    _, facts, _ = built
    ev = scoring.evaluate(facts, mode='daily')
    assert ev['groups']['macro']['state'] == 'unknown'  # DTWEXBGS weekly lag (documented deviation)
    assert ev['band'] == 'Low' and ev['no_trade'] is True and ev['confidence'] <= 0.2


# ------------------------------------------------------------------ parent analysis

def _news(facts, verification):
    return next(n for n in facts['state']['news']['items'] if n['code_verification'] == verification)


def _wall(facts, below: bool):
    ids = index(facts)
    ref = ids[facts['state']['reference_price']['fact_id']]['value']
    walls = [w for w in facts['state']['liquidity']['walls'] if ids[w['price_fact_id']]['value'] is not None]
    return next(w['price_fact_id'] for w in walls if (ids[w['price_fact_id']]['value'] < ref) == below)


def _option_level(facts, below: bool):
    ids = index(facts)
    ref = ids[facts['state']['reference_price']['fact_id']]['value']
    return next(c['fact_id'] for c in facts['state']['options']['clusters']
                if ids[c['fact_id']]['value'] is not None and (ids[c['fact_id']]['value'] < ref) == below)


def mutate_unsafe(a, facts):
    a['three_domains']['bias'] = claim('TEST ' + XSS)


def mutate_raw_number(a, facts):
    a['three_domains']['bias'] = claim('TEST 83000ドルが節目。')


def mutate_unknown_fact(a, facts):
    a['positioning_summary'] = claim('TEST 根拠あり。', ['btc.unknown.fact'], 'fact_interpretation')


def mutate_token_not_listed(a, facts):
    a['thesis']['fact_ids'] = a['thesis']['fact_ids'][:1]


def mutate_interpretation_without_facts(a, facts):
    a['positioning_summary'] = claim('TEST 偏りがある。', [], 'fact_interpretation')


def mutate_headline_as_fact(a, facts):
    n = _news(facts, 'headline_only')
    a['news_assessments'] = [{'news_id': n['id'], 'event_cluster_id': n['event_cluster_id'], 'group_id': 'context_only',
                              'impact': 'supportive', 'importance': 'medium', 'verification': 'headline_only',
                              'source_quote': n['title'][:20], 'fact_ids': [],
                              'interpretation': claim('TEST 見出しは上向き。', [a['thesis']['fact_ids'][0]],
                                                      'fact_interpretation')}]


def mutate_primary_without_official(a, facts):
    n = _news(facts, 'secondary_body_retrieved')
    a['news_assessments'] = [{'news_id': n['id'], 'event_cluster_id': n['event_cluster_id'], 'group_id': 'context_only',
                              'impact': 'unknown', 'importance': 'medium', 'verification': 'primary_confirmed',
                              'source_quote': n['title'][:20], 'fact_ids': [], 'interpretation': claim('TEST 参考。')}]


def mutate_quote_not_in_text(a, facts):
    mutate_primary_without_official(a, facts)
    a['news_assessments'][0].update(verification='secondary_body_confirmed', source_quote='TEST 存在しない引用')


def mutate_wrong_side(a, facts):
    a['destination_choice'].update(side='up', kind='observed_book_cluster', level_fact_ids=[_wall(facts, below=True)],
                                   invalidation_rule_ids=['book_snapshot_expired'])


def mutate_changes_requested(a, facts):
    a['requested_status'] = 'changes_requested'
    a['issues'] = [{'code': 'fact_mismatch', 'description': 'TEST', 'fact_ids': []}]


def mutate_edition(a, facts):
    a['edition_id'] = 'parent-daily-20990101T000000-ffffffff'


@pytest.mark.parametrize('mutate, problem', [
    (mutate_unsafe, 'markup_or_unsafe_characters'), (mutate_raw_number, 'raw_number_outside_tokens'),
    (mutate_unknown_fact, 'unknown_fact:btc.unknown.fact'), (mutate_token_not_listed, 'token_fact_not_listed'),
    (mutate_interpretation_without_facts, 'interpretation_without_facts'),
    (mutate_headline_as_fact, 'unverified_news_stated_as_fact'),
    (mutate_primary_without_official, 'primary_confirmed_without_official_body'),
    (mutate_quote_not_in_text, 'source_quote_not_in_retrieved_text'),
    (mutate_wrong_side, 'level_on_wrong_side'), (mutate_changes_requested, 'parent_requested_changes'),
    (mutate_edition, 'edition_id_mismatch')])
def test_parent_analysis_rejections(built, mutate, problem):
    _, facts, _ = built
    a = valid_analysis(facts)
    mutate(a, facts)
    with pytest.raises(AnalysisError) as caught:
        analysis_mod.validate(a, facts, edition_id=EDITION)
    assert any(problem in p for p in caught.value.problems), caught.value.problems


# ------------------------------------------------------------------ report and machine

def build_edition(built, analysis=None, minutes=12):
    collection, facts, ctx = built
    ctx = copy.copy(ctx)
    ctx.generated_at = (parse_time(facts['as_of']) + timedelta(minutes=minutes)).astimezone(JST)
    stages = BtcStages()
    validated = stages.validate_analysis(analysis or valid_analysis(facts), facts, ctx)
    parts = stages.build_report(validated, facts, collection, ctx)
    machine = machine_mod.build(parts.machine_core, parts.machine_extensions, mode='daily', session_slot='pm',
                                collected_at=collection['collection_started_at'],
                                generated_at=ctx.generated_at.isoformat(), as_of=facts['as_of'])
    return parts, machine


def test_report_structure_and_machine_schema(built):
    parts, machine = build_edition(built)
    BtcStages().validate_machine(machine, parts)  # B9 schema (own validator) + MD decision block
    md = parts.markdown
    assert re.match(r'^# BTCUSD チャート外分析 Daily — \d{4}-\d{2}-\d{2} \d{2}:\d{2} JST\n', md)
    headings = re.findall(r'^## (\d+)\.', md, re.M)
    assert headings == [str(i) for i in range(11)]
    assert report.LIQUIDITY_NOTE in md and '出典: Alternative.me' in md
    assert machine['research_only'] is True and machine['execution_enabled'] is False
    assert machine['btc']['confidence_kind'] == 'uncalibrated_ordinal'
    assert len(machine['btc']['analysis']['scenarios']) == 1  # B6: scenarios come from the parent
    figure_ids = {f['id'] for f in parts.figures}
    assert figure_ids <= {'etf_flows', 'derivatives_bias_funding', 'derivatives_bias_oi', 'observed_depth',
                          'positioning_distribution', 'event_timeline', 'news_reaction'}


def test_markdown_and_machine_must_agree(built):
    parts, machine = build_edition(built)
    changed = copy.deepcopy(machine)
    changed['confidence'] = 0.75
    with pytest.raises(JobError) as caught:
        BtcStages().validate_machine(changed, parts)
    assert caught.value.category == 'markdown_machine_mismatch'
    extra = copy.deepcopy(machine)
    extra['unexpected_key'] = 1
    with pytest.raises(JobError) as caught:
        BtcStages().validate_machine(extra, parts)
    assert caught.value.category == 'machine_invalid'


def test_own_schema_validator_rejects_unknown_keywords():
    with pytest.raises(SchemaError):
        schema_validate({'a': 1}, {'type': 'object', 'properties': {'a': {'type': 'integer', 'exclusiveMaximun': 3}}})
    assert schema_validate(json.loads(MACHINE_SCHEMA.read_text()), {'type': 'object'}) == []


def test_destination_expires_at_finalize(built):
    """B5: option OI is valid for 15 minutes (book walls about 60 s); finalizing later lists the fact as expired
    and drops the destination to none with the 失効 note."""
    _, facts, _ = built
    a = valid_analysis(facts)
    a['destination_choice'].update(side='up', kind='option_oi_cluster', level_fact_ids=[_option_level(facts, below=False)],
                                   invalidation_rule_ids=['option_expiry_passed'],
                                   rationale=claim('TEST 上のオプション建玉の集中。'))
    parts, machine = build_edition(built, a, minutes=1)
    assert machine['btc']['liquidity']['destination']['side'] == 'up'
    parts, machine = build_edition(built, a, minutes=120)
    dest = machine['btc']['liquidity']['destination']
    assert dest['side'] == 'none' and dest['kind'] == 'none' and dest['rationale']['text'].startswith('目的地は失効')
    assert a['destination_choice']['level_fact_ids'][0] in machine['btc']['freshness']['expired_at_finalize']
    BtcStages().validate_machine(machine, parts)


# ------------------------------------------------------------------ end to end

def with_hostile_headline(collection: dict) -> dict:
    c = copy.deepcopy(collection)
    news = next(s for s in c['sources'] if s['source_id'] == 'news')
    base = next(k for k in news['values']['kept'] if k['body'].get('status') != 'retrieved')
    hostile = copy.deepcopy(base)
    hostile.update(id='n999', event_cluster_id='c-0000000000000999', reaction=None,
                   title=XSS + ' Bitcoin [click](javascript:alert(2)) | `x`',
                   url='https://www.coindesk.com/synthetic/test-hostile-headline',
                   excerpt='<script>alert(3)</script>',
                   body={'status': 'not_attempted', 'error_kind': 'budget_exhausted'})
    news['values']['kept'].insert(0, hostile)
    return c


def test_hostile_headline_end_to_end(tmp_path, real_browser):
    """A headline carrying markup is neutralised in facts, input, MD, HTML and machine; the edition passes review."""
    collection = with_hostile_headline(load_collection())
    started = parse_time(collection['collection_started_at']).astimezone(JST)
    clock = lambda: started + timedelta(minutes=5)  # noqa: E731
    job = workflow.Job(root=tmp_path / 'job', python=Path('/usr/bin/python3'))

    def collect(job_, ctx, output):
        write_json(output, collection)
    assert workflow.prepare(job, 'daily', BtcStages(), collect, clock=clock, started=started) == 0
    state = read_json(job.root / 'latest-daily.json')
    assert state['status'] == 'awaiting_parent_authoring'
    request_path = Path(state['request_path'])
    request = read_json(request_path)
    facts = read_json(request['facts_path'])
    item = next(n for n in facts['state']['news']['items'] if 'test-hostile-headline' in n['url'])
    assert '<' not in item['title'] and '[' not in item['title'] and '|' not in item['title']
    assert '＜img src=x onerror=alert(1)＞' in item['title'] and 'javascript：' in item['title']
    briefing = Path(request['input_path']).read_text()
    assert XSS not in briefing and '<script' not in briefing

    a = valid_analysis(facts, edition_id=request_path.parent.name)
    a['news_assessments'] = [{'news_id': item['id'], 'event_cluster_id': item['event_cluster_id'],
                              'group_id': 'context_only', 'impact': 'unknown', 'importance': 'low',
                              'verification': 'headline_only', 'source_quote': 'Bitcoin', 'fact_ids': [],
                              'interpretation': claim('TEST 見出しのみで参考扱い。')}]
    analysis_path = write_json(request_path.parent / 'analysis.json', a)
    package = write_json(request_path.parent / 'package.json',
                         {'request_path': str(request_path), 'analysis_path': str(analysis_path)})
    now = started + timedelta(minutes=12)
    assert workflow.render_package(job, package, 'daily', BtcStages(), now=now) == 0
    edition_path = Path(read_json(job.root / 'latest-daily.json')['edition_path'])
    out = read_json(edition_path)['outputs']
    md = Path(out['md_path']).read_text()
    html = Path(out['html_path']).read_text()
    machine = read_json(out['json_path'])
    assert '＜img src=x onerror=alert(1)＞' in md and XSS not in md and '](javascript' not in md
    assert static_html_problems(html) == []
    assert not re.search(r'<\s*img\b', html, re.I) and not re.search(r'<\s*script\b', html, re.I)
    assert not re.search(r'<[^>]*\sonerror\s*=', html, re.I)  # only as text, never an attribute
    assert any('＜img' in n['title'] for n in machine['btc']['news'])
    assert read_json(out['render_evidence_path'])['status'] == 'passed'

    review_path = write_json(job.work / 'review.json', full_review(edition_path))
    assert workflow.accept_review(job, review_path, 'daily', now=now) == 0
    journal_contract(job, 'daily')


# ------------------------------------------------------------------ fixture hygiene (public repository)

OWN_TEXT_PATHS = {'fixture_note', 'terms_restricted/[]/detail', 'sources/[]/values/note', 'sources/[]/values/scope',
                  'sources/[]/values/underlying_scope', 'sources/[]/values/market', 'sources/[]/values/report',
                  'sources/[]/values/unit', 'sources/[]/values/series/[]/label',
                  'sources/[]/values/events/[]/name'}  # our labels + official BEA release names (US government)


def test_fixture_has_no_external_prose():
    """Headlines, excerpts and article URLs are synthetic; other multi-word strings are our labels or data names."""
    from btc.news import BODY_HOSTS
    collection = load_collection()
    news = next(s for s in collection['sources'] if s['source_id'] == 'news')
    hosts = {h for hs in BODY_HOSTS.values() for h in hs}
    for k in news['values']['kept']:
        assert re.fullmatch(r'Synthetic headline \d{2} about Bitcoin', k['title'])
        assert k['excerpt'] == '' or re.fullmatch(r'Synthetic excerpt \d{2}', k['excerpt'])
        body = k['body'].get('excerpt')
        assert body is None or re.fullmatch(r'Synthetic body excerpt \d{2} about Bitcoin', body)
        m = re.fullmatch(r'https://([a-z0-9.-]+)/synthetic/\d{2}', k['url'])
        assert m and m.group(1) in hosts
    found = set()

    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, path + [key if not re.fullmatch(r'[A-Z0-9_]+', key) else '[]'])
        elif isinstance(node, list):
            for value in node:
                walk(value, path + ['[]'])
        elif isinstance(node, str) and len(node.split()) >= 4:
            found.add('/'.join(path))
    walk(collection, [])
    news_paths = {'sources/[]/values/kept/[]/title', 'sources/[]/values/kept/[]/excerpt',
                  'sources/[]/values/kept/[]/body/excerpt'}
    assert found - news_paths <= OWN_TEXT_PATHS, found - news_paths - OWN_TEXT_PATHS
