"""Bounded public HTTP fetching for BTCUSD sources.

Rules (i2-pipeline-prompt / PARENT-DECISIONS P2, P5, P8):
- per-request timeout, at most one retry, one overall time budget;
- parallel sources; one failure never stops the others;
- only fixed ``error_kind`` labels leave this module: no raw response body,
  header, exception text or secret-bearing URL is returned or logged;
- raw bodies stay in memory only; the result keeps ``raw_sha256``;
- ``retrieved_at`` (our clock) is distinct from ``observed_at`` /
  ``published_at`` (the provider's meaning) and missing values stay ``None``;
- SSRF guard (on by default): only ``https`` URLs on the default port with a
  DNS host name (no userinfo, no IP literal); redirects only to the same host;
  every connection resolves the name itself, refuses loopback / private /
  link-local / multicast / reserved / unspecified / shared addresses (also
  inside IPv4-mapped, 6to4, Teredo and NAT64 forms) and connects to the
  vetted address, so a second DNS answer cannot redirect the socket. TLS
  still verifies the certificate for the host name. No proxy or netrc
  (``trust_env = False``).

Individual sources live in ``btc/sources/`` (Phase B) and use ``Fetcher``.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
import hashlib
import ipaddress
import json
import re
import socket
import threading
import time
from typing import Callable, Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

from btc.common import UTC, parse_time

STATUSES = ('ok', 'partial', 'unavailable', 'stale')
ERROR_KINDS = (
    'timeout', 'connection', 'tls', 'http_401', 'http_403', 'http_404', 'http_429',
    'http_4xx', 'http_5xx', 'redirect_blocked', 'too_large', 'parse_error', 'invalid_payload',
    'empty_payload', 'missing_field', 'stale_observation', 'future_observation',
    'credentials_missing', 'budget_exhausted', 'not_attempted', 'not_used_by_terms',
    'source_disabled', 'url_not_allowed', 'address_not_allowed', 'unexpected',
)
RETRYABLE = {'timeout', 'http_5xx'}  # design 3.1: 401/403/404/429 are not retried
SECRET_PARAM_NAMES = {'api_key', 'apikey', 'key', 'token', 'access_token', 'secret',
                      'client_secret', 'password', 'auth', 'signature'}
DEFAULT_TIMEOUT = 20.0
CONNECT_TIMEOUT = 5.0
DEFAULT_BUDGET = 600.0  # whole collection: about ten minutes
MAX_BODY_BYTES = 20 * 1024 * 1024
USER_AGENT = 'LAA-chart-intel-btcusd/1 (+public market data; contact via repository owner)'
MAX_REDIRECTS = 3
REDIRECT_CODES = {301, 302, 303, 307, 308}
_LABEL = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
_HOSTNAME = re.compile(rf'(?=.{{1,253}}$){_LABEL}(?:\.{_LABEL})+')
_NAT64 = ipaddress.ip_network('64:ff9b::/96')


class FetchError(Exception):
    """Carries only a fixed label. ``str()`` never contains external text."""

    def __init__(self, kind: str):
        if kind not in ERROR_KINDS:
            kind = 'unexpected'
        super().__init__(kind)
        self.kind = kind


def redact_url(url: str, secret_params: Iterable[str] = ()) -> str:
    """Remove secret-bearing query parameters, userinfo and fragments."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ''
    hidden = {name.lower() for name in secret_params} | SECRET_PARAM_NAMES
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in hidden]
    host = parts.hostname or ''
    if parts.port:
        host = f'{host}:{parts.port}'
    return urlunsplit((parts.scheme, host, parts.path, urlencode(query), ''))


