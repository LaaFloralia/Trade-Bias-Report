"""Jev headline triage for BTC news, run by the entry outside the sandbox (standard library only).

Why here: the triage script resolves its credential with 1Password ``op``,
which needs the Keychain and its own ``/tmp`` directory. The sandboxed
workers have neither (no secret ever enters the sandbox), so:

1. the sandboxed collect worker writes ``news-candidates.json`` (public
   headlines only: id, url, title, excerpt, publisher, published_at);
2. the entry (this module, loaded from the runtime snapshot by file path)
   validates that file strictly, runs the same triage script with the same
   constants, minimal environment and timeout as XAU, in a private temp
   directory, and writes ``news-triage.json`` (validated per-id scores and a
   fixed ``{mode, reason}`` record only);
3. the sandboxed news-detail worker validates ``news-triage.json`` again and
   selects headlines with the same constants (``btc.news.select``).

Constants equal ``scrapers/news_triage.py`` (P4: same triage type and the
same shared daily budget; tests assert the equality). ``LAA_JEV_DISABLED=1``,
a missing script, failure, timeout or any validation error fall back to the
keyword rule in the worker; the reason is a fixed label.

Never imports third-party code or ``btc.*``: the entry must stay stdlib-only.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
from urllib.parse import urlsplit

TRIAGE = '/Users/laa/.agents/skills/typesafe-ai/scripts/run.sh'
THRESHOLD = 0.5
KEEP_MIN, KEEP_MAX = 5, 25
BUDGET_USD = '0.20'
TIMEOUT_SECONDS = 25
QUESTION = ('Does this headline specifically report news that can move the Bitcoin (BTC/USD) price: Bitcoin itself, '
            'spot Bitcoin ETFs or their flows, crypto regulation or enforcement, exchange incidents or hacks, '
            'stablecoins, large corporate or government Bitcoin purchases or sales, Federal Reserve policy, '
            'US inflation or jobs data, or the US dollar and interest rates?')

CANDIDATES_NAME = 'news-candidates.json'
TRIAGE_NAME = 'news-triage.json'
VERSION = 1
MAX_CANDIDATES = 200
MAX_CANDIDATES_BYTES = 512 * 1024
MAX_TRIAGE_BYTES = 64 * 1024
FIELDS = {'id': 4, 'url': 500, 'title': 300, 'excerpt': 300, 'publisher': 60, 'published_at': 27}
ID = re.compile(r'n\d{3}')
PUBLISHED = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z')
CONTROL = re.compile(r'[\x00-\x1f\x7f-\x9f  ]')
HOST = re.compile(r'[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+')
REASON = re.compile(r'[a-z_]{1,48}')
MODEL = re.compile(r'[A-Za-z0-9._:/-]{1,60}')
MODES = ('jev', 'fallback', 'skipped')


class TriageInputError(ValueError):
    """Fixed-label validation failure (the message is a label, never file content)."""


# ------------------------------------------------------------ candidates file

def clean_text(value, limit: int) -> str:
    """Worker side: collapse whitespace, drop control characters, cap the length."""
    return CONTROL.sub(' ', ' '.join(str(value or '').split()))[:limit].strip()


def candidates_document(candidates: list[dict]) -> dict:
    """Worker side: the only headline fields that leave the sandbox for the entry."""
    rows = []
    for item in candidates[:MAX_CANDIDATES]:
        row = {'id': str(item.get('id')), 'url': str(item.get('url')),
               'title': clean_text(item.get('title'), FIELDS['title']),
               'excerpt': clean_text(item.get('excerpt'), FIELDS['excerpt']),
               'publisher': clean_text(item.get('publisher'), FIELDS['publisher']),
               'published_at': str(item.get('published_at'))}
        # Rows the entry would reject (non-https or over-long URL, odd time) are not sent; they keep no score.
        if ID.fullmatch(row['id']) and len(row['url']) <= FIELDS['url'] and _https_url(row['url']) \
                and not CONTROL.search(row['url']) and PUBLISHED.fullmatch(row['published_at']) and row['title']:
            rows.append(row)
    return {'version': VERSION, 'candidates': rows}


def _https_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    host = parts.hostname or ''
    return (parts.scheme == 'https' and port is None and parts.username is None and parts.password is None
            and '@' not in parts.netloc and parts.netloc.lower() == host and bool(HOST.fullmatch(host)))


def validate_candidates(data) -> list[dict]:
    """Entry side: exact shape, bounded count and lengths, https URLs, unique n### ids."""
    if not isinstance(data, dict) or set(data) != {'version', 'candidates'} or data['version'] != VERSION:
        raise TriageInputError('candidates_shape')
    rows = data['candidates']
    if not isinstance(rows, list) or len(rows) > MAX_CANDIDATES:
        raise TriageInputError('candidates_count')
    seen, out = set(), []
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(FIELDS):
            raise TriageInputError('candidate_fields')
        for key, limit in FIELDS.items():
            value = row[key]
            if not isinstance(value, str) or len(value) > limit or CONTROL.search(value):
                raise TriageInputError('candidate_value')
        if not ID.fullmatch(row['id']) or row['id'] in seen:
            raise TriageInputError('candidate_id')
        if not PUBLISHED.fullmatch(row['published_at']) or not _https_url(row['url']) or not row['title']:
            raise TriageInputError('candidate_value')
        seen.add(row['id'])
        out.append({key: row[key] for key in FIELDS})
    return out


