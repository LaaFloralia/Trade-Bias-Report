"""A1: a request never outlives min(start + timeout, deadline), even when the body trickles in."""
from __future__ import annotations

import socket
import threading
import time

import pytest

from btc.fetch import Deadline, FetchError, Fetcher, SourceTask, collect_parallel


class SlowServer:
    """Local HTTP server: 'fixed' (Content-Length 24, 1 byte / 0.3 s), 'chunked', or 'silent'."""

    def __init__(self, mode: str):
        self.mode = mode
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.stop = threading.Event()
        self.clients = []
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f'http://127.0.0.1:{self.sock.getsockname()[1]}/slow'

    def serve(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            self.clients.append(conn)
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def handle(self, conn):
        try:
            conn.recv(4096)
            if self.mode == 'silent':
                self.stop.wait(30)
                return
            if self.mode == 'fixed':
                conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 24\r\n\r\n')
                for _ in range(24):
                    if self.stop.wait(0.3):
                        return
                    conn.sendall(b'x')
            else:
                conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n')
                for _ in range(24):
                    if self.stop.wait(0.3):
                        return
                    conn.sendall(b'1\r\nx\r\n')
                conn.sendall(b'0\r\n\r\n')
        except OSError:
            pass

    def close(self):
        self.stop.set()
        for conn in self.clients:
            try:
                conn.close()
            except OSError:
                pass
        self.sock.close()


@pytest.fixture(params=['fixed', 'chunked', 'silent'])
def slow(request):
    server = SlowServer(request.param)
    yield server
    server.close()


def test_slow_body_or_silence_stops_at_the_deadline(slow):
    fetcher = Fetcher(Deadline(2.0), retry_delay=0.1, guard=False)  # local test server
    started = time.monotonic()
    with pytest.raises(FetchError) as caught:
        fetcher.get(slow.url, timeout=1.0)
    elapsed = time.monotonic() - started
    assert caught.value.kind in ('timeout', 'budget_exhausted')
    assert str(caught.value) in ('timeout', 'budget_exhausted')  # fixed label, no exception text
    assert elapsed < 2.0 + 1.0, elapsed


def test_single_request_wall_limit_is_its_timeout(slow):
    fetcher = Fetcher(Deadline(60.0), guard=False)  # local test server
    started = time.monotonic()
    with pytest.raises(FetchError) as caught:
        fetcher.get(slow.url, timeout=1.0, retries=0)
    assert caught.value.kind == 'timeout'
    assert time.monotonic() - started < 1.0 + 1.0


def test_collect_parallel_returns_within_budget(slow):
    def task(f):
        f.get(slow.url, timeout=20)
        raise AssertionError('slow source must not succeed')

    started = time.monotonic()
    out = collect_parallel([SourceTask(f's{i}', task) for i in range(3)], budget_seconds=2.0, max_workers=3,
                       fetcher_factory=lambda d: Fetcher(d, guard=False))  # local test server
    assert time.monotonic() - started < 2.0 + 3.0
    for result in out['results']:
        assert result['status'] == 'unavailable'
        assert result['error_kind'] in ('timeout', 'budget_exhausted')
