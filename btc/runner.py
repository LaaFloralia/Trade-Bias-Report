#!/usr/bin/env python3
"""BTCUSD chart-intel entry point and sandboxed worker.

Installed copy: ``<root>/runner.py`` (by btc/install_job.py), called by
``<root>/run.sh daily|weekly [--dry-run|--parent-package <abs>|--parent-review <abs>]``.

Entry (not sandboxed, standard library only):
  log entry -> ``<root>/run.lock`` -> snapshot of the committed source HEAD
  (allow-list only) into ``<root>/runtime/`` -> worker under ``sandbox-exec``.
Worker (``<root>/runtime/btc/runner.py --worker``): imports ``btc.*`` from the
snapshot and runs one workflow phase.

Collection (prepare phase), all steps started by the entry:
1. ``op-run-batch.sh <runtime>/btc/secrets.env.tpl /usr/bin/env -u
   OP_SERVICE_ACCOUNT_TOKEN sandbox-exec -f boundary.sb -D TMPDIR=<run tmp>
   <python> -B <worker> --collect-worker ...``: ``op`` needs the Keychain, so
   it runs outside the sandbox; its service-account token is removed before
   ``sandbox-exec``, so only FRED_API_KEY reaches the sandboxed collect worker
   (context sources and news headlines; writes ``context.json`` and
   ``news-candidates.json``).
2. Jev headline triage in the entry itself (``runtime/btc/headline_triage.py``,
   standard library only): strict validation of ``news-candidates.json``, the
   XAU triage script in a private temp directory, ``news-triage.json`` out.
3. ``sandbox-exec ... --finish-worker`` without any wrapper or secret: news
   detail, quotes and books; writes ``collection.json``.
Worker output streams are discarded and only fixed labels are passed on
(``--collect-status``) to the sandboxed prepare worker, which records the
state and builds the facts without any secret. Every worker refuses to start
(exit 78, ``credential_env_present``) when any ``OP_*`` variable is present.

Configuration (installed run.sh sets these): BTC_JOB_ROOT, BTC_SOURCE_REPO,
BTC_PYTHON. Development may point them at a temporary root and a worktree;
the snapshot still uses only the committed HEAD.
"""
from __future__ import annotations

import sys
from pathlib import Path

if __name__ == '__main__':
    # Running runtime/btc/runner.py puts runtime/btc first on sys.path; module
    # names there must never shadow the standard library or third-party code.
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [p for p in sys.path if p not in ('', _here)]

import argparse
import os
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import importlib.util
import json
import shutil
import subprocess
import tempfile
import time
import uuid
from zoneinfo import ZoneInfo

JST = ZoneInfo('Asia/Tokyo')
PRODUCTION_ROOT = Path('/Users/laa/.codex/jobs/chart-intel-btcusd')
DEFAULT_SOURCE = Path('/Users/laa/dev/fundamental-macro-analysis')
DEFAULT_PYTHON = DEFAULT_SOURCE / '.venv/bin/python'
# Tests replace the wrapper with a fake (never resolves secrets); production uses op-run-batch.sh.
SECRETS_WRAPPER = Path(os.environ.get('BTC_SECRETS_WRAPPER') or '/Users/laa/.config/laa/op-run-batch.sh')
SANDBOX = Path('/usr/bin/sandbox-exec')
# Existing modules imported read-only: btc.machine -> scripts.intel, btc.news -> scrapers.news_triage.
SNAPSHOT_EXTRA = ('scripts/intel.py', 'scrapers/news_triage.py')
SNAPSHOT_SUFFIXES = ('.py', '.json', '.md')
STRIPPED_ENV = ('PYTHONPATH', 'PYTHONHOME', 'FRED_API_KEY', 'TWELVEDATA_API_KEY', 'OP_SERVICE_ACCOUNT_TOKEN',
                'SUPABASE_SERVICE_ROLE_KEY', 'SUPABASE_URL', 'MYFXBOOK_EMAIL', 'MYFXBOOK_PASSWORD',
                # Production always uses Playwright's bundled Chromium: an installed Chrome (dev channel)
                # would need its per-user singleton socket, which lies outside the sandbox boundary.
                'BTC_PLAYWRIGHT_CHANNEL', 'MAC_CHROMIUM_TMPDIR')
