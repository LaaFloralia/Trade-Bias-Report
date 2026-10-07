"""Snapshot allow-list, job install, dry run, sandbox policy and the real entry chain.

The entry test runs the installed run.sh with sandbox-exec and a fake secrets
wrapper (no 1Password, no network) from a temporary committed repository.
"""
import json
import re
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import pytest

from btc import install_job, runner

REPO = Path(__file__).resolve().parents[2]
PYTHON = sys.executable


def test_snapshot_allow_list():
    allowed = runner.snapshot_allowed
    assert allowed('btc/runner.py') and allowed('btc/sources/fred.py') and allowed('btc/secrets.env.tpl')
    assert allowed('btc/schemas/machine.schema.json') and allowed('scripts/intel.py')
    for name in ('btc/job_template/run.sh', 'btc/job_template/PARENT-WORKFLOW.md', 'btc/.env', 'tests/btc/x.py',
                 'docs/btc/README.md', 'scripts/publish_report.py', 'config.yaml', 'btc/__pycache__/a.py',
                 '.env.tpl', 'btc/run.sh'):
        assert not allowed(name), name


def make_repo(tmp_path):
    repo = tmp_path / 'source'
    shutil.copytree(REPO / 'btc', repo / 'btc', ignore=shutil.ignore_patterns('__pycache__'))
    (repo / 'scripts').mkdir(parents=True)
    shutil.copy(REPO / 'scripts/intel.py', repo / 'scripts/intel.py')
    (repo / 'scrapers').mkdir()
    shutil.copy(REPO / 'scrapers/news_triage.py', repo / 'scrapers/news_triage.py')
    (repo / 'tests').mkdir()
    (repo / 'tests/secret.py').write_text('x = 1\n')
    (repo / '.env').write_text('FRED_API_KEY=not-a-real-value\n')
    git = ['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@example.invalid']
    subprocess.run(['git', 'init', '-q', '-b', 'master', str(repo)], check=True)
    subprocess.run(git + ['add', '-A'], check=True)
    subprocess.run(git + ['commit', '-q', '-m', 'init'], check=True)
    return repo


def test_install_from_commit_backs_up_and_refuses_protected(tmp_path):
    repo = make_repo(tmp_path)
    root = tmp_path / 'job'
    record = install_job.install(root, repo, Path(PYTHON))
    assert {'run.sh', 'runner.py', 'PARENT-WORKFLOW.md', 'boundary.sb'} <= set(record['sha256'])
    for directory in ('reports', 'work', 'logs', 'history', 'runtime'):
        assert (root / directory).is_dir()
    run_sh = (root / 'run.sh').read_text()
    assert f'BTC_JOB_ROOT={root}' in run_sh and '@ROOT@' not in run_sh
    assert oct((root / 'run.sh').stat().st_mode & 0o777) == '0o700'
    policy = (root / 'boundary.sb').read_text()
    assert '(deny file-write* (subpath "/"))' in policy and not re.search(r'@[A-Z_]+@', policy)
    writable = write_rules(policy)
    assert f'(subpath "{root.resolve()}/work")' in writable and f'(subpath "{root.resolve()}")' not in writable
    assert not any('/runtime' in rule for rule in writable)
    assert record['python_prefixes'] == [os.path.realpath(sys.prefix), os.path.realpath(sys.base_prefix)]
    # Uncommitted edits are never installed; a changed file is backed up first.
    (repo / 'btc/runner.py').write_text('# uncommitted\n')
    (root / 'runner.py').write_text('# locally edited\n')
    record = install_job.install(root, repo, Path(PYTHON))
    assert '# uncommitted' not in (root / 'runner.py').read_text()
    assert any(p.endswith('runner.py') for p in record['backups'])
    with pytest.raises(install_job.InstallError):
        install_job.install(install_job.PRODUCTION_ROOT, repo, Path(PYTHON))
    with pytest.raises(install_job.InstallError):
        install_job.install(Path('/Users/laa/.codex/jobs/chart-intel/sub'), repo, Path(PYTHON))
    with pytest.raises(install_job.InstallError):
        install_job.install(repo / 'job', repo, Path(PYTHON))