def classify_exception(error: BaseException) -> str:
    """Map an exception to a fixed label without reading its message."""
    if isinstance(error, FetchError):
        return error.kind
    try:
        import requests
    except ImportError:  # pragma: no cover - dependency always present
        requests = None
    if requests is not None:
        exc = requests.exceptions
        if isinstance(error, (exc.ConnectTimeout, exc.ReadTimeout, exc.Timeout)):
            return 'timeout'
        if isinstance(error, exc.SSLError):
            return 'tls'
        if isinstance(error, exc.TooManyRedirects):
            return 'redirect_blocked'
        if isinstance(error, (exc.ConnectionError, exc.ChunkedEncodingError)):
            return 'connection'
    if isinstance(error, TimeoutError):
        return 'timeout'
    if isinstance(error, (json.JSONDecodeError, UnicodeDecodeError)):
        return 'parse_error'
    if isinstance(error, (KeyError, IndexError)):
        return 'missing_field'
    if isinstance(error, (TypeError, ValueError)):
        return 'invalid_payload'
    return 'unexpected'


def status_kind(code: int) -> str | None:
    if 200 <= code < 300:
        return None
    if code in (401, 403, 404, 429):
        return f'http_{code}'
    if 300 <= code < 400:
        return 'redirect_blocked'
    if 400 <= code < 500:
        return 'http_4xx'
    return 'http_5xx'


@dataclass
class Response:
    """In-memory response. Not serialisable on purpose: bodies are never stored."""
    status_code: int
    body: bytes
    url: str  # redacted
    retrieved_at: str
    attempts: int
    elapsed_ms: float
    content_type: str = ''
    started_at: str = ''

    @property
    def raw_sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    def text(self) -> str:
        try:
            return self.body.decode('utf-8')
        except UnicodeDecodeError:
            raise FetchError('parse_error') from None

    def json(self):
        try:
            return json.loads(self.body)
        except (ValueError, UnicodeDecodeError):
            raise FetchError('parse_error') from None


class Deadline:
    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.end = clock() + seconds

    def remaining(self) -> float:
        return max(0.0, self.end - self.clock())

    def expired(self) -> bool:
        return self.remaining() <= 0


# ------------------------------------------------------------------ SSRF guard

def address_allowed(text: str) -> bool:
    """True only for a globally routable unicast address (embedded IPv4 forms checked too)."""
    try:
        ip = ipaddress.ip_address(str(text).split('%', 1)[0])
    except ValueError:
        return False
    if ip.version == 6:
        embedded = [ip.ipv4_mapped, ip.sixtofour, ip.teredo[1] if ip.teredo else None,
                    ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF) if ip in _NAT64 else None]
        if any(e is not None and not address_allowed(str(e)) for e in embedded):
            return False
    return bool(ip.is_global and not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                                      or ip.is_reserved or ip.is_unspecified))


def _ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    try:
        socket.inet_aton(host)  # 2130706433, 0x7f.1, 127.1 ...
        return True
    except OSError:
        return False


def checked_url(url: str) -> str:
    """Return the lower-case host of an allowed URL or raise ``url_not_allowed``.

    Allowed: ``https``, no explicit port, no userinfo, a DNS name (not an IP
    literal in any notation, no trailing dot, ASCII letters/digits/hyphen).
    """
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise FetchError('url_not_allowed') from None
    host = parts.hostname or ''
    if (parts.scheme != 'https' or port is not None or parts.username is not None or parts.password is not None
            or parts.netloc.lower() != host or not _HOSTNAME.fullmatch(host) or _ip_literal(host)
            or host.rsplit('.', 1)[-1].isdigit()):
        raise FetchError('url_not_allowed')
    return host


def vetted_addresses(host: str, port: int, resolver=socket.getaddrinfo) -> list[tuple]:
    """Resolve once; refuse the host when any answer is not a public address."""
    try:
        infos = resolver(host, port, 0, socket.SOCK_STREAM)
    except OSError:
        raise FetchError('connection') from None
    answers = []
    for family, _type, _proto, _canon, sockaddr in infos:
        if family not in (socket.AF_INET, socket.AF_INET6):
            raise FetchError('address_not_allowed')
        answers.append((family, sockaddr[0]))
    if not answers:
        raise FetchError('connection')
    if not all(address_allowed(ip) for _, ip in answers):
        raise FetchError('address_not_allowed')
    return answers


