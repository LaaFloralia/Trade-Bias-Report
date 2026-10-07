"""btc.fetch: bounded fetching, fixed error labels, no leakage. No network."""
from datetime import datetime, timedelta, timezone
import time

import pytest
import requests

from btc.fetch import (Deadline, FetchError, Fetcher, SourceResult, SourceTask, apply_freshness, classify_exception,
                       collect_parallel, overall_status, redact_url)

SECRET = 'sk-SECRET-VALUE-123'


class FakeResponse:
    def __init__(self, code, body=b'{"ok": 1}'):
        self.status_code, self._body, self.headers = code, body, {'Content-Type': 'application/json'}

    def iter_content(self, size):
        yield self._body

    def close(self):
        pass


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def fetcher(outcomes, budget=60):
    session = FakeSession(outcomes)
    return Fetcher(Deadline(budget), session=session, sleep=lambda s: None), session


def test_redact_removes_secret_params_and_userinfo():
    url = f'https://user:pw@api.example.com/v1/series?series_id=WALCL&api_key={SECRET}&file_type=json#frag'
    safe = redact_url(url, ['api_key'])
    assert SECRET not in safe and 'pw' not in safe and 'series_id=WALCL' in safe and '#' not in safe
    assert 'token' not in redact_url('https://x.test/?token=abc&a=1')


def test_success_records_hash_not_body():
    f, _ = fetcher([FakeResponse(200, b'{"v": 2}')])
    response = f.get('https://api.example.com/x', params={'api_key': SECRET, 'q': 1}, secret_params=['api_key'])
    assert response.json() == {'v': 2} and response.attempts == 1
    assert SECRET not in response.url and len(response.raw_sha256) == 64
    datetime.fromisoformat(response.retrieved_at)


def test_one_retry_on_transient_failure_then_success():
    f, session = fetcher([FakeResponse(503), FakeResponse(200)])
    assert f.get('https://x.test/').attempts == 2 and len(session.calls) == 2


def test_at_most_one_retry():
    f, session = fetcher([requests.exceptions.ReadTimeout(SECRET)] * 5)
    with pytest.raises(FetchError) as caught:
        f.get('https://x.test/', retries=5)
    assert caught.value.kind == 'timeout' and len(session.calls) == 2
    assert SECRET not in str(caught.value)


def test_no_retry_on_client_error():
    f, session = fetcher([FakeResponse(404), FakeResponse(200)])
    with pytest.raises(FetchError) as caught:
        f.get('https://x.test/')
    assert caught.value.kind == 'http_404' and len(session.calls) == 1


def test_budget_exhausted_before_request():
    f, session = fetcher([FakeResponse(200)], budget=0)
    with pytest.raises(FetchError) as caught:
        f.get('https://x.test/')
    assert caught.value.kind == 'budget_exhausted' and not session.calls


def test_classify_never_reads_messages():
    assert classify_exception(requests.exceptions.ConnectionError('host secret')) == 'connection'
    assert classify_exception(requests.exceptions.SSLError('x')) == 'tls'
    assert classify_exception(KeyError('price')) == 'missing_field'
    assert classify_exception(RuntimeError(SECRET)) == 'unexpected'
    assert FetchError('not-a-label').kind == 'unexpected'


def test_source_result_rejects_naive_times_and_unknown_status():
    with pytest.raises(ValueError):
        SourceResult('s', status='ok', observed_at='2026-10-07T09:00:00')
    with pytest.raises(ValueError):
        SourceResult('s', status='great')
    assert SourceResult('s', error_kind='weird').error_kind == 'unexpected'


def test_freshness_marks_stale_and_future_without_dropping_value():
    now = datetime(2026, 10, 7, 0, 0, tzinfo=timezone.utc)
    old = SourceResult('s', status='ok', observed_at='2026-10-05T00:00:00+00:00', values={'v': 1})
    apply_freshness(old, timedelta(days=1), now)
    assert old.status == 'stale' and old.error_kind == 'stale_observation' and old.values == {'v': 1}
    future = SourceResult('s', status='ok', observed_at='2026-10-07T01:00:00+00:00')
    assert apply_freshness(future, timedelta(days=1), now).status == 'unavailable'


def test_collect_parallel_isolates_failures_and_keeps_order():
    def ok(f):
        return SourceResult('a', status='ok', retrieved_at=datetime.now(timezone.utc).isoformat(), values={'x': 1})

    def boom(f):
        raise ValueError(SECRET)

    def wrong_id(f):
        return SourceResult('other', status='ok')
    result = collect_parallel([SourceTask('a', ok), SourceTask('b', boom, 'https://b.test/'),
                               SourceTask('c', wrong_id)], budget_seconds=10)
    rows = result['results']
    assert [r['source_id'] for r in rows] == ['a', 'b', 'c']
    assert rows[0]['status'] == 'ok'
    assert rows[1] == {**rows[1], 'status': 'unavailable', 'error_kind': 'invalid_payload', 'source_url': 'https://b.test/'}
    assert rows[2]['error_kind'] == 'invalid_payload'
    assert SECRET not in repr(result)
    assert overall_status(rows) == 'partial'


def test_collect_parallel_budget_marks_unfinished():
    def slow(f):
        time.sleep(1.5)
        return SourceResult('slow', status='ok')
    result = collect_parallel([SourceTask('slow', slow)], budget_seconds=0.2)
    assert result['budget_exhausted'] is True
    assert result['results'][0]['error_kind'] == 'budget_exhausted'


def test_overall_status():
    assert overall_status([{'status': 'ok'}]) == 'ok'
    assert overall_status([{'status': 'unavailable'}]) == 'unavailable'
    assert overall_status([]) == 'unavailable'
    assert overall_status([{'status': 'ok'}, {'status': 'stale'}]) == 'partial'
