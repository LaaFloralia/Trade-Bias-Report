"""SSRF guard for article bodies and every fetch (btc.news / btc.fetch). No network; DNS is mocked."""
import socket

import pytest
import urllib3.util.connection

from btc import news
from btc.collect import SOURCE_CATALOG
from btc.fetch import Deadline, FetchError, Fetcher, address_allowed, checked_url, vetted_addresses
from btc.sources.options import INSTRUMENT

TITLE = 'Synthetic headline about Bitcoin ETF flows'
ARTICLE = ('<html><head><title>x</title><script>ignored()</script></head><body><article>'
           + 'Synthetic article text: Synthetic headline about Bitcoin ETF flows. ' * 12 + '</article></body></html>').encode()


class FakeResponse:
    def __init__(self, code=200, body=ARTICLE, headers=None):
        self.status_code, self._body = code, body
        self.headers = {'Content-Type': 'text/html', **(headers or {})}

    def iter_content(self, size):
        yield self._body

    def close(self):
        pass


class FakeSession:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def resolver_to(*ips):
    calls = []

    def resolve(host, port, family=0, type=0, *rest):
        calls.append((host, port))
        return [(socket.AF_INET6 if ':' in ip else socket.AF_INET, socket.SOCK_STREAM, 6, '',
                 (ip, port) if ':' not in ip else (ip, port, 0, 0)) for ip in ips]
    resolve.calls = calls
    return resolve


def no_network(*args, **kwargs):
    raise AssertionError('a socket was opened')


def item(url, source_id='coindesk_rss'):
    return {'url': url, 'source_id': source_id, 'title': TITLE}


REJECTED = [
    'http://127.0.0.1:8765/x', 'http://169.254.169.254/', 'https://[::1]/', 'https://2130706433/',
    'https://evil.example/', 'https://www.coindesk.com:8443/x', 'https://user@www.coindesk.com/x',
    'http://www.coindesk.com/x', 'https://www.coindesk.com:443/x', 'https://www.coindesk.com./x',
    'https://evil.www.coindesk.com/x', 'https://coindesk.com/x', 'https://www.sec.gov/news/x',
    'https://127.1/', 'https://0x7f000001/', 'https://user:pw@www.coindesk.com/x', 'javascript:alert(1)',
    'https://www.coindesk.com@127.0.0.1/x', 'https://www.coindesk.com\\@127.0.0.1/x', '', None,
]


@pytest.mark.parametrize('url', REJECTED)
def test_body_url_outside_the_feed_host_is_never_requested(url, monkeypatch):
    monkeypatch.setattr(urllib3.util.connection, 'create_connection', no_network)
    session = FakeSession()
    resolve = resolver_to('151.101.1.1')
    f = Fetcher(Deadline(60), session=session, resolver=resolve)
    assert news.body_check(f, item(url)) == {'status': 'not_attempted', 'error_kind': 'url_not_allowed'}
    assert session.calls == [] and resolve.calls == []


def test_feed_hosts_are_per_feed():
    assert news.body_url_allowed('https://www.sec.gov/newsroom/press-releases/2026-1', 'sec_rss')
    assert not news.body_url_allowed('https://www.sec.gov/newsroom/press-releases/2026-1', 'coindesk_rss')
    assert not news.body_url_allowed('https://www.coindesk.com/x', 'unknown_feed')
    assert set(news.BODY_HOSTS) == {fid for fid, _, _ in news.GENERAL_FEEDS + news.OFFICIAL_FEEDS}


def test_allowed_url_is_fetched_without_redirects():
    session = FakeSession(FakeResponse())
    f = Fetcher(Deadline(60), session=session)
    url = 'https://www.coindesk.com/synthetic/ssrf-01'
    body = news.body_check(f, item(url))
    assert body['status'] == 'retrieved' and body['host'] == 'www.coindesk.com'
    assert len(body['excerpt']) <= 280 and 'ignored' not in body['excerpt']
    assert [c[0] for c in session.calls] == [url] and session.calls[0][1]['allow_redirects'] is False


def test_redirect_from_an_allowed_article_is_not_followed():
    session = FakeSession(FakeResponse(302, b'', {'Location': 'http://127.0.0.1:8765/x'}))
    f = Fetcher(Deadline(60), session=session)
    body = news.body_check(f, item('https://www.coindesk.com/markets/a'))
    assert body == {'status': 'unavailable', 'error_kind': 'redirect_blocked'} and len(session.calls) == 1


@pytest.mark.parametrize('ips', [('127.0.0.1',), ('10.0.0.5',), ('192.168.1.10',), ('169.254.169.254',),
                                 ('::1',), ('::ffff:127.0.0.1',), ('fe80::1',), ('0.0.0.0',),
                                 ('151.101.1.1', '172.16.0.1')])
