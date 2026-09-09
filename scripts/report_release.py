"""Verified, reversible report release. Default is a network-free dry run.

Write a version first, verify it, replace latest, then verify Storage + HP bytes.
The final metadata is the publication marker; failed attempts are never success.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request
import uuid

BUCKET = 'bias-reports'
SITE_BASE = 'https://www.laa-inc.com/reports'


class ReleaseError(RuntimeError):
    pass


def sha(body):
    return hashlib.sha256(body).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    os.replace(temp, path)


def mode_path(kind, filename='latest.html'):
    if kind not in {'daily', 'weekly'}:
        raise ReleaseError('Invalid report kind')
    return f'{kind}/{filename}'


@contextmanager
def release_lock(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'publish.lock').open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReleaseError('Another publication is active') from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class Storage:
    def __init__(self):
        self.base = (os.getenv('SUPABASE_URL') or '').rstrip('/')
        self.key = os.getenv('SUPABASE_SERVICE_ROLE_KEY') or ''
        if not self.base or not self.key:
            raise ReleaseError('SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY is not configured')
        if not re.fullmatch(r'https://[a-z0-9-]+\.supabase\.co', self.base):
            raise ReleaseError('Unexpected Storage origin')

    def _request(self, url, method='GET', body=None, mime=None, authenticated=False, upsert=True):
        headers = {'Cache-Control': 'no-cache, max-age=0'}
        if mime:
            headers['Content-Type'] = mime
        if authenticated:
            headers.update({'Authorization': f'Bearer {self.key}', 'apikey': self.key})
        if method == 'POST':
            headers['x-upsert'] = 'true' if upsert else 'false'
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        # Refuse redirects on credential-bearing requests.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=45) as response:
                return response.read(), response.headers.get('Content-Type', '').split(';')[0].lower()
        except urllib.error.HTTPError as error:
            if method == 'GET' and error.code == 404:
                return None
            raise ReleaseError(f'HTTP {error.code} during {method}; response suppressed') from None
        except Exception as error:
            raise ReleaseError(f'Network failure during {method}: {type(error).__name__}; details suppressed') from None

    def get(self, path):
        return self._request(f'{self.base}/storage/v1/object/public/{BUCKET}/{path}')

    def put(self, path, body, mime, upsert=True):
        self._request(f'{self.base}/storage/v1/object/{BUCKET}/{path}', 'POST', body, mime, True, upsert)

    def delete(self, path):
        self._request(f'{self.base}/storage/v1/object/{BUCKET}', 'DELETE',
                      json.dumps({'prefixes': [path]}).encode(), 'application/json', True)

    def reader(self, kind):
        return self._request(f'{SITE_BASE}/{kind}')


def verify(response, expected, label, html=False, site=False):
    if response is None:
        raise ReleaseError(f'{label}: object is missing')
    body, mime = response
    allowed = {'text/html'} if site else ({'text/html', 'text/plain'} if html else {'application/json'})
    if mime not in allowed:
        raise ReleaseError(f'{label}: unexpected MIME')
    if sha(body) != sha(expected):
        raise ReleaseError(f'{label}: readback hash mismatch')


def publication_inputs(html_path, bundle_path, acceptance_path, now=None):
    from scripts.report_acceptance import validate_acceptance
    result = validate_acceptance(html_path, bundle_path, acceptance_path, now=now)
    bundle = json.loads(Path(bundle_path).read_text())
    body = Path(html_path).read_bytes()
    return bundle, body, result


def _release(html_path, bundle_path, acceptance_path, directory, *, publish=False, transport=None, now=None):
    """Return a receipt; exceptions carry fixed labels only, never remote response text."""
    current = now or datetime.now().astimezone()
    directory = Path(directory)
    with release_lock(directory):
        bundle, body, acceptance = publication_inputs(html_path, bundle_path, acceptance_path, current)
        if publish and bundle.get('synthetic'):
            raise ReleaseError('Synthetic reports cannot be published')
        kind = bundle['kind']
        attempt = f'{current.strftime("%Y%m%dT%H%M%S")}-{uuid.uuid4().hex[:8]}'
        version = mode_path(kind, f'versions/{bundle["reportDate"]}/{attempt}-{sha(body)[:12]}.html')
        metadata = {'schemaVersion': 1, 'kind': kind, 'asOf': bundle['asOf'],
                    'reportDate': bundle['reportDate'], 'publishedAt': current.isoformat(),
                    'url': f'{SITE_BASE}/{kind}', 'sha256': sha(body), 'versionPath': version}
        metadata_bytes = (json.dumps(metadata, ensure_ascii=False, indent=2) + '\n').encode()
        receipt = {'attemptId': attempt, 'kind': kind, 'generation': 'succeeded',
                   'publication': 'not_attempted', 'dryRun': not publish, 'metadata': metadata,
                   'acceptanceSha256': acceptance['acceptanceSha256'], 'rollback': 'not_needed'}
        if not publish:
            receipt.update(publication='dry_run_validated', writes=[version, mode_path(kind), mode_path(kind, 'latest.json')])
            write_json(directory / f'{attempt}.dry-run.json', receipt)
            return receipt
        store = transport if transport is not None else Storage()
        active = directory / 'active-publication.json'
        if active.exists():
            previous = json.loads(active.read_text())
            if previous.get('publication') in {'publishing', 'rollback_failed'}:
                raise ReleaseError('Unresolved publication journal; recover before a new release')
        attempt_dir = directory / attempt
        attempt_dir.mkdir()
        html_key, meta_key = mode_path(kind), mode_path(kind, 'latest.json')
        previous_html, previous_meta = store.get(html_key), store.get(meta_key)
        known_path = directory / f'last-published-{kind}.json'
        if known_path.exists():
            known = json.loads(known_path.read_text())
            if previous_html is None or sha(previous_html[0]) != known['metadata']['sha256']:
                raise ReleaseError('Current Storage edition differs from the last verified publication')
            verify(store.reader(kind), previous_html[0], 'previous HP edition', html=True, site=True)
        if previous_meta is not None:
            try:
                known_meta = json.loads(previous_meta[0])
            except ValueError:
                raise ReleaseError('Existing publication metadata is invalid') from None
            if 'sha256' in known_meta and (previous_html is None or known_meta['sha256'] != sha(previous_html[0])):
                raise ReleaseError('Existing metadata and HTML disagree')
        # Snapshot every prior public byte locally before the first write.
        for name, response in [('previous.html', previous_html), ('previous.json', previous_meta)]:
            if response is not None:
                (attempt_dir / name).write_bytes(response[0])
        receipt.update(publication='publishing', phase='version', backupDirectory=str(attempt_dir),
                       hadPreviousHtml=previous_html is not None, hadPreviousMetadata=previous_meta is not None)
        write_json(active, receipt)
        touched = False
        try:
            store.put(version, body, 'text/html; charset=utf-8', upsert=False)
            verify(store.get(version), body, 'version', html=True)
            receipt['phase'] = 'latest'
            write_json(active, receipt)
            # Mark as touched before PUT: a timeout can occur after a server-side write.
            touched = True
            store.put(html_key, body, 'text/html; charset=utf-8')
            verify(store.get(html_key), body, 'Storage latest', html=True)
            verify(store.reader(kind), body, 'HP latest', html=True, site=True)
            receipt['phase'] = 'metadata'
            write_json(active, receipt)
            store.put(meta_key, metadata_bytes, 'application/json; charset=utf-8')
            verify(store.get(meta_key), metadata_bytes, 'metadata')
            # Verify HTML again after metadata to catch a concurrent writer.
            verify(store.get(html_key), body, 'Storage final', html=True)
            verify(store.reader(kind), body, 'HP final', html=True, site=True)
            receipt.update(publication='succeeded', phase='complete', storageVerified=True, hpVerified=True)
            write_json(directory / f'last-published-{kind}.json', receipt)
        except Exception as error:
            receipt.update(publication='failed', error=f'{type(error).__name__}: {error}' if isinstance(error, ReleaseError) else type(error).__name__)
            if touched:
                try:
                    for key, previous, is_html in [(html_key, previous_html, True), (meta_key, previous_meta, False)]:
                        if previous is None:
                            store.delete(key)
                            if store.get(key) is not None:
                                raise ReleaseError('Rollback deletion did not persist')
                        else:
                            store.put(key, previous[0], 'text/html; charset=utf-8' if is_html else 'application/json; charset=utf-8')
                            verify(store.get(key), previous[0], 'rollback', html=is_html)
                    if previous_html:
                        verify(store.reader(kind), previous_html[0], 'HP rollback', html=True, site=True)
                    receipt['rollback'] = 'verified'
                except Exception:
                    receipt.update(publication='rollback_failed', rollback='unverified')
            write_json(active, receipt)
            write_json(attempt_dir / 'receipt.json', receipt)
            raise ReleaseError(f'Publication failed; rollback={receipt["rollback"]}; see local receipt') from None
        write_json(active, receipt)
        write_json(attempt_dir / 'receipt.json', receipt)
        return receipt


def release(html_path, bundle_path, acceptance_path, directory, *, publish=False, transport=None, now=None):
    try:
        return _release(html_path, bundle_path, acceptance_path, directory, publish=publish, transport=transport, now=now)
    except Exception as error:
        write_json(Path(directory) / f'failed-{uuid.uuid4().hex}.json', {
            'at': datetime.now().astimezone().isoformat(), 'generation': 'not_revalidated',
            'publication': 'failed' if publish else 'dry_run_rejected', 'errorType': type(error).__name__})
        raise


def recover(directory, *, transport=None):
    """Resume rollback from a crash journal; caller must have publication approval."""
    directory = Path(directory).absolute()
    with release_lock(directory):
        active = directory / 'active-publication.json'
        receipt = json.loads(active.read_text())
        if receipt.get('publication') not in {'publishing', 'rollback_failed'}:
            raise ReleaseError('No unresolved publication to recover')
        kind = receipt['kind']
        backup = Path(receipt['backupDirectory']).absolute()
        if not backup.is_relative_to(directory) or backup.resolve().parent != directory.resolve():
            raise ReleaseError('Invalid rollback backup directory')
        store = transport if transport is not None else Storage()
        try:
            for filename, present, mime, is_html in [
                ('previous.html', receipt['hadPreviousHtml'], 'text/html; charset=utf-8', True),
                ('previous.json', receipt['hadPreviousMetadata'], 'application/json; charset=utf-8', False)]:
                key = mode_path(kind, 'latest.html' if is_html else 'latest.json')
                if present:
                    body = (backup / filename).read_bytes()
                    store.put(key, body, mime)
                    verify(store.get(key), body, 'recovery', html=is_html)
                    if is_html:
                        verify(store.reader(kind), body, 'HP recovery', html=True, site=True)
                else:
                    store.delete(key)
                    if store.get(key) is not None:
                        raise ReleaseError('Recovery deletion did not persist')
            receipt.update(publication='failed', rollback='verified', phase='recovered')
        except Exception:
            receipt.update(publication='rollback_failed', rollback='unverified')
            write_json(active, receipt)
            raise ReleaseError('Recovery failed; old publication is not verified') from None
        write_json(active, receipt)
        write_json(backup / 'recovery.json', receipt)
        return receipt
