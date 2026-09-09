from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pytest

from scripts import report_bundle as bundle
from scripts import report_release as release
from scripts import upload_report

NOW = datetime.fromisoformat('2026-09-09T18:00:00+09:00')


def sample_data():
    return {'timestamp': NOW.isoformat(), 'report_quote': {'close': 3400.5},
            'retail_sentiment': {'XAUUSD': {'long_pct': 40, 'short_pct': 60, 'avg_long_entry': 3500,
                                          'avg_short_entry': 3300, 'timestamp': NOW.isoformat()}},
            'gold_etf': {'as_of_date': '2026-09-08', 'change_5d_t': -3.5, 'change_20d_t': 17.8},
            'cot_disaggregated': {'data': {'date': '2026-09-01',
                  **{key: {'long': 10, 'short': 20, 'net': -10} for key in
                     ['managed_money', 'swap_dealers', 'producer_merchant', 'other_reportables']}}},
            'economic_calendar': {'events': [{'date': 'Thursday, September 10, 2026', 'time_jst': '21:30',
                                             'country': 'United States', 'indicator': 'PPI (MoM)'}]}}


@pytest.mark.parametrize('stamp', ['2026-09-08T18:00:00+09:00', '2026-09-09T11:59:00+09:00', '2026-09-09T18:01:00+09:00', None])
def test_stale_future_missing_timestamp(stamp):
    data = sample_data(); data['timestamp'] = stamp
    with pytest.raises(bundle.BundleError):
        bundle.observations(data, NOW)


def test_missing_price_stops_generation():
    data = sample_data(); data['report_quote']['close'] = None
    with pytest.raises(bundle.BundleError, match='Numeric observation'):
        bundle.observations(data, NOW)


def test_all_figures_derive_from_named_fields():
    data = sample_data(); result = bundle.observations(data, NOW)
    assert result['missing'] == []
    assert {f['id'] for f in result['figures']} == set(bundle.REQUIRED_FIGURES)
    bundle.check_bindings(data, result, result['figures'])
    result['figures'][0]['items'][0]['value'] = 3500  # another real price must not pass
    with pytest.raises(bundle.BundleError, match='does not match'):
        bundle.check_bindings(data, result, result['figures'])


def test_missing_figures_not_synthesized():
    data = sample_data(); data['gold_etf']['change_5d_t'] = None
    result = bundle.observations(data, NOW)
    assert any('etf' in entry for entry in result['missing'])
    assert 'etf' not in {f['id'] for f in result['figures']}


def test_missing_percentages_are_not_zero():
    data = sample_data(); data['retail_sentiment']['XAUUSD']['short_pct'] = None
    result = bundle.observations(data, NOW)
    assert 'retail' not in {f['id'] for f in result['figures']}


def test_inconsistent_cot_is_not_plotted():
    data = sample_data(); data['cot_disaggregated']['data']['managed_money']['net'] = 999
    result = bundle.observations(data, NOW)
    assert 'cot' not in {f['id'] for f in result['figures']}


class MemoryStorage:
    def __init__(self):
        self.values = {'daily/latest.html': (b'old edition', 'text/plain'), 'daily/latest.json': (b'{"old":true}', 'application/json')}
        self.calls = []
        self.fail = None
        self.mismatch = None
        self.latest_writes = 0
    def get(self, path):
        self.calls.append(('get', path))
        if self.mismatch == 'version' and '/versions/' in path:
            return b'wrong', 'text/plain'
        if self.mismatch == 'metadata' and path == 'daily/latest.json' and self.latest_writes == 1:
            self.mismatch = None
            return b'old', 'application/json'
        return self.values.get(path)
    def put(self, path, body, mime, upsert=True):
        self.calls.append(('put', path))
        if self.fail == 'version' and '/versions/' in path:
            raise release.ReleaseError('synthetic upload failure')
        if path == 'daily/latest.html': self.latest_writes += 1
        self.values[path] = (body, mime.split(';')[0])
        if self.fail == 'latest-timeout' and self.latest_writes == 1:
            raise release.ReleaseError('synthetic timeout after commit')
    def reader(self, kind):
        self.calls.append(('reader', kind))
        body, _ = self.values[f'{kind}/latest.html']
        if self.mismatch == 'hp' and self.latest_writes == 1:
            return b'old cached edition', 'text/html'
        return body, 'text/html'
    def delete(self, path):
        self.values.pop(path, None)


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    b = {'kind': 'daily', 'reportDate': '2026-09-09', 'asOf': NOW.isoformat()}
    # Transaction tests isolate acceptance; acceptance itself has adversarial tests below.
    monkeypatch.setattr(release, 'publication_inputs', lambda *args: (b, b'new edition', {'acceptanceSha256': 'a'*64}))
    return (tmp_path/'report.html', tmp_path/'report.bundle.json', tmp_path/'report.acceptance.json', tmp_path/'publication')