WORKER_TIMEOUT = 45 * 60
COLLECT_TIMEOUT = 15 * 60  # collect worker + entry triage + news-detail worker together
GUARD_EXIT = 78
CONTEXT_NAME = 'context.json'


class Paths:
    def __init__(self, root=None, source=None, python=None):
        self.root = Path(root or os.environ.get('BTC_JOB_ROOT') or PRODUCTION_ROOT).absolute()
        self.source = Path(source or os.environ.get('BTC_SOURCE_REPO') or DEFAULT_SOURCE).absolute()
        self.python = Path(python or os.environ.get('BTC_PYTHON') or DEFAULT_PYTHON)
        self.runtime = self.root / 'runtime'
        self.policy = self.root / 'boundary.sb'
        self.lock = self.root / 'run.lock'
        self.logs = self.root / 'logs'
        self.manifest = self.root / 'snapshot.json'
        self.worker = self.runtime / 'btc' / 'runner.py'
        self.secrets_template = self.runtime / 'btc' / 'secrets.env.tpl'


# --------------------------------------------------------------------- entry

def snapshot_allowed(name: str) -> bool:
    path = Path(name)
    if any(part.startswith('.') for part in path.parts) or '__pycache__' in path.parts:
        return False
    if name in SNAPSHOT_EXTRA:
        return True
    if path.parts[0] != 'btc' or 'job_template' in path.parts:
        return False
    return path.suffix in SNAPSHOT_SUFFIXES or path.name == 'secrets.env.tpl'


def runtime_git() -> str:
    for candidate in ('git', '/Library/Developer/CommandLineTools/usr/bin/git',
                      '/Users/laa/Applications/Sourcetree.app/Contents/Resources/git_local/bin/git'):
        try:
            if subprocess.run([candidate, '--version'], capture_output=True, timeout=10).returncode == 0:
                return candidate
        except (OSError, subprocess.TimeoutExpired):
            pass
    raise RuntimeError('No working installed Git for the read-only runtime snapshot')


def committed_revision(paths: Paths, git: str | None = None) -> str:
    git = git or runtime_git()
    return subprocess.check_output([git, '-C', str(paths.source), 'rev-parse', 'HEAD'], text=True,
                                   stderr=subprocess.DEVNULL).strip()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _runtime_matches(paths: Paths, manifest: dict) -> bool:
    files = {str(p.relative_to(paths.runtime)) for p in paths.runtime.rglob('*')
             if p.is_file() and '__pycache__' not in p.parts} if paths.runtime.is_dir() else set()
    expected = manifest.get('sha256', {})
    return files == set(expected) and all(
        _sha((paths.runtime / name).read_bytes()) == checksum for name, checksum in expected.items())


def prepare_snapshot(paths: Paths) -> dict:
    """Copy allow-listed committed files only; never uncommitted edits, dotfiles, tests or docs."""
    git = runtime_git()
    revision = committed_revision(paths, git)
    if paths.manifest.is_file():
        previous = json.loads(paths.manifest.read_text())
        if previous.get('revision') == revision and previous.get('source') == str(paths.source) \
                and _runtime_matches(paths, previous):
            return previous
    listing = subprocess.check_output([git, '-C', str(paths.source), 'ls-tree', '-r', '-z', revision])
    staging = paths.root / f'.runtime-{uuid.uuid4().hex[:8]}'
    staging.mkdir(mode=0o700)
    copied = {}
    try:
        for entry in filter(None, listing.decode().split('\0')):
            info, name = entry.split('\t', 1)
            mode, kind, blob = info.split()
            if kind != 'blob' or mode not in {'100644', '100755'} or not snapshot_allowed(name):
                continue
            data = subprocess.check_output([git, '-C', str(paths.source), 'cat-file', 'blob', blob])
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.write_bytes(data)
            target.chmod(0o600)
            copied[name] = _sha(data)
        for required in ('btc/runner.py', 'btc/workflow.py', 'btc/secrets.env.tpl'):
            if required not in copied:
                raise RuntimeError('Committed source lacks a required BTC runtime file')
        old = paths.root / f'.runtime-old-{uuid.uuid4().hex[:8]}'
        if paths.runtime.exists():
            paths.runtime.rename(old)
        staging.rename(paths.runtime)
        shutil.rmtree(old, ignore_errors=True)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    manifest = {'revision': revision, 'source': str(paths.source), 'created_at': datetime.now(JST).isoformat(),
                'sha256': copied}
    tmp = paths.manifest.with_suffix('.tmp')
    tmp.write_text(json.dumps(manifest, indent=2) + '\n')
    tmp.chmod(0o600)
    os.replace(tmp, paths.manifest)
    return manifest


