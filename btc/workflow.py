"""Parent-authored BTCUSD editions: prepare -> parent package -> parent review.

Same stages and state names as XAU chart-intel/parent_workflow.py:
running -> awaiting_parent_authoring -> awaiting_parent_review ->
succeeded_local | needs_attention | failed. ``latest-<mode>.json`` is the
state record overwritten at every stage and always carries
``symbol: "BTCUSD"``. Deterministic tools never launch another AI.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import uuid

from btc import SYMBOL
from btc.common import (DIR_MODE, FILE_MODE, JST, JobError, contained, digest, ensure_dir, now_jst,
                        parse_time, read_json, write_bytes, write_json, write_text)
from btc.stages import AnalysisError, RunContext, Stages

MODES = ('daily', 'weekly')
MAX_DATA_AGE = timedelta(hours=6)
NEEDS_ATTENTION = {'credentials_unavailable', 'credential_env_present', 'collection_failed', 'collection_worker_failed',
                   'collection_timeout', 'collection_invalid', 'collection_stale'}


@dataclass
class Job:
    root: Path
    python: Path
    secrets_wrapper: Path = Path('/Users/laa/.config/laa/op-run-batch.sh')
    collect_timeout: int = 15 * 60

    def __post_init__(self):
        self.root = Path(self.root).absolute()
        self.python = Path(self.python)
        self.secrets_wrapper = Path(self.secrets_wrapper)

    runtime = property(lambda self: self.root / 'runtime')
    reports = property(lambda self: self.root / 'reports')
    work = property(lambda self: self.root / 'work')
    logs = property(lambda self: self.root / 'logs')
    history = property(lambda self: self.root / 'history')
    workflow_path = property(lambda self: self.root / 'PARENT-WORKFLOW.md')
    secrets_template = property(lambda self: self.runtime / 'btc' / 'secrets.env.tpl')
    worker_script = property(lambda self: self.runtime / 'btc' / 'runner.py')


def session_slot(mode: str, started: datetime) -> str:
    """P8: Daily morning (London-before) = am, evening (NY-before) = pm."""
    if mode == 'weekly':
        return 'weekly'
    if mode != 'daily':
        raise JobError('invalid_mode')
    return 'am' if started.astimezone(JST).hour < 13 else 'pm'


def base_record(mode: str, now: datetime) -> dict:
    return {'mode': mode, 'symbol': SYMBOL, 'started_at': now.isoformat(), 'ok': False, 'status': 'running',
            'stage': 'collection', 'generation_status': 'not_started', 'review_status': 'not_performed',
            'independent_review_status': 'not_performed', 'publication_ready': False,
            'publication_status': 'disabled', 'delivery': 'disabled', 'historical_revision': False,
            'outputs': {}, 'execution': 'parent_only'}


def append_log(path: Path, record: dict) -> None:
    ensure_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, FILE_MODE)
    with os.fdopen(fd, 'a', encoding='utf-8') as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + '\n')


def record_status(job: Job, record: dict) -> dict:
    record['symbol'] = SYMBOL
    record['updated_at'] = now_jst().isoformat()
    write_json(job.root / f'latest-{record["mode"]}.json', record)
    append_log(job.logs / 'runs.jsonl', record)
    return record


def failure(job: Job, record: dict, error: BaseException, default_category: str) -> dict:
    category = error.category if isinstance(error, JobError) else default_category
    status = 'needs_attention' if category in NEEDS_ATTENTION else 'failed'
    if record.get('stage') in ('parent_render', 'parent_review'):
        status = 'needs_attention'
    record.update(ok=False, status=status, error=type(error).__name__, error_category=category)
    if record.get('generation_status') in ('not_started', 'running'):
        record['generation_status'] = 'failed'
    return record_status(job, record)


def new_work_folder(job: Job, mode: str, now: datetime) -> Path:
    folder = job.work / f'parent-{mode}-{now.astimezone(JST).strftime("%Y%m%dT%H%M%S")}-{uuid.uuid4().hex[:8]}'
    ensure_dir(job.work)
    folder.mkdir(mode=DIR_MODE)
    return folder


def check_fresh(collected_at: str, now: datetime, max_age: timedelta = MAX_DATA_AGE) -> datetime:
    stamp = parse_time(collected_at)
    if stamp > now:
        raise JobError('collection_future', 'Collection timestamp is in the future')
    if now - stamp > max_age:
        raise JobError('collection_stale', 'Collection is older than six hours')
    return stamp


def validate_collection(collection: dict) -> str:
    if not isinstance(collection, dict) or collection.get('symbol') != SYMBOL:
        raise JobError('collection_invalid', 'Collection is not a BTCUSD record')
    try:
        started = parse_time(collection['collection_started_at'])
        completed = parse_time(collection['collection_completed_at'])
    except (KeyError, ValueError):
        raise JobError('collection_invalid', 'Collection timestamps are missing or naive') from None
    if completed < started or not isinstance(collection.get('sources'), list):
        raise JobError('collection_invalid', 'Collection record is inconsistent')
    return started.astimezone(JST).isoformat()


def previous_passed_editions(job: Job, mode: str, limit: int = 3, before: datetime | None = None) -> list[dict]:
    """Only this job's parent-passed editions whose bytes still match the review."""
    base = job.reports / mode / 'editions'
    found = []
    if not base.is_dir():
        return found
    for edition in base.glob('*/*.edition.json'):
        try:
            record = read_json(edition)
            outputs = record['outputs']
            if (record.get('symbol') != SYMBOL or record.get('mode') != mode or record.get('status') != 'succeeded_local'
                    or record.get('review_status') != 'parent_passed'):
                continue
            review = read_json(outputs['parent_review_path'])
            if (review.get('status') != 'parent_passed' or review.get('htmlSha256') != digest(outputs['html_path'])
                    or review.get('bundleSha256') != digest(outputs['bundle_path'])
                    or review.get('machineSha256') != digest(outputs['json_path'])):
                continue
            collected = parse_time(record['collected_at'])
            if before is not None and collected >= before:
                continue
            found.append({'edition_path': str(edition), 'collected_at': collected.isoformat(),
                          'session_slot': record.get('session_slot'), 'md_path': outputs['md_path'],
                          'machine_path': outputs['json_path'], 'facts_path': outputs.get('facts_path')})
        except (OSError, KeyError, ValueError, TypeError):
            continue
    found.sort(key=lambda x: parse_time(x['collected_at']), reverse=True)
    return found[:limit]


