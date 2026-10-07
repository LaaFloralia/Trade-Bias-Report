"""Workflow states, hashes, editions, latest manifest and parent review (no network)."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import re

import pytest

from btc import workflow
from btc.acceptance import CHECKS
from btc.common import JST, digest, read_json, write_json
from btc.stages import PendingStages
from tests.btc.fakes import FakeStages, fake_collect

ISO_OFFSET = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?[+-]\d{2}:\d{2}$')


def make_job(tmp_path):
    return workflow.Job(root=tmp_path / 'job', python=Path('/usr/bin/python3'))


def latest(job, mode='daily'):
    return read_json(job.root / f'latest-{mode}.json')


def prepare(job, mode='daily', stages=None, capsys=None):
    code = workflow.prepare(job, mode, stages or FakeStages(), fake_collect())
    request = latest(job, mode).get('request_path')
    return code, request


def write_package(request_path, analysis=None):
    folder = Path(request_path).parent
    analysis_path = write_json(folder / 'analysis.json', analysis or
                               {'conclusion': 'TEST の合成結論', 'bias': 0.25, 'confidence': 0.4,
                                'fact_ids': ['F_TEST_FLOW_1D']})
    return write_json(folder / 'package.json', {'request_path': str(request_path), 'analysis_path': str(analysis_path)})


def full_review(edition_path, **overrides):
    edition = read_json(edition_path)
    out = edition['outputs']
    render = read_json(out['render_evidence_path'])
    bundle = read_json(out['bundle_path'])
    review = {'edition_path': str(edition_path), 'status': 'parent_passed', 'reviewer': 'parent-ciel',
              'independentProcess': False, 'reviewedAt': datetime.now(JST).isoformat(), 'issues': [],
              'limitations': ['TEST'],
              'checks': {k: {'passed': True, 'evidence': f'TEST {k}'} for k in CHECKS},
              'imagesReviewed': [{'path': x['path'], 'sha256': x['sha256']} for x in render['images']],
              'htmlSha256': digest(out['html_path']), 'bundleSha256': digest(out['bundle_path']),
              'renderSha256': digest(out['render_evidence_path']), 'machineSha256': digest(out['json_path']),
              'factsSha256': digest(bundle['factsPath'])}
    review.update(overrides)
    return review


def test_session_slot():
    assert workflow.session_slot('daily', datetime(2026, 10, 7, 9, 0, tzinfo=JST)) == 'am'
    assert workflow.session_slot('daily', datetime(2026, 10, 7, 18, 0, tzinfo=JST)) == 'pm'
    assert workflow.session_slot('weekly', datetime(2026, 10, 10, 7, 0, tzinfo=JST)) == 'weekly'


def test_prepare_writes_request_and_latest(tmp_path):
    job = make_job(tmp_path)
    code, request_path = prepare(job)
    assert code == 0
    request = read_json(request_path)
    assert request['status'] == 'awaiting_parent_authoring' and request['symbol'] == 'BTCUSD'
    assert re.match(r'^parent-daily-\d{8}T\d{6}-[0-9a-f]{8}$', Path(request_path).parent.name)
    for key in ('data', 'facts', 'input', 'analysis_schema'):
        assert digest(request[f'{key}_path']) == request[f'{key}_sha256']
    assert request['session_slot'] in ('am', 'pm') and ISO_OFFSET.match(request['collected_at'])
    state = latest(job)
    assert state['status'] == 'awaiting_parent_authoring' and state['symbol'] == 'BTCUSD'
    assert oct(Path(request_path).stat().st_mode & 0o777) == '0o600'
    assert oct(Path(request_path).parent.stat().st_mode & 0o777) == '0o700'
    log = [json.loads(x) for x in (job.logs / 'runs.jsonl').read_text().splitlines()]
    assert [x['status'] for x in log] == ['running', 'awaiting_parent_authoring']


def test_pending_stages_fail_with_fixed_label(tmp_path):
    job = make_job(tmp_path)
    assert workflow.prepare(job, 'daily', PendingStages(), fake_collect()) == 1
    state = latest(job)
    assert state['status'] == 'failed' and state['error_category'] == 'pipeline_not_configured'
    assert state['symbol'] == 'BTCUSD'


def test_collection_failure_is_needs_attention(tmp_path):
    from btc.common import JobError
    job = make_job(tmp_path)

    def broken(job, ctx, output):
        raise JobError('credentials_unavailable')
    assert workflow.prepare(job, 'daily', FakeStages(), broken) == 1
    state = latest(job)
    assert state['status'] == 'needs_attention' and state['error_category'] == 'credentials_unavailable'


def test_stale_collection_rejected(tmp_path):
    job = make_job(tmp_path)
    old = datetime.now(JST) - timedelta(hours=7)
    assert workflow.prepare(job, 'daily', FakeStages(), fake_collect(old)) == 1
    assert latest(job)['error_category'] == 'collection_stale'


def test_render_rejects_changed_facts(tmp_path):
    job = make_job(tmp_path)
    _, request_path = prepare(job)
    facts_path = Path(read_json(request_path)['facts_path'])
    facts_path.write_text(facts_path.read_text().replace('125.5', '999.5'))
    package = write_package(request_path)
    assert workflow.render_package(job, package, 'daily', FakeStages(), browser_check=lambda *a, **k: None) == 1
    state = latest(job)
    assert state['status'] == 'needs_attention' and state['error_category'] == 'input_changed'


def test_render_rejects_invalid_analysis_with_problems(tmp_path, capsys):
    job = make_job(tmp_path)
    _, request_path = prepare(job)
    package = write_package(request_path, {'conclusion': 'x', 'fact_ids': ['F_NOPE']})
    assert workflow.render_package(job, package, 'daily', FakeStages()) == 1
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out['error_category'] == 'analysis_rejected'
    assert 'unknown_fact:F_NOPE' in out['analysis_problems'] and 'missing:bias' in out['analysis_problems']


def test_render_rejects_mode_mismatch_and_outside_paths(tmp_path):
    job = make_job(tmp_path)
    _, request_path = prepare(job)
    package = write_package(request_path)
    assert workflow.render_package(job, package, 'weekly', FakeStages()) == 1
    assert latest(job, 'weekly')['error_category'] == 'mode_mismatch'
    outside = write_json(tmp_path / 'package.json', {'request_path': str(request_path), 'analysis_path': 'x'})
    assert workflow.render_package(job, outside, 'daily', FakeStages()) == 1
    assert latest(job)['error_category'] == 'path_outside_job'


def test_symlinked_package_rejected(tmp_path):
    job = make_job(tmp_path)
    _, request_path = prepare(job)
    package = write_package(request_path)
    link = Path(request_path).parent.parent / 'linked'
    link.symlink_to(Path(request_path).parent)
    assert workflow.render_package(job, link / package.name, 'daily', FakeStages()) == 1
    assert latest(job)['error_category'] == 'symlink_input'


def test_machine_contract_enforced(tmp_path):
    job = make_job(tmp_path)
    _, request_path = prepare(job)
    package = write_package(request_path, {'conclusion': 'x', 'bias': 2.0, 'confidence': 0.4})
    assert workflow.render_package(job, package, 'daily', FakeStages(), browser_check=lambda *a, **k: None) == 1
    assert latest(job)['error'] == 'MachineError'


@pytest.fixture
def edition(tmp_path, real_browser):
    job = make_job(tmp_path)
    _, request_path = prepare(job)
    package = write_package(request_path)
    assert workflow.render_package(job, package, 'daily', FakeStages()) == 0
    state = latest(job)
    assert state['status'] == 'awaiting_parent_review' and state['symbol'] == 'BTCUSD'
    return job, Path(state['edition_path'])


def journal_contract(job, mode):
    """Python mirror of journal src/lib/chart-intel/files.ts (readVerifiedReport, symbolRequired)."""
    report = latest(job, mode)
    assert report['mode'] == mode and report['symbol'] == 'BTCUSD'
    assert report['status'] == 'succeeded_local' and report['ok'] is True
    assert report['generation_status'] == 'succeeded' and report['review_status'] == 'parent_passed'
    assert report['historical_revision'] is False
    assert ISO_OFFSET.match(report['collected_at']) and ISO_OFFSET.match(report['generated_at'])
    base = (job.root / 'reports' / mode / 'editions').resolve()
    out = report['outputs']
    files = [Path(out[k]).resolve() for k in ('html_path', 'parent_review_path', 'bundle_path')]
    assert all(f.is_relative_to(base) and f.parent.parent == base for f in files)
    assert len({f.parent for f in files}) == 1
    assert files[0].stat().st_size <= 2_000_000
    review = read_json(files[1])
    assert review['status'] == 'parent_passed'
    assert review['htmlSha256'] == digest(files[0]) and review['bundleSha256'] == digest(files[2])


def test_full_flow_reaches_journal_contract(edition):
    job, edition_path = edition
    record = read_json(edition_path)
    out = record['outputs']
    name = Path(out['md_path']).name
    assert re.match(r'^BTCUSD_Daily_Report_\d{4}-\d{2}-\d{2}_\d{4}\.md$', name)
    assert Path(out['md_path']).parent.name == Path(record['request_path']).parent.name
    html = Path(out['html_path']).read_text()
    assert '<meta name="report-symbol" content="BTCUSD">' in html
    assert not re.search(r'<\s*script\b', html, re.I)
    machine = read_json(out['json_path'])
    assert machine['symbol'] == 'BTCUSD' and machine['bias'] == 0.25 and machine['btc'] == {'test': True}
    assert machine['session_slot'] == record['session_slot'] and machine['historical_revision'] is False
    render = read_json(out['render_evidence_path'])
    assert {v['label'] for v in render['viewports']} == {'desktop', 'wide', 'mobile'}
    names = {Path(x['path']).name for x in render['images']}
    assert {'desktop-overview.png', 'wide-overview.png', 'mobile-overview.png', 'desktop-figure-1.png',
            'mobile-figure-1.png'} <= names
    assert any(n.startswith('mobile-table-right-') for n in names)
    review_path = write_json(job.work / 'review.json', full_review(edition_path))
    assert workflow.accept_review(job, review_path, 'daily') == 0
    journal_contract(job, 'daily')
    previous = workflow.previous_passed_editions(job, 'daily')
    assert previous and previous[0]['edition_path'] == str(edition_path)


@pytest.mark.parametrize('change, category', [
    ({'independentProcess': True}, 'parent_review_failed'),
    ({'reviewer': 'someone'}, 'parent_review_failed'),
    ({'htmlSha256': '0' * 64}, 'parent_review_failed'),
    ({'factsSha256': '0' * 64}, 'parent_review_failed'),
    ({'imagesReviewed': []}, 'parent_review_failed'),
    ({'checks': {'facts_match_sources': {'passed': True, 'evidence': 'x'}}}, 'parent_review_failed'),
])
def test_review_rejections(edition, change, category):
    job, edition_path = edition
    review_path = write_json(job.work / 'review.json', full_review(edition_path, **change))
    assert workflow.accept_review(job, review_path, 'daily') == 1
    state = latest(job)
    assert state['status'] == 'needs_attention' and state['error_category'] == category


def test_review_requires_every_figure_and_overview(edition):
    job, edition_path = edition
    review = full_review(edition_path)
    review['imagesReviewed'] = [x for x in review['imagesReviewed'] if 'wide-overview' not in x['path']]
    assert workflow.accept_review(job, write_json(job.work / 'review.json', review), 'daily') == 1


def test_review_with_issues_is_changes_requested(edition):
    job, edition_path = edition
    review_path = write_json(job.work / 'review.json', full_review(edition_path, issues=['TEST 指摘']))
    assert workflow.accept_review(job, review_path, 'daily') == 2
    state = latest(job)
    assert state['status'] == 'needs_attention' and state['review_status'] == 'changes_requested'
    assert workflow.previous_passed_editions(job, 'daily') == []


def test_review_rejects_changed_machine(edition):
    job, edition_path = edition
    machine = Path(read_json(edition_path)['outputs']['json_path'])
    review = full_review(edition_path)
    machine.write_text(machine.read_text().replace('"bias": 0.25', '"bias": 0.5'))
    assert workflow.accept_review(job, write_json(job.work / 'review.json', review), 'daily') == 1