def isolated_environment(paths: Paths, tmpdir: Path | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV and not k.startswith('OP_')}
    if tmpdir is not None:
        env['TMPDIR'] = str(tmpdir) + '/'
        env['MAC_CHROMIUM_TMPDIR'] = str(tmpdir)  # macOS Chromium temp (instead of DARWIN_USER_TEMP_DIR)
    env.update(TZ='Asia/Tokyo', PYTHONDONTWRITEBYTECODE='1', PYTHON_DOTENV_DISABLED='1',
               BTC_JOB_ROOT=str(paths.root), BTC_SOURCE_REPO=str(paths.source), BTC_PYTHON=str(paths.python),
               BRAIN_PATH=str(paths.root / 'disabled-brain'))
    return env


@contextmanager
def execution_lock(paths: Paths):
    paths.root.mkdir(parents=True, exist_ok=True)
    with paths.lock.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another BTCUSD chart-intel run is already active') from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


COLLECT_STATUSES = ('ok', 'credentials_unavailable', 'collection_worker_failed', 'collection_timeout',
                    'credential_env_present')


def credential_env_present(environ=None) -> bool:
    """Worker guard: no 1Password variable (OP_*) may ever reach a sandboxed worker (names only)."""
    return any(name.startswith('OP_') for name in (os.environ if environ is None else environ))


def sandbox_prefix(paths: Paths, tmpdir: Path | str = '<run tmp>') -> list[str]:
    return [str(SANDBOX), '-f', str(paths.policy), '-D', f'TMPDIR={tmpdir}']


def worker_command(paths: Paths, args, tmpdir: Path | str = '<run tmp>') -> list[str]:
    command = sandbox_prefix(paths, tmpdir) + [str(paths.python), '-B', str(paths.worker),
                                                args.mode, '--worker', '--root', str(paths.root)]
    for option, path in (('--parent-package', args.parent_package), ('--parent-review', args.parent_review)):
        if path is not None:
            command.extend([option, str(Path(path).absolute())])
    return command


def append_entry(paths: Paths, entry: dict) -> None:
    paths.logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(paths.logs / 'entries.jsonl', os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, 'a') as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + '\n')


def dry_run_plan(paths: Paths, args) -> dict:
    try:
        revision = committed_revision(paths)
    except (RuntimeError, OSError, subprocess.CalledProcessError):
        revision = None
    phase = 'parent_package' if args.parent_package else 'parent_review' if args.parent_review else 'prepare'
    return {'dry_run': True, 'symbol': 'BTCUSD', 'mode': args.mode, 'phase': phase, 'root': str(paths.root),
            'source_repo': str(paths.source), 'source_revision': revision, 'python': str(paths.python),
            'lock': str(paths.lock), 'sandbox_policy': str(paths.policy), 'runtime': str(paths.runtime),
            'command': worker_command(paths, args),
            'collection': {'wrapper': str(SECRETS_WRAPPER), 'template': str(paths.secrets_template),
                           'secrets': ['FRED_API_KEY'],
                           'credential_boundary': 'op outside the sandbox; token removed before sandbox-exec',
                           'command': collect_command(paths, args.mode, Path('<work>/.collect-<id>/collection.json'),
                                                      '<collection start>'),
                           'news_triage': 'entry (outside the sandbox): runtime/btc/headline_triage.py',
                           'finish_command': finish_command(paths, args.mode,
                                                            Path('<work>/.collect-<id>/collection.json'),
                                                            '<collection start>')} if phase == 'prepare' else None,
            'state': str(paths.root / f'latest-{args.mode}.json'),
            'reports': str(paths.root / 'reports' / args.mode / 'editions'),
            'execution': 'parent_only', 'delivery': 'disabled', 'publication': 'disabled',
            'writes': 'none (dry run)'}