def prepare(job: Job, mode: str, stages: Stages, collect, clock=now_jst, started: datetime | None = None) -> int:
    """Collect, compute facts and write the parent's request. ``collect(job, ctx, output)``.

    ``started`` is the collection start chosen by the entry (it names the work folder).
    """
    if mode not in MODES:
        raise JobError('invalid_mode')
    now = started.astimezone(JST) if started is not None else clock()
    record = record_status(job, base_record(mode, now))
    try:
        folder = new_work_folder(job, mode, now)
        slot = session_slot(mode, now)
        ctx = RunContext(root=job.root, mode=mode, session_slot=slot, started_at=now, work_dir=folder,
                         history_dir=job.history,
                         previous_editions=previous_passed_editions(job, mode, before=now))
        record.update(work_path=str(folder), session_slot=slot)
        collection_path = folder / 'collection.json'
        collect(job, ctx, collection_path)
        if not collection_path.is_file():
            raise JobError('collection_failed', 'Collection produced no record')
        collection = read_json(collection_path)
        collected_at = validate_collection(collection)
        check_fresh(collected_at, clock())
        ctx.started_at = parse_time(collected_at)
        record.update(stage='facts', collected_at=collected_at)
        facts = stages.build_facts(collection, ctx)
        if not isinstance(facts, dict) or facts.get('symbol') != SYMBOL:
            raise JobError('facts_invalid', 'Facts are not a BTCUSD record')
        facts_path = write_json(folder / 'facts.json', facts)
        input_path = write_text(folder / 'analysis-input.md', stages.analysis_input(collection, facts, ctx))
        schema_path = write_json(folder / 'analysis.schema.json', stages.analysis_schema())
        request = {'schemaVersion': 1, 'symbol': SYMBOL, 'mode': mode, 'session_slot': slot,
                   'created_at': now.isoformat(), 'collected_at': collected_at,
                   'data_path': str(collection_path), 'data_sha256': digest(collection_path),
                   'facts_path': str(facts_path), 'facts_sha256': digest(facts_path),
                   'input_path': str(input_path), 'input_sha256': digest(input_path),
                   'analysis_schema_path': str(schema_path), 'analysis_schema_sha256': digest(schema_path),
                   'previous_editions': ctx.previous_editions, 'workflow': str(job.workflow_path),
                   'historical': False, 'status': 'awaiting_parent_authoring', 'publication_ready': False}
        request_path = write_json(folder / 'request.json', request)
        record.update(status='awaiting_parent_authoring', stage='parent_authoring',
                      request_path=str(request_path))
        record_status(job, record)
        print(json.dumps({'status': record['status'], 'symbol': SYMBOL, 'request_path': str(request_path),
                          'session_slot': slot, 'complete': False}, ensure_ascii=False))
        return 0
    except Exception as error:  # noqa: BLE001 - recorded as a fixed category
        failure(job, record, error, 'parent_input_preparation_failed')
        print(json.dumps({'status': record['status'], 'error_category': record['error_category']}))
        return 1


