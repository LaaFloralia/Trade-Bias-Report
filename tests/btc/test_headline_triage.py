"""Entry-side Jev headline triage (btc.headline_triage) and the split news collection.

No real triage script, 1Password or network: the script runner is a fake.
"""
import ast
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess

import pytest

from btc import collect, headline_triage as triage, news
from btc.fetch import SourceResult, SourceTask

REPO = Path(__file__).resolve().parents[2]


def candidate(index=1, **overrides):
    row = {'id': f'n{index:03d}', 'url': f'https://www.coindesk.com/markets/2026/10/08/synthetic-{index}',
           'title': f'Synthetic bitcoin headline {index}', 'excerpt': 'Synthetic text.', 'publisher': 'CoinDesk',
           'published_at': '2026-10-07T21:00:00Z'}
    row.update(overrides)
    return row


def document(rows):
    return {'version': 1, 'candidates': rows}


def test_constants_equal_xau_triage():
    from scrapers import news_triage as xau
    assert (triage.TRIAGE, triage.THRESHOLD, triage.KEEP_MIN, triage.KEEP_MAX, triage.BUDGET_USD) == \
        (xau.TRIAGE, xau.THRESHOLD, xau.KEEP_MIN, xau.KEEP_MAX, xau.BUDGET_USD)