def run_entry(paths: Paths, args) -> int:
    entry = {'entryId': uuid.uuid4().hex, 'symbol': 'BTCUSD', 'mode': args.mode,
             'started_at': datetime.now(JST).isoformat(), 'status': 'started', 'stage': 'entry'}
    append_entry(paths, entry)
    try:
        with execution_lock(paths):
            entry['stage'] = 'snapshot'
            manifest = prepare_snapshot(paths)
            entry['revision'] = manifest['revision']
            entry['entry_matches_snapshot'] = (
                Path(__file__).resolve() == paths.worker.resolve()
                or _sha(Path(__file__).read_bytes()) == manifest['sha256'].get('btc/runner.py'))
            for path in (paths.python, paths.policy, SANDBOX, paths.worker):
                if not path.is_file():
                    raise RuntimeError('Required runtime file missing')
            (paths.root / 'work').mkdir(exist_ok=True, mode=0o700)
            tmpdir = Path(os.path.realpath(tempfile.mkdtemp(prefix='btcusd-run-')))
            staging = None
            try:
                extra = []
                if not (args.parent_package or args.parent_review):
                    entry['stage'] = 'collect'
                    started = datetime.now(JST)
                    staging = paths.root / 'work' / f'.collect-{uuid.uuid4().hex[:8]}'
                    staging.mkdir(mode=0o700)
                    label = collect_outside_sandbox(paths, args.mode, staging / 'collection.json', started, tmpdir,
                                                    entry)
                    entry['collect_status'] = label
                    extra = ['--collected', str(staging / 'collection.json'), '--collect-status', label,
                             '--session-started', started.isoformat()]
                entry['stage'] = 'worker'
                try:
                    code = subprocess.run(worker_command(paths, args, tmpdir) + extra, cwd=paths.runtime,
                                          env=isolated_environment(paths, tmpdir), timeout=WORKER_TIMEOUT).returncode
                except subprocess.TimeoutExpired:
                    code = 124
                if code == GUARD_EXIT:
                    entry['error_category'] = 'credential_env_present'
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
                if staging is not None:
                    shutil.rmtree(staging, ignore_errors=True)
            entry.update(status='succeeded' if code == 0 else 'failed', exitCode=code)
            return code
    except Exception as error:  # noqa: BLE001 - type name only
        entry.update(status='failed', errorType=type(error).__name__)
        print(json.dumps({'status': 'failed', 'stage': entry['stage'], 'error': type(error).__name__}))
        return 1
    finally:
        entry['finished_at'] = datetime.now(JST).isoformat()
        append_entry(paths, entry)


# -------------------------------------------------------------------- worker

def _job(paths: Paths):
    from btc.workflow import Job
    return Job(root=paths.root, python=paths.python, secrets_wrapper=SECRETS_WRAPPER)


def collect_command(paths: Paths, mode: str, output: Path, started: str, tmpdir: Path | str = '<run tmp>') -> list[str]:
    # op run passes its own OP_SERVICE_ACCOUNT_TOKEN on to its child: drop it before sandbox-exec.
    return [str(SECRETS_WRAPPER), str(paths.secrets_template), '/usr/bin/env', '-u', 'OP_SERVICE_ACCOUNT_TOKEN'] + \
        sandbox_prefix(paths, tmpdir) + [
            str(paths.python), '-B', str(paths.worker), mode, '--collect-worker', '--root', str(paths.root),
            '--collect-output', str(output), '--session-started', started]