def test_allowed_host_resolving_to_a_private_address_is_refused_before_connecting(ips, monkeypatch):
    monkeypatch.setattr(urllib3.util.connection, 'create_connection', no_network)
    resolve = resolver_to(*ips)
    f = Fetcher(Deadline(60), resolver=resolve, sleep=lambda s: None)  # real guarded requests session
    body = news.body_check(f, item('https://www.coindesk.com/markets/a'))
    assert body == {'status': 'unavailable', 'error_kind': 'address_not_allowed'}
    assert resolve.calls and all(c == ('www.coindesk.com', 443) for c in resolve.calls)


def test_connection_is_pinned_to_the_vetted_address(monkeypatch):
    """The socket goes to the IP that passed the check; DNS is not asked again (no rebinding window)."""
    opened = []

    def record(address, *args, **kwargs):
        opened.append(address)
        raise ConnectionRefusedError()
    monkeypatch.setattr(urllib3.util.connection, 'create_connection', record)
    answers = iter([[('151.101.1.1',)], [('127.0.0.1',)]])  # a rebinding DNS: public first, then loopback
    calls = []

    def resolve(host, port, *rest):
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port)) for (ip,) in next(answers)]
    f = Fetcher(Deadline(60), resolver=resolve, sleep=lambda s: None)
    with pytest.raises(FetchError) as caught:
        f.get('https://www.coindesk.com/markets/a', retries=0, allow_redirects=False)
    assert caught.value.kind == 'connection'
    assert opened == [('151.101.1.1', 443)] and calls == ['www.coindesk.com']


def test_fetcher_refuses_disallowed_urls_for_every_source():
    for url in ('https://2130706433/', 'http://api.binance.com/x', 'https://api.binance.com:8443/x',
                'https://u@api.binance.com/x', 'https://[::ffff:7f00:1]/', 'https://localhost/'):
        session = FakeSession()
        with pytest.raises(FetchError) as caught:
            Fetcher(Deadline(60), session=session).get(url)
        assert caught.value.kind == 'url_not_allowed' and session.calls == []


def test_redirects_only_to_the_same_host():
    session = FakeSession(FakeResponse(301, b'', {'Location': '/btc/'}), FakeResponse(200, b'ok'))
    assert Fetcher(Deadline(60), session=session).get('https://farside.co.uk/btc').body == b'ok'
    assert [c[0] for c in session.calls] == ['https://farside.co.uk/btc', 'https://farside.co.uk/btc/']
    for location in ('https://evil.example/', 'http://farside.co.uk/btc/', 'https://farside.co.uk:8443/',
                     'https://127.0.0.1/'):
        session = FakeSession(FakeResponse(302, b'', {'Location': location}))
        with pytest.raises(FetchError) as caught:
            Fetcher(Deadline(60), session=session).get('https://farside.co.uk/btc')
        assert caught.value.kind == 'redirect_blocked' and len(session.calls) == 1


@pytest.mark.parametrize('ip,allowed', [
    ('8.8.8.8', True), ('2606:4700::1111', True), ('151.101.1.1', True),
    ('127.0.0.1', False), ('10.1.2.3', False), ('172.16.0.1', False), ('192.168.0.1', False),
    ('169.254.169.254', False), ('100.64.0.1', False), ('0.0.0.0', False), ('224.0.0.1', False),
    ('240.0.0.1', False), ('255.255.255.255', False), ('::', False), ('::1', False), ('fe80::1%en0', False),
    ('fc00::1', False), ('ff02::1', False), ('::ffff:127.0.0.1', False), ('::ffff:10.0.0.1', False),
    ('64:ff9b::7f00:1', False), ('2002:7f00:1::', False), ('2001:db8::1', False), ('not-an-ip', False)])
def test_address_classes(ip, allowed):
    assert address_allowed(ip) is allowed


def test_vetted_addresses_rejects_mixed_answers_and_dns_failure():
    with pytest.raises(FetchError) as caught:
        vetted_addresses('x.example.com', 443, resolver_to('151.101.1.1', '::ffff:192.168.0.1'))
    assert caught.value.kind == 'address_not_allowed'

    def fail(*args):
        raise socket.gaierror('no name')
    with pytest.raises(FetchError) as caught:
        vetted_addresses('x.example.com', 443, fail)
    assert caught.value.kind == 'connection'


def test_every_configured_url_passes_the_guard():
    """Audit: all fetch roots are constant https URLs on DNS names with default ports."""
    urls = [url for _, url in SOURCE_CATALOG if url.startswith('https://')] + [url for _, _, url in news.GENERAL_FEEDS + news.OFFICIAL_FEEDS]
    for url in urls:
        checked_url(url)


@pytest.mark.parametrize('name,ok', [('BTC-16OCT26-81000-P', True), ('BTC-9JAN27-100000-C', True),
                                     ('BTC-16OCT26-81000-P&x=1', False), ('../../x', False),
                                     ('ETH-16OCT26-3000-C', False), ('BTC-16OCT26-81000-P\n', False)])
def test_deribit_instrument_parameter_is_validated(name, ok):
    assert bool(INSTRUMENT.fullmatch(name)) is ok