def report_basename(mode: str, collected_at: str) -> str:
    stamp = parse_time(collected_at).astimezone(JST)
    return f'BTCUSD_{mode.capitalize()}_Report_{stamp.strftime("%Y-%m-%d_%H%M")}'


def _load_request(job: Job, package_path: Path, expected_mode: str | None):
    package_path = contained(package_path, job.root)
    package = read_json(package_path)
    request_path = contained(package['request_path'], job.work)
    request = read_json(request_path)
    mode = request.get('mode')
    if mode not in MODES or request.get('symbol') != SYMBOL:
        raise JobError('request_invalid', 'Invalid request mode or symbol')
    if expected_mode is not None and mode != expected_mode:
        raise JobError('mode_mismatch', 'Package mode differs from the scheduled task')
    if request.get('status') != 'awaiting_parent_authoring' or request.get('historical') is not False:
        raise JobError('request_invalid', 'Request is not awaiting parent authoring')
    for key in ('data', 'facts', 'input', 'analysis_schema'):
        path = contained(request[f'{key}_path'], request_path.parent)
        if digest(path) != request[f'{key}_sha256']:
            raise JobError('input_changed', f'{key} changed after request')
    return package_path, package, request_path, request


def render_package(job: Job, package_path, expected_mode: str | None, stages: Stages,
                   now: datetime | None = None, browser_check=None, render=None) -> int:
    from btc import acceptance, bundle as bundle_mod, machine as machine_mod, presentation
    browser_check = browser_check or acceptance.browser_check
    render = render or presentation.render
    record = {'mode': expected_mode or 'daily', 'ok': False, 'status': 'running', 'stage': 'parent_render',
              'generation_status': 'running', 'review_status': 'not_performed',
              'independent_review_status': 'not_performed', 'publication_ready': False,
              'publication_status': 'disabled', 'delivery': 'disabled', 'historical_revision': False,
              'outputs': {}, 'execution': 'parent_only'}
    try:
        package_path, package, request_path, request = _load_request(job, Path(package_path), expected_mode)
        mode, slot, collected_at = request['mode'], request['session_slot'], request['collected_at']
        record.update(mode=mode, session_slot=slot, collected_at=collected_at,
                      request_path=str(request_path), package_path=str(package_path))
        current = now or now_jst()
        check_fresh(collected_at, current)
        generated_at = current.isoformat()
        folder = request_path.parent
        collection = read_json(request['data_path'])
        facts = read_json(request['facts_path'])
        analysis = read_json(contained(package['analysis_path'], job.work))
        ctx = RunContext(root=job.root, mode=mode, session_slot=slot, started_at=parse_time(collected_at),
                         work_dir=folder, history_dir=job.history,
                         previous_editions=request.get('previous_editions', []), generated_at=current)
        try:
            normalized = stages.validate_analysis(analysis, facts, ctx)
        except AnalysisError as error:
            record['analysis_problems'] = error.problems
            raise JobError('analysis_rejected', 'Parent analysis JSON was rejected') from None
        parts = stages.build_report(normalized, facts, collection, ctx)
        presentation.validate_quotes(parts.summary, parts.figures, parts.markdown)
        machine = machine_mod.build(parts.machine_core, parts.machine_extensions, mode=mode, session_slot=slot,
                                    collected_at=collected_at, generated_at=generated_at,
                                    as_of=facts.get('as_of'))
        validator = getattr(stages, 'validate_machine', None)
        if validator is not None:
            validator(machine, parts)  # schema + MD decision block == machine.json, else no render
        # A corrected package for the same request renders into <work name>-r2, -r3 ... (earlier output is kept).
        base = job.reports / mode / 'editions'
        ensure_dir(base)
        target, revision = base / package_path.parent.name, 1
        while target.exists():
            revision += 1
            target = base / f'{package_path.parent.name}-r{revision}'
        target.mkdir(mode=DIR_MODE)
        stem = target / report_basename(mode, collected_at)
        paths = {suffix: stem.with_name(stem.name + suffix) for suffix in (
            '.md', '.data.json', '.facts.json', '.analysis.json', '.analysis-input.md', '.summary.json',
            '.figures.json', '.machine.json', '.bundle.json')}
        write_text(paths['.md'], parts.markdown)
        write_bytes(paths['.data.json'], Path(request['data_path']).read_bytes())
        write_bytes(paths['.facts.json'], Path(request['facts_path']).read_bytes())
        write_json(paths['.analysis.json'], normalized)
        write_bytes(paths['.analysis-input.md'], Path(request['input_path']).read_bytes())
        write_json(paths['.summary.json'], parts.summary)
        write_json(paths['.figures.json'], parts.figures)
        write_json(paths['.machine.json'], machine)
        bundle_mod.build(kind=mode, session_slot=slot, as_of=collected_at, md_path=paths['.md'],
                         data_path=paths['.data.json'], facts_path=paths['.facts.json'],
                         summary_path=paths['.summary.json'], figures_path=paths['.figures.json'],
                         machine_path=paths['.machine.json'], bindings=parts.bindings,
                         required_figures=parts.required_figures, missing=parts.missing,
                         limitations=parts.limitations, bundle_path=paths['.bundle.json'])
        outputs = {'md_path': str(paths['.md']), 'json_path': str(paths['.machine.json']),
                   'summary_path': str(paths['.summary.json']), 'visuals_path': str(paths['.figures.json']),
                   'bundle_path': str(paths['.bundle.json']), 'facts_path': str(paths['.facts.json']),
                   'data_path': str(paths['.data.json']), 'analysis_path': str(paths['.analysis.json'])}
        record['outputs'] = outputs
        outputs.update(render(paths['.md'], summary_path=paths['.summary.json'], visuals_path=paths['.figures.json'],
                              as_of=collected_at, kind=mode, session_slot=slot, generated_at=generated_at))
        outputs['render_evidence_path'] = browser_check(outputs['html_path'], paths['.bundle.json'], now=current)
        record.update(status='awaiting_parent_review', generation_status='succeeded', generated_at=generated_at)
        edition_path = stem.with_name(stem.name + '.edition.json')
        record['edition_path'] = str(edition_path)
        write_json(edition_path, {**record, 'symbol': SYMBOL})
        record_status(job, record)
        print(json.dumps({'status': record['status'], 'edition_path': str(edition_path), 'outputs': outputs},
                         ensure_ascii=False))
        return 0
    except Exception as error:  # noqa: BLE001
        failure(job, record, error, 'parent_render_failed')
        out = {'status': record['status'], 'error_category': record['error_category']}
        if record.get('analysis_problems'):
            out['analysis_problems'] = record['analysis_problems']
        print(json.dumps(out, ensure_ascii=False))
        return 1