def test_dry_run_has_zero_network(inputs):
    store = MemoryStorage()
    result = release.release(*inputs, transport=store, now=NOW)
    assert result['publication'] == 'dry_run_validated'
    assert result['generation'] == 'succeeded'
    assert store.calls == []
    assert 'publishedAt' in result['metadata']
    assert not (inputs[3]/'last-published-daily.json').exists()


def test_publish_version_before_latest_and_verified_receipt(inputs):
    store = MemoryStorage()
    result = release.release(*inputs, publish=True, transport=store, now=NOW)
    assert result['publication'] == 'succeeded'
    assert store.calls[2][0] == 'put' and '/versions/' in store.calls[2][1]
    assert store.calls[3][0] == 'get' and '/versions/' in store.calls[3][1]
    assert store.values['daily/latest.html'][0] == b'new edition'
    m = json.loads(store.values['daily/latest.json'][0])
    assert m['sha256'] == release.sha(b'new edition')
    assert m['asOf'] == NOW.isoformat() and m['url'].endswith('/daily')
    assert (inputs[3]/'last-published-daily.json').exists()


@pytest.mark.parametrize('failure,mismatch', [('version', None), (None, 'version'), ('latest-timeout', None), (None, 'hp'), (None, 'metadata')])
def test_failures_preserve_or_restore_old_edition(inputs, failure, mismatch):
    store = MemoryStorage(); before = deepcopy(store.values)
    store.fail, store.mismatch = failure, mismatch
    with pytest.raises(release.ReleaseError, match='Publication failed'):
        release.release(*inputs, publish=True, transport=store, now=NOW)
    for key, value in before.items():
        assert store.values[key][0] == value[0]
    assert not (inputs[3]/'last-published-daily.json').exists()
    receipt = json.loads((inputs[3]/'active-publication.json').read_text())
    assert receipt['publication'] == 'failed'


def test_duplicate_publication_rejected(inputs):
    with release.release_lock(inputs[3]):
        with pytest.raises(release.ReleaseError, match='already|active'):
            release.release(*inputs, now=NOW)


def test_unresolved_crash_blocks_new_publish(inputs):
    inputs[3].mkdir()
    release.write_json(inputs[3]/'active-publication.json', {'publication': 'publishing'})
    with pytest.raises(release.ReleaseError, match='Unresolved'):
        release.release(*inputs, publish=True, transport=MemoryStorage(), now=NOW)


def test_wrong_mime_and_hash_rejected():
    for response in [(b'ok','application/octet-stream'), (b'wrong','text/html')]:
        with pytest.raises(release.ReleaseError):
            release.verify(response, b'ok', 'HP', html=True, site=True)


def test_cli_returns_failure_on_missing_or_unreviewed_html(tmp_path):
    assert upload_report.main([str(tmp_path/'no.html'), '--mode', 'daily']) == 1


def test_crash_recovery_restores_both_objects(inputs):
    store = MemoryStorage()
    directory = inputs[3]; backup = directory/'interrupted'; backup.mkdir(parents=True)
    (backup/'previous.html').write_bytes(b'previous edition')
    (backup/'previous.json').write_bytes(b'{"old":1}')
    release.write_json(directory/'active-publication.json', {
        'publication': 'publishing', 'kind': 'daily', 'backupDirectory': str(backup),
        'hadPreviousHtml': True, 'hadPreviousMetadata': True})
    result = release.recover(directory, transport=store)
    assert result['rollback'] == 'verified'
    assert store.values['daily/latest.html'][0] == b'previous edition'
    assert store.values['daily/latest.json'][0] == b'{"old":1}'


def test_wrong_mode_stops_before_network(inputs, tmp_path, monkeypatch):
    p=tmp_path/'report.html';p.write_text('html')
    p.with_suffix('.bundle.json').write_text(json.dumps({'kind':'weekly'}))
    monkeypatch.setattr(upload_report, 'release', lambda *a, **k: (_ for _ in ()).throw(AssertionError('must not release')))
    assert upload_report.upload(p,'daily',publish=True) is None


def test_optional_average_entries_are_explicitly_unavailable():
    data=sample_data(); data['retail_sentiment']['XAUUSD'].update(avg_long_entry=None, avg_short_entry=None)
    result=bundle.observations(data,NOW)
    assert result['missing']==[]
    assert result['limitations'] and '方向判断は行いません' in result['limitations'][0]
    price=next(f for f in result['figures'] if f['id']=='price')
    assert '比較不能' in price['title'] and '取得できず' in price['caption']
    assert len(price['items'])==1 and price['items'][0]['value']==3400.5
    assert 'retail' in {f['id'] for f in result['figures']}
    bundle.check_bindings(data,result,result['figures'])