def run(root, *args, env=None):
    full = {**os.environ, **(env or {})}
    full.pop('BTC_JOB_ROOT', None)
    return subprocess.run(['/bin/bash', str(root / 'run.sh'), *args], capture_output=True, text=True,
                          env=full, timeout=300)


@pytest.fixture
def installed(tmp_path):
    repo = make_repo(tmp_path)
    root = tmp_path / 'job'
    install_job.install(root, repo, Path(PYTHON))
    return repo, root


def test_install_refuses_unexpected_python_locations(tmp_path, monkeypatch):
    """The python prefixes open reads under /Users/laa: home itself or a protected tree is refused."""
    repo = make_repo(tmp_path)
    for prefixes in (['/Users/laa', sys.base_prefix], [sys.prefix, '/Users'], ['/Users/laa/Brain/x/y', sys.prefix],
                     [sys.prefix, '/Users/laa/Library/Keychains/x'], ['/a b/c/d', sys.prefix]):
        fake = tmp_path / f'python-{uuid.uuid4().hex[:6]}'
        fake.write_text('#!/bin/sh\nprintf \'%s\\n\' \'' + json.dumps(prefixes) + '\'\n')
        fake.chmod(0o700)
        with pytest.raises(install_job.InstallError):
            install_job.install(tmp_path / 'job', repo, fake)
    assert not (tmp_path / 'job' / 'boundary.sb').exists()


def test_dry_run_writes_nothing(installed):
    repo, root = installed
    before = sorted(str(p) for p in root.rglob('*'))
    result = run(root, 'daily', '--dry-run')
    assert result.returncode == 0
    plan = json.loads(result.stdout)
    assert plan['dry_run'] is True and plan['symbol'] == 'BTCUSD' and plan['root'] == str(root)
    assert plan['collection']['secrets'] == ['FRED_API_KEY'] and plan['command'][0] == '/usr/bin/sandbox-exec'
    # op runs outside the sandbox and starts sandbox-exec, never the other way round; its token is
    # removed before sandbox-exec, and the news-detail worker runs without any wrapper.
    collect = plan['collection']['command']
    assert collect[1].endswith('runtime/btc/secrets.env.tpl')
    assert collect[2:6] == ['/usr/bin/env', '-u', 'OP_SERVICE_ACCOUNT_TOKEN', '/usr/bin/sandbox-exec']
    finish = plan['collection']['finish_command']
    assert finish[0] == '/usr/bin/sandbox-exec' and '--finish-worker' in finish
    assert not any('op-run' in part or part.endswith('.tpl') for part in finish)
    assert sorted(str(p) for p in root.rglob('*')) == before


def fake_wrapper(tmp_path, body):
    path = tmp_path / f'wrapper-{uuid.uuid4().hex[:6]}.sh'
    path.write_text('#!/bin/bash\n' + body + '\n')
    path.chmod(0o700)
    return path


def test_entry_chain_records_credential_failure(installed, tmp_path):
    repo, root = installed
    wrapper = fake_wrapper(tmp_path, 'exit 1')
    result = run(root, 'daily', env={'BTC_SECRETS_WRAPPER': str(wrapper)})
    assert result.returncode == 1
    state = json.loads((root / 'latest-daily.json').read_text())
    assert state['status'] == 'needs_attention' and state['error_category'] == 'credentials_unavailable'
    assert state['symbol'] == 'BTCUSD'
    manifest = json.loads((root / 'snapshot.json').read_text())
    assert set(manifest['sha256']) >= {'btc/runner.py', 'btc/workflow.py', 'btc/secrets.env.tpl', 'scripts/intel.py'}
    assert not (root / 'runtime/tests').exists() and not (root / 'runtime/.env').exists()
    assert not (root / 'runtime/btc/job_template').exists()
    entries = [json.loads(x) for x in (root / 'logs/entries.jsonl').read_text().splitlines()]
    assert entries[-1]['revision'] == manifest['revision'] and entries[-1]['entry_matches_snapshot'] is True


FAKE_TOKEN = 'fake-test-value'