def carry_forward(job: Job, record: dict, mode: str) -> dict:
    """On adoption only: persist the edition's known news (R-05) and its incident openings/releases (R-04)."""
    from btc import carry, scoring
    outputs = record['outputs']
    facts = read_json(contained(outputs['facts_path'], job.reports))
    analysis = read_json(contained(outputs['analysis_path'], job.reports))
    if 'news_assessments' not in analysis:
        # Only stage stubs in tests lack it: the parent-analysis schema requires the key.
        return {'skipped': 'analysis_without_news_assessments'}
    if facts['state'].get('carry', {}).get('status') == 'invalid':
        # Never write over a carried file this edition could not read (R2-04).
        return {'skipped': 'carry_state_invalid', 'error': facts['state']['carry'].get('error')}
    try:
        carry.check(job.history)
    except carry.CarryStateError as error:
        return {'skipped': 'carry_state_invalid', 'error': error.code}
    ev = scoring.evaluate(facts, mode=mode, assessments=analysis['news_assessments'],
                          recoveries=analysis.get('incident_recoveries') or [])
    edition_id = Path(outputs['md_path']).parent.name
    added = carry.record_known(job.history, facts, analysis, edition_id=edition_id, mode=mode)
    incidents = carry.record_incidents(job.history, facts, analysis, ev, edition_id=edition_id)
    return {'known_news_added': added, 'incidents_opened': incidents['opened'],
            'incidents_released': incidents['released'],
            'incidents_open_after': [i['incident_id'] for i in carry.open_incidents(job.history)]}