def guarded_session(resolver=socket.getaddrinfo, *, create_connection=None):
    """requests.Session whose https connections are pinned to vetted addresses."""
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3 import HTTPSConnectionPool
    from urllib3.connection import HTTPSConnection
    from urllib3.exceptions import ConnectTimeoutError
    from urllib3.util import connection as u3conn

    connect = create_connection or u3conn.create_connection

    class PinnedHTTPSConnection(HTTPSConnection):
        def _new_conn(self):
            # self.host stays the DNS name: SNI and certificate checks use it.
            last: Exception | None = None
            for _family, ip in vetted_addresses(self._dns_host, self.port, resolver):
                try:
                    return connect((ip, self.port), self.timeout, source_address=self.source_address,
                                   socket_options=self.socket_options)
                except socket.timeout:
                    raise ConnectTimeoutError(self, 'connect timeout') from None
                except OSError as error:
                    last = error
            raise last or OSError('no address')

    class PinnedPool(HTTPSConnectionPool):
        ConnectionCls = PinnedHTTPSConnection

    class PinnedAdapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            super().init_poolmanager(*args, **kwargs)
            self.poolmanager.pool_classes_by_scheme = {'https': PinnedPool}

        def proxy_manager_for(self, *args, **kwargs):  # never through a proxy
            raise FetchError('url_not_allowed')

    class Refuse(HTTPAdapter):
        def send(self, *args, **kwargs):
            raise FetchError('url_not_allowed')

    session = requests.Session()
    session.trust_env = False
    session.max_redirects = MAX_REDIRECTS
    session.mount('https://', PinnedAdapter())
    session.mount('http://', Refuse())
    session.headers['User-Agent'] = USER_AGENT
    return session