def tree_text(root: Path) -> str:
    return '\n'.join(p.read_text(errors='replace') for p in root.rglob('*') if p.is_file() and p.stat().st_size < 10 ** 7)


def test_entry_chain_runs_collect_worker_in_sandbox(installed, tmp_path):
    repo, root = installed
    # Like op-run-batch.sh: export the service-account token, drop the template, exec the command
    # (op run passes its environment on to the child).
    wrapper = fake_wrapper(tmp_path, f'export OP_SERVICE_ACCOUNT_TOKEN={FAKE_TOKEN}; shift; exec "$@"')
    # BTC_STAGES=pending keeps the real pipeline from touching the network in this test.
    result = run(root, 'weekly', env={'BTC_SECRETS_WRAPPER': str(wrapper), 'BTC_STAGES': 'pending'})
    assert result.returncode == 1
    state = json.loads((root / 'latest-weekly.json').read_text())
    # The worker started (marker written inside the sandbox), then the pending stages refused to collect.
    assert state['error_category'] == 'collection_worker_failed' and state['session_slot'] == 'weekly'
    entries = [json.loads(x) for x in (root / 'logs/entries.jsonl').read_text().splitlines()]
    # Not credential_env_present: the token was removed before sandbox-exec, so the worker guard passed.
    assert entries[-1]['collect_status'] == 'collection_worker_failed'
    assert not list((root / 'work').glob('.collect-*'))  # staging removed by the entry
    assert FAKE_TOKEN not in tree_text(root) and FAKE_TOKEN not in result.stdout + result.stderr


def test_entry_chain_guard_trips_on_any_op_variable(installed, tmp_path):
    """A credential variable that is not removed (here OP_CONNECT_TOKEN) stops the collect worker before any work."""
    repo, root = installed
    wrapper = fake_wrapper(tmp_path, f'export OP_CONNECT_TOKEN={FAKE_TOKEN}; shift; exec "$@"')
    result = run(root, 'daily', env={'BTC_SECRETS_WRAPPER': str(wrapper), 'BTC_STAGES': 'pending'})
    assert result.returncode == 1
    state = json.loads((root / 'latest-daily.json').read_text())
    assert state['error_category'] == 'credential_env_present' and state['status'] == 'needs_attention'
    entries = [json.loads(x) for x in (root / 'logs/entries.jsonl').read_text().splitlines()]
    assert entries[-1]['collect_status'] == 'credential_env_present'
    assert FAKE_TOKEN not in tree_text(root) and FAKE_TOKEN not in result.stdout + result.stderr


@pytest.mark.parametrize('mode_args', [['--worker'], ['--collect-worker', '--collect-output', 'X', '--session-started',
                                                        '2026-10-08T07:00:00+09:00'],
                                       ['--finish-worker', '--collect-output', 'X', '--session-started',
                                        '2026-10-08T07:00:00+09:00']])
def test_worker_guard_refuses_injected_credentials(installed, mode_args):
    """Directly injected OP_* variables: every worker mode exits 78 before touching files; names/values not printed."""
    repo, root = installed
    runner.prepare_snapshot(runner.Paths(root=root, source=repo, python=PYTHON))
    worker = root / 'runtime/btc/runner.py'
    assert worker.is_file()
    before = sorted((str(p), p.stat().st_mtime_ns) for p in root.rglob('*') if p.is_file())
    args = [a.replace('X', str(root / 'work/.collect-guard/collection.json')) for a in mode_args]
    env = {k: v for k, v in os.environ.items() if not k.startswith('OP_')}
    result = subprocess.run([PYTHON, '-B', str(worker), 'daily', *args, '--root', str(root)], capture_output=True,
                            text=True, timeout=60, env={**env, 'OP_SERVICE_ACCOUNT_TOKEN': FAKE_TOKEN})
    assert result.returncode == 78
    assert json.loads(result.stdout) == {'status': 'failed', 'error_category': 'credential_env_present'}
    assert FAKE_TOKEN not in result.stdout + result.stderr and 'OP_SERVICE' not in result.stdout + result.stderr
    assert sorted((str(p), p.stat().st_mtime_ns) for p in root.rglob('*') if p.is_file()) == before
    assert runner.credential_env_present({'OP_X': '1'}) and not runner.credential_env_present({'XOP_': '1'})