# ------------------------------------------------------ bounded file handling

def read_bounded(directory: Path, name: str, limit: int) -> bytes:
    """Read a regular file from a directory without following symlinks or blocking on FIFOs."""
    dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise TriageInputError('file_not_regular_or_too_large')
        data = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise TriageInputError('file_too_large')
    return data


def write_exclusive(directory: Path, name: str, data: bytes) -> None:
    """Create a new private file in a directory (no symlink following; a stale entry is removed first)."""
    dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            os.unlink(name, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)


def load_candidates(directory: Path) -> list[dict]:
    try:
        data = json.loads(read_bounded(directory, CANDIDATES_NAME, MAX_CANDIDATES_BYTES).decode('utf-8'))
    except (TriageInputError, FileNotFoundError):
        raise
    except (OSError, UnicodeDecodeError, ValueError):
        raise TriageInputError('candidates_unreadable') from None
    return validate_candidates(data)


# ------------------------------------------------------------------- triage

def payload(candidates: list[dict]) -> dict:
    return {'candidates': [{'id': c['id'], 'url': c['url'], 'title': c['title'], 'text': c['excerpt'],
                            'author': c['publisher'], 'date': c['published_at'], 'kind': 'news_headline'}
                           for c in candidates]}


def triage_command(source: Path, out_dir: Path, count: int) -> list[str]:
    return ['/bin/bash', TRIAGE, 'triage', '--data-class', 'public', '--question', QUESTION,
            '--input', str(source), '--out-dir', str(out_dir), '--threshold', str(THRESHOLD), '--keep-min', '0',
            '--keep-max', str(count), '--budget-usd', BUDGET_USD, '--timeout', '10']


def run_triage(candidates: list[dict], *, runner=None, environ=None, timeout: int = TIMEOUT_SECONDS):
    """Probability per id, or (None, record) -> keyword rule in the worker. Public headlines only."""
    environ = os.environ if environ is None else environ
    if environ.get('LAA_JEV_DISABLED') == '1':
        return None, {'mode': 'fallback', 'reason': 'disabled'}
    if not candidates:
        return {}, {'mode': 'skipped', 'reason': 'no_candidates'}
    if not Path(TRIAGE).is_file():
        return None, {'mode': 'fallback', 'reason': 'triage_unavailable'}
    run = runner or subprocess.run
    started = time.monotonic()
    try:
        # Private temp directory of the entry (never the sandbox's TMPDIR).
        with tempfile.TemporaryDirectory(prefix='btcusd-triage-') as tmp:
            source = Path(tmp) / 'candidates.json'
            source.write_text(json.dumps(payload(candidates), ensure_ascii=False))
            proc = run(triage_command(source, Path(tmp), len(candidates)), capture_output=True, timeout=timeout,
                       check=False, stdin=subprocess.DEVNULL,
                       env={'HOME': str(Path.home()), 'USER': environ.get('USER', 'laa'),
                            'PATH': '/opt/homebrew/bin:/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1',
                            'TMPDIR': tmp})
            if proc.returncode:
                return None, {'mode': 'fallback', 'reason': 'triage_failed'}
            data = json.loads((Path(tmp) / 'triage.json').read_text())
    except subprocess.TimeoutExpired:
        return None, {'mode': 'fallback', 'reason': 'timeout'}
    except (OSError, ValueError, TypeError, KeyError):
        return None, {'mode': 'fallback', 'reason': 'triage_unreadable'}
    report = data.get('report') if isinstance(data, dict) else None
    if not isinstance(report, dict) or report.get('mode') != 'jev':
        reason = str((report or {}).get('fallback') or 'jev_unavailable')
        return None, {'mode': 'fallback', 'reason': reason if REASON.fullmatch(reason) else 'jev_unavailable'}
    ids = {c['id'] for c in candidates}
    scores = {}
    for row in data.get('candidates') or []:
        if not isinstance(row, dict):
            continue
        p, ident = row.get('p'), row.get('id')
        if ident in ids and isinstance(p, (int, float)) and not isinstance(p, bool) and 0 <= p <= 1:
            scores[ident] = float(p)
    model = str(report.get('model'))
    tokens = report.get('input_tokens')
    return scores, {'mode': 'jev', 'model': model if MODEL.fullmatch(model) else 'unrecognised',
                    'input_tokens': tokens if isinstance(tokens, int) and not isinstance(tokens, bool)
                    and 0 <= tokens < 10 ** 9 else None,
                    'elapsed_ms': round((time.monotonic() - started) * 1000), 'candidates_sent': len(candidates)}