def finish_command(paths: Paths, mode: str, output: Path, started: str, tmpdir: Path | str = '<run tmp>') -> list[str]:
    return sandbox_prefix(paths, tmpdir) + [
        str(paths.python), '-B', str(paths.worker), mode, '--finish-worker', '--root', str(paths.root),
        '--collect-output', str(output), '--session-started', started]


def entry_triage(paths: Paths, staging: Path) -> dict:
    """Jev headline triage outside the sandbox (stdlib module from the runtime snapshot). Never raises."""
    try:
        spec = importlib.util.spec_from_file_location('btc_entry_headline_triage',
                                                      paths.runtime / 'btc' / 'headline_triage.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.entry_triage(staging)
    except Exception:  # noqa: BLE001 - fixed label; the worker then falls back to the keyword rule
        return {'mode': 'fallback', 'reason': 'triage_module_failed'}


def _sandboxed(command: list[str], paths: Paths, tmpdir: Path, timeout: float):
    return subprocess.run(command, cwd=paths.runtime, env=isolated_environment(paths, tmpdir),
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          timeout=timeout)


def collect_outside_sandbox(paths: Paths, mode: str, output: Path, started: datetime, tmpdir: Path,
                            entry: dict | None = None) -> str:
    """Entry side of collection: op wrapper -> sandbox -> collect worker; triage; news-detail worker.

    Returns a fixed label (P2, A3); the triage record (mode and reason only) goes to ``entry``.
    """
    if not SECRETS_WRAPPER.is_file() or not paths.secrets_template.is_file():
        return 'credentials_unavailable'
    marker = output.with_name('.collect-started')
    deadline = time.monotonic() + COLLECT_TIMEOUT
    try:
        proc = _sandboxed(collect_command(paths, mode, output, started.isoformat(), tmpdir), paths, tmpdir,
                          COLLECT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return 'collection_timeout'
    if proc.returncode == GUARD_EXIT:
        return 'credential_env_present'
    if not marker.exists():
        return 'credentials_unavailable'  # the wrapper never started the worker
    if proc.returncode != 0 or not output.with_name(CONTEXT_NAME).is_file():
        return 'collection_worker_failed'
    triage = entry_triage(paths, output.parent)
    if entry is not None:
        entry['news_triage'] = triage
    try:
        proc = _sandboxed(finish_command(paths, mode, output, started.isoformat(), tmpdir), paths, tmpdir,
                          max(60.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        return 'collection_timeout'
    if proc.returncode == GUARD_EXIT:
        return 'credential_env_present'
    if proc.returncode != 0 or not output.is_file():
        return 'collection_worker_failed'
    return 'ok'


def staged_collection(staged: Path, label: str):
    """Prepare-worker side: adopt the entry's staged collection or record its fixed failure label."""
    def collect(job, ctx, output: Path) -> None:
        from btc.common import JobError, contained
        messages = {'credentials_unavailable': 'Collection worker did not start (credential wrapper failed)',
                    'credential_env_present': 'A 1Password variable reached a sandboxed worker; refused',
                    'collection_timeout': 'Collection exceeded its time limit',
                    'collection_worker_failed': 'Collection worker failed'}
        if label != 'ok':
            raise JobError(label, messages.get(label, 'Collection failed'))
        source = contained(staged, job.work)
        if not source.is_file():
            raise JobError('collection_worker_failed', messages['collection_worker_failed'])
        os.replace(source, output)
        marker = source.with_name('.collect-started')
        if marker.exists():
            os.replace(marker, output.with_name('.collect-started'))
    return collect


def _collect_context(paths: Paths, args):
    from btc.common import contained, parse_time
    from btc.stages import RunContext
    from btc.workflow import session_slot
    output = contained(args.collect_output, paths.root / 'work')
    started = parse_time(args.session_started)
    ctx = RunContext(root=paths.root, mode=args.mode, session_slot=session_slot(args.mode, started),
                     started_at=started, work_dir=output.parent, history_dir=paths.root / 'history')
    return output, ctx


def collect_worker(paths: Paths, args) -> int:
    """Context phase with FRED_API_KEY: context.json and the public headline candidates for the entry."""
    from btc import headline_triage, news
    from btc.collect import news_record
    from btc.common import write_json, write_text
    from btc.stages import get_stages
    output, ctx = _collect_context(paths, args)
    write_text(output.with_name('.collect-started'), datetime.now(JST).isoformat() + '\n')
    context = get_stages().collect_context(ctx)
    write_json(output.with_name(CONTEXT_NAME), context)
    candidates = news.headline_candidates(news_record(context))
    write_json(output.with_name(headline_triage.CANDIDATES_NAME), headline_triage.candidates_document(candidates))
    return 0


def finish_worker(paths: Paths, args) -> int:
    """News detail, quotes and books (no secrets): adopts the entry's validated triage, writes collection.json."""
    from btc import headline_triage, news
    from btc.collect import news_record
    from btc.common import write_json
    from btc.stages import get_stages
    output, ctx = _collect_context(paths, args)
    context = json.loads(output.with_name(CONTEXT_NAME).read_text(encoding='utf-8'))
    ids = {c.get('id') for c in news.headline_candidates(news_record(context)) if isinstance(c, dict)}
    scores, selection = headline_triage.load_triage(output.parent, ids)
    collection = get_stages().finish_collection(context, scores, selection, ctx)
    write_json(output, collection)
    return 0


def run_worker(paths: Paths, args) -> int:
    from btc import workflow
    from btc.stages import get_stages
    from btc.common import parse_time
    job = _job(paths)
    if args.parent_package:
        return workflow.render_package(job, Path(args.parent_package), args.mode, get_stages())
    if args.parent_review:
        return workflow.accept_review(job, Path(args.parent_review), args.mode)
    if not args.collected or not args.collect_status or not args.session_started:
        raise SystemExit('prepare worker requires the staged collection from the entry')
    return workflow.prepare(job, args.mode, get_stages(), staged_collection(args.collected, args.collect_status),
                            started=parse_time(args.session_started))


# ---------------------------------------------------------------------- main

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='BTCUSD chart-intel (parent-authored, local only)')
    parser.add_argument('mode', choices=('daily', 'weekly'))
    parser.add_argument('--dry-run', action='store_true', help='Show the plan; write nothing')
    parser.add_argument('--parent-package', type=Path, help='Validate the parent analysis and render the edition')
    parser.add_argument('--parent-review', type=Path, help='Record the explicit parent review')
    parser.add_argument('--root', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--collect-worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--finish-worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--collect-output', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--session-started', help=argparse.SUPPRESS)
    parser.add_argument('--collected', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--collect-status', choices=COLLECT_STATUSES, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.parent_package and args.parent_review:
        parser.error('Run render and review phases separately')
    if (args.collect_worker or args.finish_worker) and (
            args.worker or args.parent_package or args.parent_review or (args.collect_worker and args.finish_worker)
            or not args.collect_output or not args.session_started):
        parser.error('Invalid collect worker invocation')
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    if (args.collect_worker or args.finish_worker or args.worker) and credential_env_present():
        # Before any file or network work; names are never printed, values never read.
        print(json.dumps({'status': 'failed', 'error_category': 'credential_env_present'}))
        return GUARD_EXIT
    paths = Paths(root=args.root)
    if args.dry_run:
        print(json.dumps(dry_run_plan(paths, args), ensure_ascii=False))
        return 0
    if args.collect_worker or args.finish_worker or args.worker:
        runtime = str(Path(__file__).resolve().parent.parent)
        if runtime not in sys.path:
            sys.path.insert(0, runtime)
        os.umask(0o077)
        if args.collect_worker:
            return collect_worker(paths, args)
        if args.finish_worker:
            return finish_worker(paths, args)
        return run_worker(paths, args)
    os.umask(0o077)
    return run_entry(paths, args)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as error:  # noqa: BLE001 - type name only, no external text
        print(f'chart-intel-btcusd: {type(error).__name__}', file=sys.stderr)
        raise SystemExit(1)