CHAIN_STAGES = """

class ChainStages(PendingStages):
    \"\"\"Test-only stages (temporary repository): no network; record which OP_ names each worker saw.\"\"\"

    def collect_context(self, ctx):
        started = ctx.started_at.isoformat()
        rows = [{'id': 'n001', 'url': 'https://www.coindesk.com/markets/2026/10/08/synthetic-one',
                 'title': 'Synthetic bitcoin headline one', 'excerpt': 'Synthetic text.', 'publisher': 'CoinDesk',
                 'published_at': '2026-10-07T21:00:00Z', 'source_id': 'coindesk_rss', 'official': False},
                {'id': 'n002', 'url': 'http://insecure.example/x', 'title': 'Synthetic headline two',
                 'excerpt': '', 'publisher': 'CoinDesk', 'published_at': '2026-10-07T20:00:00Z',
                 'source_id': 'coindesk_rss', 'official': False}]
        return {'symbol': 'BTCUSD', 'mode': ctx.mode, 'collection_started_at': started, 'phases': [],
                'op_names_collect': sorted(k for k in os.environ if k.startswith('OP_')),
                'sources': [{'source_id': 'news', 'status': 'ok', 'values': {'candidates': rows}}]}

    def finish_collection(self, context, scores, selection, ctx):
        stamp = context['collection_started_at']
        return {'symbol': 'BTCUSD', 'collection_started_at': stamp, 'collection_completed_at': stamp,
                'op_names_collect': context['op_names_collect'],
                'op_names_finish': sorted(k for k in os.environ if k.startswith('OP_')),
                'selection': selection, 'scores': scores, 'sources': []}


def get_stages():
    if os.environ.get('BTC_STAGES') == 'chain':
        return ChainStages()
    if os.environ.get('BTC_STAGES') == 'pending':
        return PendingStages()
    from btc.pipeline import BtcStages
    return BtcStages()
"""


def test_entry_chain_collect_triage_finish(tmp_path):
    """Collect worker (wrapper, sandbox) -> entry triage -> news-detail worker (sandbox, no wrapper) -> prepare.

    Jev is disabled here (LAA_JEV_DISABLED=1, honoured by the entry), so nothing leaves the machine.
    """
    repo = make_repo(tmp_path)
    stages = repo / 'btc/stages.py'
    stages.write_text(stages.read_text() + CHAIN_STAGES)
    git = ['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@example.invalid']
    subprocess.run(git + ['commit', '-q', '-am', 'chain stages'], check=True)
    root = tmp_path / 'job'
    install_job.install(root, repo, Path(PYTHON))
    wrapper = fake_wrapper(tmp_path, f'export OP_SERVICE_ACCOUNT_TOKEN={FAKE_TOKEN}; shift; exec "$@"')
    result = run(root, 'daily', env={'BTC_SECRETS_WRAPPER': str(wrapper), 'BTC_STAGES': 'chain',
                                     'LAA_JEV_DISABLED': '1'})
    assert result.returncode == 1  # build_facts is pending in the test stages
    state = json.loads((root / 'latest-daily.json').read_text())
    assert state['error_category'] == 'pipeline_not_configured'
    collection = json.loads((Path(state['work_path']) / 'collection.json').read_text())
    assert collection['op_names_collect'] == [] and collection['op_names_finish'] == []
    assert collection['selection'] == {'mode': 'fallback', 'reason': 'disabled'} and collection['scores'] is None
    entries = [json.loads(x) for x in (root / 'logs/entries.jsonl').read_text().splitlines()]
    assert entries[-1]['collect_status'] == 'ok'
    assert entries[-1]['news_triage'] == {'mode': 'fallback', 'reason': 'disabled'}
    assert not list((root / 'work').glob('.collect-*'))
    assert FAKE_TOKEN not in tree_text(root)


