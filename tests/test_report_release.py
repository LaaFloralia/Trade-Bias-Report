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


@pytest.mark.parametrize('had_previous_record', [False, True])
def test_success_marker_crash_recovery_allows_the_next_publish(inputs, monkeypatch, had_previous_record):
    class SimulatedProcessExit(BaseException):
        pass
    store = MemoryStorage()
    before = deepcopy(store.values)
    known_path = inputs[3] / 'last-published-daily.json'
    if had_previous_record:
        release.write_json(known_path, {'publication': 'succeeded', 'attemptId': 'previous-attempt',
                                      'metadata': {'sha256': release.sha(before['daily/latest.html'][0])}})
    previous_record = known_path.read_bytes() if known_path.exists() else None
    original_write = release.write_json
    interrupted = False

    def crash_after_success_marker(path, value):
        nonlocal interrupted
        original_write(path, value)
        if Path(path) == known_path and value.get('publication') == 'succeeded' and not interrupted:
            interrupted = True
            raise SimulatedProcessExit()

    monkeypatch.setattr(release, 'write_json', crash_after_success_marker)
    with pytest.raises(SimulatedProcessExit):
        release.release(*inputs, publish=True, transport=store, now=NOW)
    assert json.loads(known_path.read_text())['metadata']['sha256'] == release.sha(b'new edition')
    assert json.loads((inputs[3]/'active-publication.json').read_text())['publication'] == 'publishing'
    monkeypatch.setattr(release, 'write_json', original_write)
    assert release.recover(inputs[3], transport=store)['rollback'] == 'verified'
    for key, value in before.items():
        assert store.values[key][0] == value[0]
    if previous_record is None:
        assert not known_path.exists()
    else:
        assert known_path.read_bytes() == previous_record
    assert release.release(*inputs, publish=True, transport=store, now=NOW)['publication'] == 'succeeded'


def test_success_marker_write_error_rolls_back_all_three_records(inputs, monkeypatch):
    store = MemoryStorage()
    before = deepcopy(store.values)
    known_path = inputs[3] / 'last-published-daily.json'
    release.write_json(known_path, {'publication': 'succeeded',
                                  'metadata': {'sha256': release.sha(before['daily/latest.html'][0])}})
    previous_record = known_path.read_bytes()
    original_write = release.write_json
    interrupted = False

    def fail_after_success_marker(path, value):
        nonlocal interrupted
        original_write(path, value)
        if Path(path) == known_path and value.get('publication') == 'succeeded' and not interrupted:
            interrupted = True
            raise OSError('synthetic marker write failure')

    monkeypatch.setattr(release, 'write_json', fail_after_success_marker)
    with pytest.raises(release.ReleaseError, match='rollback=verified'):
        release.release(*inputs, publish=True, transport=store, now=NOW)
    assert known_path.read_bytes() == previous_record
    for key, value in before.items():
        assert store.values[key][0] == value[0]
    monkeypatch.setattr(release, 'write_json', original_write)
    assert release.release(*inputs, publish=True, transport=store, now=NOW)['publication'] == 'succeeded'


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


def test_in_progress_edition_stays_fresh_across_midnight_without_relabeling():
    data=sample_data();data['timestamp']='2026-09-09T23:45:00+09:00'
    stamp=bundle.require_fresh(data,datetime.fromisoformat('2026-09-10T00:05:00+09:00'))
    assert stamp.date().isoformat()=='2026-09-09'


def test_next_24h_calendar_coverage_keeps_ecb_and_jobless_claims():
    data=sample_data();start='2026-09-09T23:39:00+09:00'
    data['economic_calendar']['events'] += [
        {'date':'Thursday, September 10, 2026','time_jst':'21:15','country':'Euro Zone','indicator':'ECB Interest Rate Decision'},
        {'date':'Thursday, September 10, 2026','time_jst':'21:30','country':'United States','indicator':'Initial Jobless Claims'},
        {'date':'Friday, September 11, 2026','time_jst':'21:30','country':'United States','indicator':'CPI'}]
    result=bundle.calendar_events_24h(data,start)
    assert len(result)==3 and any('ECB' in e for e in result) and any('Initial Jobless' in e for e in result)
    assert not any('CPI' in e for e in result)


def test_etf_countertrend_is_retained_in_figure_and_summary_source():
    data=sample_data();data['gold_etf'].update(streak_days=3,streak_direction='outflow')
    result=bundle.observations(data,NOW)
    figure=next(f for f in result['figures'] if f['id']=='etf')
    assert '3営業日連続の保有減' in figure['caption']
    assert any('3営業日連続の保有減' in note for note in result['limitations'])


def test_summary_preserves_observation_deadline_and_both_plan_conditions():
    source = '''# Report
## セクション0: エグゼクティブサマリー

信頼度: Low ｜ スコア 0
XAUUSD: プランAは未成立。方向未設定。
観察時間: 9/10 01:00まで。執行は保留。
最大リスク: 構造の欠測。

## セクション1: 今夜の執行プラン
### 1-1. プランA（本命）

D1・H1構造が取得不可のため、方向は未設定。

執行再検討時はCBDR / Asian Rangeを確認する。

### 1-2. プランB（同一銘柄・逆条件シナリオ）

D1・H1取得後に逆方向の構造を確認できた場合は再評価する。
確認できなければ様子見を継続する。

### 1-3. スコア

合計0。
'''
    summary = bundle.make_summary(source, 'daily', NOW.isoformat())
    conditions = summary['conditions']
    assert len(conditions) == 4
    assert conditions[0]['text'] == '9/10 01:00まで。執行は保留。'
    assert 'プランA' in conditions[2]['title']
    assert 'CBDR / Asian Range' in conditions[2]['text']
    assert '取得不可' in conditions[2]['text']
    assert 'プランB' in conditions[3]['title']
    assert conditions[3]['text'].endswith('確認できなければ様子見を継続する。')
    assert all(c['source_quote'] in source for c in conditions)


def test_weekly_plan_tables_keep_invalidation_and_counter_scenario():
    source = '''# Weekly
## セクション0: エグゼクティブサマリー

来週の方向は未設定。条件の取得後に再評価する。

## セクション8: 来週の注目シナリオ
### 8-1. プラン1

| 項目 | 内容 |
|---|---|
| 方向 | 未設定 |
| 無効化レベル | 構造未取得のため未設定 |
| チャート確認 | D1 / H1の取得後に再評価 |

**スコア内訳表（必須）:**

| # | 項目 | 点 |
|---|---|---|
| 1 | 構造 | 0 |

### 8-2. プラン2

プラン1の構造を確認するまで対抗シナリオも未設定。
確認できなければ様子見を継続する。
'''
    result = bundle.make_summary(source, 'weekly', NOW.isoformat())['conditions']
    assert len(result) == 2
    assert result[0]['text'].find('無効化レベル: 構造未取得のため未設定') >= 0
    assert 'D1 / H1の取得後に再評価' in result[0]['text']
    assert '|' not in result[0]['text']
    assert result[1]['text'].endswith('確認できなければ様子見を継続する。')
    assert all(c['source_quote'] in source for c in result)