def accept_review(job: Job, review_path, expected_mode: str | None, now: datetime | None = None) -> int:
    from btc.review import review_report
    record = {'mode': expected_mode or 'daily', 'ok': False, 'status': 'running', 'stage': 'parent_review',
              'publication_ready': False, 'publication_status': 'disabled'}
    try:
        review_path = contained(review_path, job.root)
        evidence = read_json(review_path)
        edition_path = contained(evidence['edition_path'], job.reports)
        edition = read_json(edition_path)
        mode = edition.get('mode')
        if edition.get('symbol') != SYMBOL or mode not in MODES or not edition_path.is_relative_to(job.reports / mode / 'editions'):
            raise JobError('edition_invalid', 'Review must refer to a BTCUSD edition of this job')
        if expected_mode is not None and mode != expected_mode:
            raise JobError('mode_mismatch', 'Review mode differs from the scheduled task')
        if edition.get('status') not in ('awaiting_parent_review', 'needs_attention'):
            raise JobError('edition_state', 'Edition is not awaiting parent review')
        record = edition
        record['stage'] = 'parent_review'
        outputs = record['outputs']
        path, accepted = review_report(outputs['html_path'], outputs['bundle_path'], outputs['render_evidence_path'],
                                       evidence_path=review_path, now=now)
        outputs['parent_review_path'] = path
        if accepted:
            record['carry'] = carry_forward(job, record, mode)
        record.update(ok=accepted, status='succeeded_local' if accepted else 'needs_attention',
                      review_status='parent_passed' if accepted else 'changes_requested',
                      independent_review_status='not_performed', publication_ready=False,
                      publication_status='disabled', historical_revision=False,
                      finished_at=now_jst().isoformat())
        if not accepted:
            record['error_category'] = 'changes_requested'
        write_json(edition_path, {**record, 'symbol': SYMBOL})
        record_status(job, record)
        print(json.dumps({'status': record['status'], 'parent_review_path': path, 'publication_ready': False}))
        return 0 if accepted else 2
    except Exception as error:  # noqa: BLE001
        failure(job, record, error, 'parent_review_failed')
        print(json.dumps({'status': record['status'], 'error_category': record['error_category']}))
        return 1