def test_lock_prevents_parallel_runs(installed, tmp_path):
    import fcntl
    repo, root = installed
    with (root / 'run.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = run(root, 'daily', env={'BTC_SECRETS_WRAPPER': str(fake_wrapper(tmp_path, 'exit 1'))})
    assert result.returncode == 1 and '"stage": "entry"' in result.stdout


def test_sandbox_subpath_semantics(tmp_path):
    """The XAU deny rule must not block the sibling BTC root (subpath matches whole components)."""
    xau, btc = tmp_path / 'chart-intel', tmp_path / 'chart-intel-btcusd'
    xau.mkdir()
    btc.mkdir()
    policy = tmp_path / 'p.sb'
    policy.write_text(f'(version 1)\n(allow default)\n(deny file-write* (subpath "{xau.resolve()}"))\n')
    write = lambda target: subprocess.run(['/usr/bin/sandbox-exec', '-f', str(policy), '/usr/bin/touch', str(target)],
                                          capture_output=True).returncode
    assert write(btc.resolve() / 'ok') == 0 and (btc / 'ok').exists()
    assert write(xau.resolve() / 'no') != 0 and not (xau / 'no').exists()


SANDBOX = Path('/usr/bin/sandbox-exec')
HOME = Path.home()


def user_temp_dir() -> Path:
    out = subprocess.check_output(['/usr/bin/getconf', 'DARWIN_USER_TEMP_DIR'], text=True).strip()
    return Path(os.path.realpath(out.rstrip('/')))


def write_rules(policy: str) -> list[str]:
    """Rules inside the single (allow file-write* ...) block, comments dropped."""
    return rule_block(policy, '(allow file-write*')


def test_policy_write_allow_list_is_exact(installed):
    """Writable: the job's mutable areas, the run TMPDIR and /dev streams only (no op dir, no Jev ledger)."""
    _, root = installed
    policy = (root / 'boundary.sb').read_text()
    assert policy.count('(allow file-write*') == 1 and policy.count('(allow ') == 3  # + default, read-data
    root_re = re.sub(r'([.^$|()\[\]{}*+?\\])', r'\\\1', str(root.resolve()))
    expected = [f'(subpath "{root.resolve()}/{d}")' for d in ('reports', 'work', 'logs', 'history')] + [
        f'(regex #"^{root_re}/\\.?latest-(daily|weekly)\\.json(\\.[A-Za-z0-9_]+\\.tmp)?$")',
        '(subpath (param "TMPDIR"))',
        '(literal "/dev/null")', '(literal "/dev/zero")', '(literal "/dev/dtracehelper")',
        '(regex #"^/dev/tty")', '(regex #"^/dev/fd/")']
    assert write_rules(policy) == expected
    for absent in ('/private/tmp', 'com.agilebits', '.codex/work/jev'):
        assert absent not in policy, absent
    assert 'Chrom' not in ' '.join(write_rules(policy))


def rule_block(policy: str, head: str) -> list[str]:
    """Rules of the block that starts with ``head`` (comments dropped, closing parenthesis removed)."""
    lines, depth = [], None
    for raw in policy.splitlines():
        line = raw.split(';', 1)[0].strip()
        if depth is None:
            if line == head:
                depth = 1
            continue
        if line:
            lines.append(line)
            depth += line.count('(') - line.count(')')
            if depth <= 0:
                break
    return lines[:-1] + [lines[-1][:-1]]


def test_policy_read_rules_are_exact(installed):
    """Reads under /Users/laa: python prefixes, bundled Chromium, renderer, job root, TMPDIR; denials last."""
    _, root = installed
    policy = (root / 'boundary.sb').read_text()
    assert '(deny file-read-data (subpath "/Users/laa"))' in policy
    assert rule_block(policy, '(allow file-read-data') == [
        f'(subpath "{os.path.realpath(sys.prefix)}")', f'(subpath "{os.path.realpath(sys.base_prefix)}")',
        '(subpath "/Users/laa/Library/Caches/ms-playwright")', '(subpath "/Users/laa/.agents/skills/human-first-docs")',
        f'(subpath "{root.resolve()}")', '(subpath (param "TMPDIR"))']
    assert rule_block(policy, '(deny file-read*') == [
        '(subpath "/Users/laa/Brain")', '(subpath "/Users/laa/.codex/jobs/chart-intel")',
        '(subpath "/Users/laa/Library/Keychains")', '(subpath "/Library/Keychains")']
    assert rule_block(policy, '(deny process-exec') == [
        '(literal "/usr/bin/security")', '(literal "/opt/homebrew/bin/op")', '(literal "/usr/local/bin/op")',
        '(subpath "/opt/homebrew/Caskroom/1password-cli")', '(subpath "/Applications/1Password.app")',
        '(regex #"/op$")']
    assert len(rule_block(policy, '(deny mach-lookup')) == 6
    order = [policy.index(x) for x in ('(deny file-read-data', '(allow file-read-data', '(deny file-read*',
                                       '(deny mach-lookup', '(deny process-exec')]
    assert order == sorted(order) and policy.rstrip().endswith('(regex #"/op$"))')


@pytest.mark.skipif(not SANDBOX.exists(), reason='sandbox-exec not available')
def test_installed_policy_denies_writes_outside_mutable_areas(installed, tmp_path):
    """A3: deny-all writes; only the mutable job areas and the run TMPDIR (no user temp, no /private/tmp).

    Only new probe names are created; existing files are never opened.
    """
    repo, root = installed
    run_tmp = tmp_path / 'run-tmp'
    run_tmp.mkdir()
    tag = uuid.uuid4().hex[:8]
    policy = root / 'boundary.sb'
    user_tmp = user_temp_dir()  # shared by every process of the user (Chrome's singleton socket lives here)

    def touch(target):
        return subprocess.run([str(SANDBOX), '-f', str(policy), '-D', f'TMPDIR={run_tmp.resolve()}',
                               '/usr/bin/touch', str(target)], capture_output=True).returncode

    denied = [HOME / f'.btc-probe-{tag}', HOME / 'Library/LaunchAgents' / f'btc-probe-{tag}.plist',
              Path('/opt/homebrew') / f'btc-probe-{tag}', root / 'runtime' / f'probe-{tag}',
              root / f'probe-{tag}', root / f'boundary.sb.{tag}', HOME / '.config/laa' / f'probe-{tag}',
              HOME / '.local/share/laa-automation' / f'probe-{tag}', Path('/Users/laa/hq') / f'probe-{tag}',
              HOME / '.ssh' / f'probe-{tag}', Path('/Users/laa/dev') / f'probe-{tag}',
              user_tmp / f'btc-probe-{tag}', user_tmp / f'com.google.Chrome.probe{tag}',
              user_tmp / f'org.chromium.Chromium.probe{tag}', Path('/private/tmp') / f'btc-probe-{tag}']
    allowed = [root / 'work' / f'probe-{tag}', root / 'reports' / f'probe-{tag}', root / 'history' / f'probe-{tag}',
               root / 'latest-daily.json', run_tmp / f'probe-{tag}']
    results = {}
    try:
        for target in denied:
            if target.parent.is_dir() and not target.exists():
                results[str(target)] = touch(target)
                assert results[str(target)] != 0 and not target.exists(), target
        for target in allowed:
            assert touch(target) == 0 and target.exists(), target
    finally:
        for target in denied + allowed:
            if target.name.endswith(tag) or target.name.endswith(f'{tag}.plist') or target == root / 'latest-daily.json':
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
    assert results, 'no denial was exercised'
    for protected in (Path('/Users/laa/Brain'), Path('/Users/laa/.codex/jobs/chart-intel')):
        if protected.is_dir():
            read = subprocess.run([str(SANDBOX), '-f', str(policy), '-D', f'TMPDIR={run_tmp.resolve()}',
                                   '/bin/ls', str(protected)], capture_output=True)
            assert read.returncode != 0, protected


BROWSER_PROBE = '''
import os, sys
from playwright.sync_api import sync_playwright
channel = os.environ.get('BTC_PLAYWRIGHT_CHANNEL') or None
with sync_playwright() as p:
    browser = p.chromium.launch(channel=channel) if channel else p.chromium.launch()
    page = browser.new_page(java_script_enabled=False)
    page.set_content('<p>probe</p>')
    page.screenshot(path=os.path.join(os.environ['TMPDIR'], 'probe.png'))
    browser.close()
print('browser-ok')
'''


@pytest.mark.skipif(not SANDBOX.exists(), reason='sandbox-exec not available')
def test_browser_runs_inside_installed_policy(installed, tmp_path):
    """Production browser (Playwright's bundled Chromium) works under the policy with the runner's environment."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        if not Path(p.chromium.executable_path).exists():
            pytest.skip('Playwright bundled Chromium is not installed')
    _, root = installed
    run_tmp = (tmp_path / 'run-tmp')
    run_tmp.mkdir()
    env = runner.isolated_environment(runner.Paths(root=root), run_tmp.resolve())
    assert 'BTC_PLAYWRIGHT_CHANNEL' not in env and env['MAC_CHROMIUM_TMPDIR'] == str(run_tmp.resolve())
    result = subprocess.run([str(SANDBOX), '-f', str(root / 'boundary.sb'), '-D', f'TMPDIR={run_tmp.resolve()}',
                             PYTHON, '-B', '-c', BROWSER_PROBE], capture_output=True, text=True, env=env, timeout=120,
                            cwd=run_tmp)  # like the worker (cwd inside the job root): /Users/laa is unreadable
    assert result.returncode == 0 and 'browser-ok' in result.stdout, result.stderr[-2000:]
    assert (run_tmp / 'probe.png').stat().st_size > 0


SECRET_PROBE = '''
import json, os, subprocess, sys
def label(action):
    try:
        action()
        return 'allowed'
    except PermissionError:
        return 'EPERM'
    except FileNotFoundError:
        return 'missing'
    except OSError as error:
        return f'errno{error.errno}'
quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
home = os.path.expanduser('~')
print(json.dumps({
    'security_exec': label(lambda: subprocess.run(['/usr/bin/security', 'list-keychains'], **quiet)),
    'op_exec': label(lambda: subprocess.run(['/opt/homebrew/bin/op', '--version'], **quiet)),
    'keychains_listdir': label(lambda: os.listdir(os.path.join(home, 'Library/Keychains'))),
    'agents_md_read': label(lambda: open('/Users/laa/AGENTS.md', 'rb').read(1)),
    'ssh_listdir': label(lambda: os.listdir(os.path.join(home, '.ssh'))),
    'source_read': label(lambda: open(sys.argv[1], 'rb').read(1)),
    'job_root_read': label(lambda: open(sys.argv[2], 'rb').read(1)),
}))
'''


@pytest.mark.skipif(not SANDBOX.exists(), reason='sandbox-exec not available')
def test_sandbox_refuses_secret_paths_and_home_reads(installed, tmp_path):
    """Rule D: probes run only inside the sandbox and print fixed labels, never content.

    Exec of security / op, the Keychain directory, ~/.ssh, the common instructions and the source
    repository are refused (EPERM); the job root stays readable.
    """
    if not Path('/Users/laa').is_dir():
        pytest.skip('LAA home layout not present')
    _, root = installed
    run_tmp = tmp_path / 'run-tmp'
    run_tmp.mkdir()
    env = runner.isolated_environment(runner.Paths(root=root), run_tmp.resolve())
    result = subprocess.run([str(SANDBOX), '-f', str(root / 'boundary.sb'), '-D', f'TMPDIR={run_tmp.resolve()}',
                             PYTHON, '-B', '-c', SECRET_PROBE, str(REPO / 'btc/runner.py'), str(root / 'run.sh')],
                            capture_output=True, text=True, env=env, timeout=60, cwd=run_tmp)
    assert result.returncode == 0, result.stderr[-1000:]
    labels = json.loads(result.stdout)
    assert labels['security_exec'] == 'EPERM' and labels['keychains_listdir'] == 'EPERM'
    assert labels['agents_md_read'] == 'EPERM' and labels['op_exec'] in ('EPERM', 'missing')
    assert labels['ssh_listdir'] in ('EPERM', 'missing')
    if str(REPO).startswith('/Users/laa/'):
        assert labels['source_read'] == 'EPERM'
    assert labels['job_root_read'] == 'allowed'