class Fetcher:
    """GET with timeout, one retry for transient failures, and a shared deadline.

    ``guard=False`` (tests against local servers only) disables the SSRF guard.
    """

    def __init__(self, deadline: Deadline, *, session=None, sleep=time.sleep,
                 retry_delay: float = 2.0, max_bytes: int = MAX_BODY_BYTES,
                 guard: bool = True, resolver=socket.getaddrinfo):
        self.deadline = deadline
        self.guard = guard
        self.resolver = resolver
        self.sleep = sleep
        self.retry_delay = retry_delay
        self.max_bytes = max_bytes
        self._session = session
        self._local = threading.local()

    def _get_session(self):
        if self._session is not None:
            return self._session
        session = getattr(self._local, 'session', None)
        if session is None:
            if self.guard:
                session = guarded_session(self.resolver)
            else:
                import requests
                session = requests.Session()
                session.headers['User-Agent'] = USER_AGENT
            self._local.session = session
        return session

    def get(self, url: str, *, params: dict | None = None, headers: dict | None = None,
            timeout: float = DEFAULT_TIMEOUT, secret_params: Iterable[str] = (),
            retries: int = 1, allow_redirects: bool = True) -> Response:
        retries = min(max(retries, 0), 1)
        started = time.monotonic()
        started_at = datetime.now(UTC).isoformat()
        attempts = 0
        kind = 'not_attempted'
        safe_url = redact_url(url + (('&' if '?' in url else '?') + urlencode(params) if params else ''),
                              secret_params)
        if self.guard:
            checked_url(url)  # url_not_allowed: nothing is requested
        while attempts <= retries:
            remaining = self.deadline.remaining()
            if remaining < 1.0:
                raise FetchError('budget_exhausted' if attempts == 0 else kind)
            attempts += 1
            try:
                code, body, content_type = self._attempt(url, params, headers, min(timeout, remaining),
                                                         allow_redirects)
                kind = status_kind(code)
                if kind is None:
                    if not body:
                        raise FetchError('empty_payload')
                    return Response(code, body, safe_url, datetime.now(UTC).isoformat(),
                                    attempts, round((time.monotonic() - started) * 1000, 1), content_type,
                                    started_at)
            except Exception as error:  # noqa: BLE001 - mapped to fixed labels only
                kind = classify_exception(error)
            if kind not in RETRYABLE or attempts > retries:
                break
            delay = min(self.retry_delay, max(0.0, self.deadline.remaining() - 1.0))
            if delay:
                self.sleep(delay)
        raise FetchError(kind)

    def _attempt(self, url, params, headers, limit: float, allow_redirects: bool):
        """One request whose wall-clock time is capped at ``limit`` seconds.

        requests' timeout only bounds each socket wait, and a buffered read
        returns when full or at EOF, so a slow trickle can run far past any
        deadline. The request therefore runs in a daemon thread; at the limit
        the caller shuts the socket down (ending the blocked read) and returns
        ``timeout`` without waiting further. A thread stuck before a socket
        exists (e.g. DNS) is abandoned; it never holds the collection open.
        """
        session = self._get_session()
        state: dict = {}
        finished = threading.Event()

        def work():
            response = None
            try:
                target, query = url, params
                for hop in range(MAX_REDIRECTS + 1):
                    # Redirects are followed here, one hop at a time, so each target is checked first.
                    response = session.get(target, params=query, headers=headers,
                                           timeout=(min(CONNECT_TIMEOUT, limit), limit),
                                           allow_redirects=False if self.guard else allow_redirects, stream=True)
                    location = response.headers.get('Location') if hasattr(response, 'headers') else None
                    if not (self.guard and allow_redirects and int(response.status_code) in REDIRECT_CODES
                            and location):
                        break
                    if hop == MAX_REDIRECTS:
                        raise FetchError('redirect_blocked')
                    following = urljoin(str(getattr(response, 'url', None) or target), str(location))
                    try:
                        same_host = checked_url(following) == checked_url(target)
                    except FetchError:
                        same_host = False
                    if not same_host:
                        raise FetchError('redirect_blocked')
                    response.close()
                    response, target, query = None, following, None
                state['socket'] = _socket_of(response)
                body = bytearray()
                for chunk in response.iter_content(65536):
                    if state.get('cancelled'):
                        raise FetchError('timeout')
                    body.extend(chunk)
                    if len(body) > self.max_bytes:
                        raise FetchError('too_large')
                state['result'] = (int(response.status_code), bytes(body),
                                   str(response.headers.get('Content-Type', ''))[:100])
            except BaseException as error:  # noqa: BLE001 - label only
                state['error'] = 'timeout' if state.get('cancelled') else classify_exception(error)
            finally:
                if response is not None:
                    try:
                        response.close()
                    except Exception:  # noqa: BLE001
                        pass
                finished.set()

        thread = threading.Thread(target=work, name='btc-fetch', daemon=True)
        thread.start()
        if not finished.wait(limit):
            state['cancelled'] = True
            _shutdown(state.get('socket'))
            if self._session is None:
                self._local.session = None  # the abandoned thread may still hold it
            finished.wait(min(2.0, max(0.0, self.deadline.remaining())))
            raise FetchError('timeout')
        if 'error' in state:
            raise FetchError(state['error'])
        return state['result']


def _socket_of(response):
    """Underlying socket of a streamed requests response (urllib3 1.x/2.x), or None."""
    raw = getattr(response, 'raw', None)
    for path in (('_connection', 'sock'), ('_fp', 'fp', 'raw', '_sock')):
        obj = raw
        for name in path:
            obj = getattr(obj, name, None)
            if obj is None:
                break
        if obj is not None and hasattr(obj, 'shutdown'):
            return obj
    return None


def _shutdown(sock) -> None:
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


@dataclass
class SourceResult:
    """Normalised record for one source; safe to persist and show."""
    source_id: str
    status: str = 'unavailable'
    retrieved_at: str | None = None
    observed_at: str | None = None
    published_at: str | None = None
    source_url: str | None = None
    values: dict = field(default_factory=dict)
    error_kind: str | None = None
    raw_sha256: list = field(default_factory=list)
    attempts: int = 0
    elapsed_ms: float | None = None
    notes: list = field(default_factory=list)

    def __post_init__(self):
        if self.status not in STATUSES:
            raise ValueError('invalid_status')
        if self.error_kind is not None and self.error_kind not in ERROR_KINDS:
            self.error_kind = 'unexpected'
        for key in ('retrieved_at', 'observed_at', 'published_at'):
            value = getattr(self, key)
            if value is not None:
                parse_time(value)  # offset-aware only

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def failed(cls, source_id: str, kind: str, *, source_url: str | None = None,
               retrieved_at: str | None = None, elapsed_ms: float | None = None) -> 'SourceResult':
        return cls(source_id=source_id, status='unavailable', error_kind=kind if kind in ERROR_KINDS else 'unexpected',
                   source_url=source_url, retrieved_at=retrieved_at, elapsed_ms=elapsed_ms)


