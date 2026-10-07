#!/usr/bin/env python3
"""Install the BTCUSD chart-intel job root from the committed source HEAD.

    python -B -m btc.install_job --root <abs dir outside /tmp> [--source-repo <repo>] [--python <venv python>]
    python -B -m btc.install_job --production   # parent only, after the master merge

The root must be a private directory: /tmp and /private/tmp are refused
(world-writable). Creates ``run.sh``, ``runner.py`` (entry),
``PARENT-WORKFLOW.md``, ``boundary.sb`` (BTC sandbox policy, with the job root
and the worker python's ``sys.prefix`` / ``sys.base_prefix`` filled in) and
``reports/ work/ logs/ history/ runtime/``. Changed existing files are backed up to ``backups/<time>/``
before replacement. Files come from ``git show <revision>:<path>`` so
uncommitted edits are never installed. Nothing is run or scheduled.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from zoneinfo import ZoneInfo

JST = ZoneInfo('Asia/Tokyo')
PRODUCTION_ROOT = Path('/Users/laa/.codex/jobs/chart-intel-btcusd')
DEFAULT_SOURCE = Path('/Users/laa/dev/fundamental-macro-analysis')
DEFAULT_PYTHON = DEFAULT_SOURCE / '.venv/bin/python'
XAU_ROOT = Path('/Users/laa/.codex/jobs/chart-intel')
PROTECTED = (Path('/Users/laa/dev'), XAU_ROOT, Path('/Users/laa/Brain'), Path('/Users/laa/hq'))
DIRECTORIES = ('reports', 'work', 'logs', 'history', 'runtime', 'backups')
FILES = {  # target name -> (source path in the commit, mode)
    'run.sh': ('btc/job_template/run.sh', 0o700),
    'runner.py': ('btc/runner.py', 0o600),
    'PARENT-WORKFLOW.md': ('btc/job_template/PARENT-WORKFLOW.md', 0o600),
    'boundary.sb': ('btc/job_template/boundary.sb', 0o600),
}


class InstallError(RuntimeError):
    pass


def git(source: Path, *args: str) -> bytes:
    return subprocess.check_output(['git', '-C', str(source), *args], stderr=subprocess.DEVNULL)


SAFE_PATH = re.compile(r'^/[A-Za-z0-9/._-]+$')
UNSAFE_ROOTS = (Path("/tmp"), Path("/private/tmp"))  # world-writable


def _sb_regex(path: str) -> str:
    return re.sub(r'([.^$|()\[\]{}*+?\\])', r'\\\1', path)


HOME = Path('/Users/laa')
READ_DENIED = (Path('/Users/laa/Brain'), XAU_ROOT, HOME / 'Library/Keychains', HOME / '.ssh', HOME / '.config')
PREFIX_PROBE = 'import json, sys; print(json.dumps([sys.prefix, sys.base_prefix]))'


def python_prefixes(python: Path) -> tuple[str, str]:
    """The worker python's sys.prefix and sys.base_prefix (real paths), checked before they open reads."""
    try:
        out = subprocess.run([str(python), '-I', '-B', '-c', PREFIX_PROBE], capture_output=True, text=True,
                             timeout=60, check=True, env={'PATH': '/usr/bin:/bin'}).stdout
        values = json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        raise InstallError('Cannot determine the worker python prefixes') from None
    if not (isinstance(values, list) and len(values) == 2 and all(isinstance(v, str) for v in values)):
        raise InstallError('Cannot determine the worker python prefixes')
    prefixes = []
    for value in values:
        real = Path(os.path.realpath(value))
        # Too broad (home, a parent of a protected tree) or odd characters would reopen reads: refuse.
        if not SAFE_PATH.match(str(real)) or len(real.parts) < 4 or HOME.is_relative_to(real) \
                or any(protected.is_relative_to(real) or real.is_relative_to(protected) for protected in READ_DENIED):
            raise InstallError('Unexpected worker python location for the sandbox policy')
        prefixes.append(str(real))
    return prefixes[0], prefixes[1]