def test_module_is_standard_library_only():
    tree = ast.parse((REPO / 'btc/headline_triage.py').read_text())
    names = {alias.name.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    names |= {node.module.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert names <= {'__future__', 'json', 'os', 'pathlib', 're', 'stat', 'subprocess', 'tempfile', 'time', 'urllib'}


def test_valid_candidates_round_trip():
    rows = [candidate(1), candidate(2, published_at='2026-10-07T21:00:00.123456Z')]
    assert triage.validate_candidates(document(rows)) == rows


@pytest.mark.parametrize('change', [
    {'url': 'http://www.coindesk.com/a'}, {'url': 'https://user@www.coindesk.com/a'},
    {'url': 'https://www.coindesk.com:8443/a'}, {'url': 'https://localhost/a'}, {'url': 'file:///etc/passwd'},
    {'url': 'https://www.coindesk.com/' + 'a' * 500}, {'title': 'x' * 301}, {'title': ''},
    {'title': 'line\nbreak'}, {'excerpt': 'bell\x07'}, {'publisher': 'p' * 61}, {'id': 'n1'}, {'id': 'x001'},
    {'published_at': '2026-10-07 21:00:00'}, {'published_at': 1}, {'extra': 'field'},
])
def test_invalid_candidates_are_rejected(change):
    with pytest.raises(triage.TriageInputError):
        triage.validate_candidates(document([candidate(1, **change)]))


def test_shape_count_and_duplicate_ids_are_rejected():
    for data in ({'candidates': []}, {'version': 2, 'candidates': []}, document('x'),
                 document([candidate(1), candidate(1)]), document([candidate(i % 999 + 1) for i in range(201)])):
        with pytest.raises(triage.TriageInputError):
            triage.validate_candidates(data)


def test_worker_document_keeps_only_valid_public_fields():
    rows = [dict(candidate(1, title='  Synthetic\n  title\x00 '), source_id='coindesk_rss', official=False,
                 feed_raw_sha256='0' * 64, first_seen_at='2026-10-07T21:01:00Z'),
            dict(candidate(2, url='http://insecure.example/a'), source_id='x')]
    doc = triage.candidates_document(rows)
    assert [r['id'] for r in doc['candidates']] == ['n001']
    assert set(doc['candidates'][0]) == set(triage.FIELDS) and doc['candidates'][0]['title'] == 'Synthetic title'
    assert triage.validate_candidates(json.loads(json.dumps(doc)))


class FakeRun:
    def __init__(self, report, rows=None, returncode=0, raise_timeout=False):
        self.report, self.rows, self.returncode, self.raise_timeout = report, rows or [], returncode, raise_timeout
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if self.raise_timeout:
            raise subprocess.TimeoutExpired(command, kwargs['timeout'])
        out_dir = Path(command[command.index('--out-dir') + 1])
        self.sent = json.loads(Path(command[command.index('--input') + 1]).read_text())
        (out_dir / 'triage.json').write_text(json.dumps({'report': self.report, 'candidates': self.rows}))
        return subprocess.CompletedProcess(command, self.returncode, b'', b'')


@pytest.fixture
def script(tmp_path, monkeypatch):
    path = tmp_path / 'run.sh'
    path.write_text('#!/bin/bash\nexit 1\n')
    monkeypatch.setattr(triage, 'TRIAGE', str(path))
    return path


def test_run_triage_uses_xau_constants_minimal_env_and_private_tmp(script, tmp_path):
    rows = [candidate(1), candidate(2)]
    fake = FakeRun({'mode': 'jev', 'model': 'jev-1.13.0', 'input_tokens': 321},
                   [{'id': 'n001', 'p': 0.9}, {'id': 'n002', 'p': 1.5}, {'id': 'n999', 'p': 0.7}, 'junk'])
    scores, record = triage.run_triage(rows, runner=fake, environ={'USER': 'laa', 'OP_SERVICE_ACCOUNT_TOKEN': 'x'})
    assert scores == {'n001': 0.9}
    assert record['mode'] == 'jev' and record['model'] == 'jev-1.13.0' and record['input_tokens'] == 321
    assert record['candidates_sent'] == 2
    command, kwargs = fake.calls[0]
    assert command[:3] == ['/bin/bash', str(script), 'triage']
    assert command[command.index('--question') + 1] == triage.QUESTION
    assert command[command.index('--threshold') + 1] == '0.5' and command[command.index('--budget-usd') + 1] == '0.20'
    assert command[command.index('--keep-max') + 1] == '2' and kwargs['timeout'] == 25
    assert set(kwargs['env']) == {'HOME', 'USER', 'PATH', 'PYTHONDONTWRITEBYTECODE', 'TMPDIR'}
    assert Path(kwargs['env']['TMPDIR']).name.startswith('btcusd-triage-')
    assert not Path(kwargs['env']['TMPDIR']).exists()  # private directory removed afterwards
    assert fake.sent['candidates'][0] == {'id': 'n001', 'url': rows[0]['url'], 'title': rows[0]['title'],
                                          'text': 'Synthetic text.', 'author': 'CoinDesk',
                                          'date': '2026-10-07T21:00:00Z', 'kind': 'news_headline'}


@pytest.mark.parametrize('fake, environ, expected', [
    (FakeRun({'mode': 'jev'}), {'LAA_JEV_DISABLED': '1'}, {'mode': 'fallback', 'reason': 'disabled'}),
    (FakeRun({'mode': 'jev'}, returncode=2), {}, {'mode': 'fallback', 'reason': 'triage_failed'}),
    (FakeRun({'mode': 'jev'}, raise_timeout=True), {}, {'mode': 'fallback', 'reason': 'timeout'}),
    (FakeRun({'mode': 'code', 'fallback': 'budget_exceeded'}), {}, {'mode': 'fallback', 'reason': 'budget_exceeded'}),
    (FakeRun({'mode': 'code', 'fallback': 'Bad Reason!'}), {}, {'mode': 'fallback', 'reason': 'jev_unavailable'}),
])
def test_run_triage_fallbacks(script, fake, environ, expected):
    scores, record = triage.run_triage([candidate(1)], runner=fake, environ=environ)
    assert scores is None and record == expected
    if environ.get('LAA_JEV_DISABLED'):
        assert fake.calls == []


def test_run_triage_without_script_or_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(triage, 'TRIAGE', str(tmp_path / 'missing.sh'))
    assert triage.run_triage([candidate(1)], environ={}) == (None, {'mode': 'fallback', 'reason': 'triage_unavailable'})
    assert triage.run_triage([], environ={}) == ({}, {'mode': 'skipped', 'reason': 'no_candidates'})


def test_entry_triage_writes_only_validated_scores(script, tmp_path):
    staging = tmp_path / 'staging'
    staging.mkdir()
    (staging / triage.CANDIDATES_NAME).write_text(json.dumps(document([candidate(1), candidate(2)])))
    fake = FakeRun({'mode': 'jev', 'model': 'jev-1.13.0', 'input_tokens': 10}, [{'id': 'n002', 'p': 0.25}])
    record = triage.entry_triage(staging, runner=fake, environ={})
    assert record['mode'] == 'jev'
    written = json.loads((staging / triage.TRIAGE_NAME).read_text())
    assert written['scores'] == {'n002': 0.25} and set(written) == {'version', 'selection', 'scores'}
    assert oct((staging / triage.TRIAGE_NAME).stat().st_mode & 0o777) == '0o600'
    assert triage.load_triage(staging, {'n001', 'n002'}) == ({'n002': 0.25}, written['selection'])


def test_entry_triage_refuses_symlinks_fifos_and_oversize(script, tmp_path):
    secret = tmp_path / 'outside.json'
    secret.write_text(json.dumps(document([candidate(1)])))
    for kind in ('symlink', 'fifo', 'oversize', 'invalid', 'missing'):
        staging = tmp_path / kind
        staging.mkdir()
        target = staging / triage.CANDIDATES_NAME
        if kind == 'symlink':
            target.symlink_to(secret)
        elif kind == 'fifo':
            os.mkfifo(target)
        elif kind == 'oversize':
            target.write_text(' ' * (triage.MAX_CANDIDATES_BYTES + 1))
        elif kind == 'invalid':
            target.write_text(json.dumps(document([candidate(1, url='http://x.example/')])))
        fake = FakeRun({'mode': 'jev'})
        record = triage.entry_triage(staging, runner=fake, environ={})
        assert record == {'mode': 'fallback', 'reason': 'candidates_missing' if kind == 'missing'
                          else 'candidates_invalid'}, kind
        assert fake.calls == []


def test_entry_triage_replaces_a_planted_output_symlink(script, tmp_path):
    staging = tmp_path / 'staging'
    staging.mkdir()
    victim = tmp_path / 'victim.txt'
    victim.write_text('keep')
    (staging / triage.TRIAGE_NAME).symlink_to(victim)
    (staging / triage.CANDIDATES_NAME).write_text(json.dumps(document([])))
    assert triage.entry_triage(staging, environ={}) == {'mode': 'skipped', 'reason': 'no_candidates'}
    assert victim.read_text() == 'keep' and not (staging / triage.TRIAGE_NAME).is_symlink()


@pytest.mark.parametrize('data', [
    {'version': 1, 'selection': {'mode': 'jev', 'model': 'm', 'input_tokens': 1, 'elapsed_ms': 1,
                                 'candidates_sent': 1}, 'scores': {'n009': 0.5}},
    {'version': 1, 'selection': {'mode': 'jev', 'model': 'm', 'input_tokens': 1, 'elapsed_ms': 1,
                                 'candidates_sent': 1}, 'scores': {'n001': 2}},
    {'version': 1, 'selection': {'mode': 'jev', 'model': 'bad model!', 'input_tokens': 1, 'elapsed_ms': 1,
                                 'candidates_sent': 1}, 'scores': {}},
    {'version': 1, 'selection': {'mode': 'jev', 'model': 'm', 'input_tokens': 1, 'elapsed_ms': 1,
                                 'candidates_sent': 1, 'note': 'x'}, 'scores': {}},
    {'version': 1, 'selection': {'mode': 'fallback', 'reason': 'disabled'}, 'scores': {'n001': 0.5}},
    {'version': 1, 'selection': {'mode': 'fallback', 'reason': 'Free text'}, 'scores': None},
    {'version': 1, 'selection': {'mode': 'other', 'reason': 'x'}, 'scores': None},
    {'version': 1, 'selection': {'mode': 'jev', 'model': 'm', 'input_tokens': 1, 'elapsed_ms': 1,
                                 'candidates_sent': 1}, 'scores': {'n001': True}},
    {'selection': {'mode': 'fallback', 'reason': 'x'}, 'scores': None},
])
def test_worker_rejects_tampered_triage(data):
    assert triage.validate_triage(data, {'n001'}) == (None, {'mode': 'fallback', 'reason': 'triage_invalid'})


def test_worker_accepts_fallback_and_skipped_records():
    assert triage.validate_triage({'version': 1, 'selection': {'mode': 'fallback', 'reason': 'timeout'},
                                   'scores': None}, set()) == (None, {'mode': 'fallback', 'reason': 'timeout'})
    assert triage.validate_triage({'version': 1, 'selection': {'mode': 'skipped', 'reason': 'no_candidates'},
                                   'scores': {}}, set()) == ({}, {'mode': 'skipped', 'reason': 'no_candidates'})


# ------------------------------------------------------------ split collection

def headline_record(rows):
    return SourceResult('news', status='ok', retrieved_at='2026-10-07T22:00:00Z',
                        source_url='https://www.coindesk.com/arc/outboundfeeds/rss/',
                        values={'window_start': 'a', 'window_end': 'b', 'lookback_hours': 36, 'feeds': [],
                                'candidate_count': len(rows), 'candidates': rows,
                                'feeds_ok': {'general': 5, 'official': 3}, 'headline_elapsed_ms': 5}).to_dict()


def internal(index, **extra):
    return dict(candidate(index), source_id='coindesk_rss', official=False, first_seen_at='2026-10-07T22:00:00Z',
                feed_raw_sha256='0' * 64, **extra)


def test_finish_news_selects_with_entry_scores_and_drops_candidates(monkeypatch):
    monkeypatch.setattr(news, 'body_check', lambda f, item: {'status': 'retrieved', 'host': 'www.coindesk.com'})
    monkeypatch.setattr(news, 'reaction', lambda *a: {'status': 'ok'})

    class F:
        class deadline:
            remaining = staticmethod(lambda: 100.0)
    rows = [internal(i) for i in range(1, 8)]
    scores = {f'n{i:03d}': p for i, p in zip(range(1, 8), (0.9, 0.8, 0.1, 0.7, 0.6, 0.2, 0.55))}
    selection = {'mode': 'jev', 'model': 'm', 'input_tokens': 1, 'elapsed_ms': 1, 'candidates_sent': 7}
    result = news.finish_news(F(), headline_record(rows), scores, selection,
                              now=datetime(2026, 10, 7, 22, tzinfo=timezone.utc)).to_dict()
    values = result['values']
    assert 'candidates' not in values and 'headline_elapsed_ms' not in values
    assert [k['id'] for k in values['kept']] == ['n001', 'n002', 'n004', 'n005', 'n007']
    assert values['selection'] == selection and values['selection_rule'] == {'threshold': 0.5, 'keep_min': 5,
                                                                             'keep_max': 25}
    assert all(k['code_verification'] == 'secondary_body_retrieved' for k in values['kept'])
    fallback = news.finish_news(F(), headline_record(rows), None, {'mode': 'fallback', 'reason': 'disabled'},
                                now=datetime(2026, 10, 7, 22, tzinfo=timezone.utc)).to_dict()
    assert {k['p'] for k in fallback['values']['kept']} == {None} and len(fallback['values']['kept']) == 7


def test_collection_phases_and_budget(monkeypatch):
    started = datetime.now(timezone.utc)
    rows = [internal(1)]
    first = [SourceTask('news', lambda f: SourceResult(**{k: v for k, v in headline_record(rows).items()})),
             SourceTask('fgi', lambda f: SourceResult('fgi', status='ok'))]
    context = collect.collect_context('daily', started_at=started, first=first)
    assert [p['name'] for p in context['phases']] == ['context']
    assert context['phases'][0]['budget_seconds'] == 540 - 110 - 120
    assert news.headline_candidates(collect.news_record(context)) == rows
    seen = {}

    def fake_finish(f, record, scores, selection, *, now):
        seen.update(scores=scores, selection=selection, now=now)
        values = {k: v for k, v in record['values'].items() if k not in ('candidates', 'headline_elapsed_ms')}
        return SourceResult('news', status='ok', retrieved_at=record['retrieved_at'], values=dict(values, kept=[]))
    monkeypatch.setattr(news, 'finish_news', fake_finish)
    full = collect.finish_collection(json.loads(json.dumps(context)), {'n001': 0.9}, {'mode': 'jev'},
                                     second=[SourceTask('kraken_ticker', lambda f: SourceResult('kraken_ticker',
                                                                                                 status='ok'))])
    assert [p['name'] for p in full['phases']] == ['context', 'news_detail', 'quotes_and_books']
    assert full['phases'][1]['budget_seconds'] <= 120 and full['phases'][2]['budget_seconds'] <= 140
    assert seen['scores'] == {'n001': 0.9} and seen['now'] == started
    assert [s['source_id'] for s in full['sources']] == ['news', 'fgi', 'kraken_ticker']
    assert 'candidates' not in full['sources'][0]['values'] and full['collection_completed_at']
    assert full['terms_restricted'] and full['elapsed_ms'] >= 0


def test_failed_headlines_skip_news_detail():
    started = datetime.now(timezone.utc)
    context = collect.collect_context('daily', started_at=started, first=[
        SourceTask('news', lambda f: (_ for _ in ()).throw(RuntimeError('x')))])
    assert news.headline_candidates(collect.news_record(context)) == []
    full = collect.finish_collection(context, None, {'mode': 'fallback', 'reason': 'candidates_missing'}, second=[])
    assert [p['name'] for p in full['phases']] == ['context', 'quotes_and_books']
    assert full['sources'][0]['status'] == 'unavailable'