# -------------------------------------------------------------- triage file

def triage_document(scores: dict | None, selection: dict) -> dict:
    return {'version': VERSION, 'selection': selection, 'scores': scores}


def validate_triage(data, ids: set[str]) -> tuple[dict | None, dict]:
    """Worker side: accept only the fixed record and in-range scores for known ids."""
    fallback = None, {'mode': 'fallback', 'reason': 'triage_invalid'}
    if not isinstance(data, dict) or set(data) != {'version', 'selection', 'scores'} or data['version'] != VERSION:
        return fallback
    selection, scores = data['selection'], data['scores']
    if not isinstance(selection, dict) or selection.get('mode') not in MODES:
        return fallback
    if selection['mode'] == 'jev':
        allowed = {'mode', 'model', 'input_tokens', 'elapsed_ms', 'candidates_sent'}
        tokens, elapsed, sent = (selection.get(k) for k in ('input_tokens', 'elapsed_ms', 'candidates_sent'))
        if set(selection) != allowed or not isinstance(selection['model'], str) \
                or not MODEL.fullmatch(selection['model']) \
                or not (tokens is None or (type(tokens) is int and 0 <= tokens < 10 ** 9)) \
                or type(elapsed) is not int or not 0 <= elapsed < 10 ** 7 \
                or type(sent) is not int or not 0 <= sent <= MAX_CANDIDATES:
            return fallback
    elif set(selection) != {'mode', 'reason'} or not isinstance(selection['reason'], str) \
            or not REASON.fullmatch(selection['reason']):
        return fallback
    if scores is None:
        return (None, dict(selection)) if selection['mode'] == 'fallback' else fallback
    if not isinstance(scores, dict) or selection['mode'] == 'fallback':
        return fallback
    clean = {}
    for key, value in scores.items():
        if key not in ids or isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            return fallback
        clean[key] = float(value)
    return clean, dict(selection)


def load_triage(directory: Path, ids: set[str]) -> tuple[dict | None, dict]:
    try:
        data = json.loads(read_bounded(directory, TRIAGE_NAME, MAX_TRIAGE_BYTES).decode('utf-8'))
    except FileNotFoundError:
        return None, {'mode': 'fallback', 'reason': 'triage_missing'}
    except (OSError, UnicodeDecodeError, ValueError):
        return None, {'mode': 'fallback', 'reason': 'triage_invalid'}
    return validate_triage(data, ids)


def entry_triage(directory: Path, *, runner=None, environ=None) -> dict:
    """Entry side, never raises: validate candidates, triage, write news-triage.json. Returns the record."""
    try:
        candidates = load_candidates(directory)
    except FileNotFoundError:
        scores, selection = None, {'mode': 'fallback', 'reason': 'candidates_missing'}
    except (TriageInputError, OSError):
        scores, selection = None, {'mode': 'fallback', 'reason': 'candidates_invalid'}
    else:
        scores, selection = run_triage(candidates, runner=runner, environ=environ)
    data = json.dumps(triage_document(scores, selection), ensure_ascii=False).encode()
    try:
        write_exclusive(directory, TRIAGE_NAME, data)
    except OSError:
        return {'mode': 'fallback', 'reason': 'triage_write_failed'}
    return dict(selection)