def sandbox_policy(template: str, root: Path, prefixes: tuple[str, str]) -> str:
    """Fill the job root and the python prefixes into the policy (P3, A3; read restrictions)."""
    if not SAFE_PATH.match(str(root)) or '..' in root.parts:
        raise InstallError('Unsupported path characters for the sandbox policy')
    real = Path(os.path.realpath(root))
    if any(real == base or real.is_relative_to(base) for base in UNSAFE_ROOTS):
        # World-writable temp trees: code there could be replaced by other local processes.
        raise InstallError('Job root must not be inside a temporary directory')
    if any(real.is_relative_to(protected) or protected.is_relative_to(real) for protected in READ_DENIED):
        raise InstallError('Job root overlaps a read-protected location')
    prefix, base = prefixes
    return (template.replace('@ROOT_RE@', _sb_regex(str(real))).replace('@ROOT@', str(real))
            .replace('@PY_PREFIX@', prefix).replace('@PY_BASE@', base).rstrip('\n') + '\n')


def render_run_sh(template: str, root: Path, source: Path, python: Path) -> str:
    for value in (root, source, python):
        if any(ch in str(value) for ch in ' \'"$`\\'):
            raise InstallError('Unsupported path characters for run.sh')
    return template.replace('@ROOT@', str(root)).replace('@SOURCE@', str(source)).replace('@PYTHON@', str(python))


def check_root(root: Path, source: Path, production: bool) -> None:
    if root == PRODUCTION_ROOT and not production:
        raise InstallError('The production root is installed by the parent with --production')
    if production:
        if root != PRODUCTION_ROOT or source != DEFAULT_SOURCE:
            raise InstallError('--production installs the default source into the production root only')
        branch = git(source, 'rev-parse', '--abbrev-ref', 'HEAD').decode().strip()
        if branch != 'master':
            raise InstallError('Production install requires the source repository on master')
    for protected in PROTECTED:
        if root == protected or root.is_relative_to(protected) or protected.is_relative_to(root):
            raise InstallError('Job root overlaps a protected location')
    if root == source or root.is_relative_to(source):
        raise InstallError('Job root must be outside the source repository')


def write_private(path: Path, data: bytes, mode: int) -> None:
    tmp = path.with_name(f'.{path.name}.install-tmp')
    tmp.write_bytes(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def install(root: Path, source: Path, python: Path, production: bool = False, ref: str = 'HEAD') -> dict:
    root, source = root.absolute(), source.absolute()
    check_root(root, source, production)
    revision = git(source, 'rev-parse', ref).decode().strip()
    contents = {}
    for name, (path, mode) in FILES.items():
        try:
            contents[name] = git(source, 'show', f'{revision}:{path}')
        except subprocess.CalledProcessError:
            raise InstallError(f'Committed source lacks {path}') from None
    contents['run.sh'] = render_run_sh(contents['run.sh'].decode(), root, source, python).encode()
    prefixes = python_prefixes(python)
    contents['boundary.sb'] = sandbox_policy(contents['boundary.sb'].decode(), root, prefixes).encode()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    for directory in DIRECTORIES:
        (root / directory).mkdir(exist_ok=True, mode=0o700)
    stamp = datetime.now(JST).strftime('%Y%m%dT%H%M%S%f')
    backups, installed = [], {}
    for name, data in contents.items():
        target = root / name
        if target.exists() and target.read_bytes() != data:
            backup_dir = root / 'backups' / stamp
            backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_private(backup_dir / name, target.read_bytes(), 0o600)
            backups.append(str(backup_dir / name))
        write_private(target, data, FILES[name][1])
        installed[name] = hashlib.sha256(data).hexdigest()
    record = {'symbol': 'BTCUSD', 'installed_at': datetime.now(JST).isoformat(), 'root': str(root),
              'source_repo': str(source), 'revision': revision, 'python': str(python),
              'python_prefixes': list(prefixes),
              'production': production, 'sha256': installed, 'backups': backups}
    write_private(root / 'install.json', (json.dumps(record, indent=2, ensure_ascii=False) + '\n').encode(), 0o600)
    return record


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--source-repo', type=Path, default=DEFAULT_SOURCE)
    parser.add_argument('--python', type=Path, default=DEFAULT_PYTHON)
    parser.add_argument('--ref', default='HEAD')
    parser.add_argument('--production', action='store_true')
    args = parser.parse_args(argv)
    if args.root is None and not args.production:
        parser.error('--root is required (or --production for the parent)')
    try:
        record = install(args.root or PRODUCTION_ROOT, args.source_repo, args.python, args.production, args.ref)
    except (InstallError, subprocess.CalledProcessError) as error:
        print(f'install_job: {error if isinstance(error, InstallError) else "git command failed"}', file=sys.stderr)
        return 1
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