def apply_freshness(result: SourceResult, max_age: timedelta, now: datetime | None = None,
                    reference: str = 'observed_at') -> SourceResult:
    """Mark an ok/partial result stale when its observation is older than max_age.

    The value is kept (``stale`` is a status, not a deletion); a future
    observation is unusable and becomes ``unavailable``.
    """
    stamp = getattr(result, reference)
    if result.status not in ('ok', 'partial') or stamp is None:
        return result
    current = now or datetime.now(UTC)
    observed = parse_time(stamp)
    if observed - current > timedelta(minutes=5):
        result.status, result.error_kind = 'unavailable', 'future_observation'
    elif current - observed > max_age:
        result.status, result.error_kind = 'stale', 'stale_observation'
    return result


@dataclass
class SourceTask:
    source_id: str
    run: Callable[[Fetcher], SourceResult]
    source_url: str | None = None  # display URL (already secret-free)


def collect_parallel(tasks: list[SourceTask], *, budget_seconds: float = DEFAULT_BUDGET,
                     max_workers: int = 8, fetcher_factory: Callable[[Deadline], Fetcher] | None = None,
                     clock: Callable[[], float] = time.monotonic) -> dict:
    """Run every source concurrently inside one budget.

    Returns ``{'results': [...], 'budget_seconds', 'elapsed_ms', 'budget_exhausted'}``
    in task order. Unfinished tasks at the deadline become
    ``unavailable / budget_exhausted``; their threads are abandoned (each
    request is itself bounded by the remaining budget).
    """
    ids = [task.source_id for task in tasks]
    if len(set(ids)) != len(ids):
        raise ValueError('duplicate_source_id')
    deadline = Deadline(budget_seconds, clock)
    fetcher = (fetcher_factory or (lambda d: Fetcher(d)))(deadline)
    started = clock()
    results: dict[str, SourceResult] = {}

    def guarded(task: SourceTask) -> SourceResult:
        t0 = time.monotonic()
        try:
            result = task.run(fetcher)
            if not isinstance(result, SourceResult) or result.source_id != task.source_id:
                raise FetchError('invalid_payload')
        except Exception as error:  # noqa: BLE001 - fixed labels only
            result = SourceResult.failed(task.source_id, classify_exception(error),
                                         source_url=task.source_url,
                                         retrieved_at=datetime.now(UTC).isoformat())
        if result.elapsed_ms is None:
            result.elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
        if result.source_url is None:
            result.source_url = task.source_url
        return result

    pool = ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(tasks) or 1)),
                              thread_name_prefix='btc-source')
    exhausted = False
    try:
        pending = {pool.submit(guarded, task): task for task in tasks}
        while pending:
            remaining = deadline.remaining()
            if remaining <= 0:
                exhausted = True
                break
            done, _ = wait(list(pending), timeout=remaining, return_when=FIRST_COMPLETED)
            for future in done:
                task = pending.pop(future)
                results[task.source_id] = future.result()
        for future, task in pending.items():
            future.cancel()
            results[task.source_id] = SourceResult.failed(task.source_id, 'budget_exhausted',
                                                          source_url=task.source_url)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return {'results': [results[i].to_dict() for i in ids], 'budget_seconds': budget_seconds,
            'elapsed_ms': round((clock() - started) * 1000, 1), 'budget_exhausted': exhausted}


def overall_status(results: list[dict]) -> str:
    """ok only when every source is ok; unavailable when none produced data."""
    states = [r.get('status') for r in results]
    if states and all(s == 'ok' for s in states):
        return 'ok'
    if not states or all(s == 'unavailable' for s in states):
        return 'unavailable'
    return 'partial'
