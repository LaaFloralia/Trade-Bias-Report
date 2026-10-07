"""Edition manifest (bundle.json) binding source, summary, figures and facts.

Same idea as XAU scripts/report_bundle.py, but figure numbers are bound to
fact IDs in the edition's facts file (computed by code), never to free text:
a figure item's ``value`` equals the fact's ``display_value`` (the number as
printed in ``display``) and its ``display`` equals the fact's ``display``.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import math
from pathlib import Path

from btc import SYMBOL
from btc.common import JST, digest, parse_time, read_json, write_json

MAX_DATA_AGE = timedelta(hours=6)
FACT_KEYS = ('fact_id', 'value', 'unit', 'display', 'display_value', 'observed_at', 'retrieved_at', 'source_id',
             'stale', 'status')
FACT_STATUSES = ('ok', 'provisional', 'partial', 'stale', 'missing', 'invalid', 'conflict', 'warming_up',
                 'not_applicable', 'terms_restricted')
USABLE = ('ok', 'provisional', 'partial', 'stale')


class BundleError(ValueError):
    pass


def require_fresh(as_of: str, now: datetime | None = None, max_age: timedelta = MAX_DATA_AGE) -> datetime:
    try:
        stamp = parse_time(as_of).astimezone(JST)
    except ValueError:
        raise BundleError('Missing or invalid collection timestamp') from None
    current = (now or datetime.now(JST)).astimezone(JST)
    if stamp > current or current - stamp > max_age:
        raise BundleError('Data is stale or future-dated; maximum age is six hours')
    return stamp


def validate_facts(facts: dict) -> dict:
    """Fact-file shape (full field contract: btc/schemas/facts.schema.json, checked when facts are built)."""
    if not isinstance(facts, dict) or facts.get('symbol') != SYMBOL or not isinstance(facts.get('facts'), list):
        raise BundleError('Facts file must be a BTCUSD fact object')
    index = {}
    for fact in facts['facts']:
        if not isinstance(fact, dict) or any(key not in fact for key in FACT_KEYS):
            raise BundleError('Fact record is missing a required key')
        if not isinstance(fact['fact_id'], str) or not fact['fact_id'] or fact['fact_id'] in index:
            raise BundleError('Fact IDs must be unique nonempty strings')
        if fact['status'] not in FACT_STATUSES or not isinstance(fact['stale'], bool):
            raise BundleError('Fact status or stale flag is invalid')
        for key in ('value', 'display_value'):
            value = fact[key]
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value)):
                raise BundleError('Fact values must be finite numbers or null')
        if fact['status'] in USABLE and (fact['value'] is None or fact['display_value'] is None):
            raise BundleError('A usable fact must carry a value')
        if fact['value'] is None and fact['display_value'] is not None:
            raise BundleError('A missing value cannot have a display value')
        for key in ('observed_at', 'retrieved_at'):
            if fact[key] is not None:
                try:
                    parse_time(fact[key])
                except ValueError:
                    raise BundleError('Fact timestamps must be offset-aware ISO 8601') from None
        index[fact['fact_id']] = fact
    return index


def check_bindings(facts_index: dict, bindings: list, figures: list) -> None:
    """Every numeric figure item is bound to one fact with the same value and display."""
    items = {}
    for figure in figures:
        for item in figure.get('items', []):
            if 'value' in item:
                key = (figure.get('id'), item.get('label'))
                if key in items:
                    raise BundleError('Duplicate figure item label')
                items[key] = item
    bound = {}
    for binding in bindings:
        key = (binding.get('figure'), binding.get('label'))
        if key in bound:
            raise BundleError('Duplicate figure binding')
        bound[key] = binding
    if set(items) != set(bound):
        raise BundleError('Figure bindings are missing or unexpected')
    for key, binding in bound.items():
        fact = facts_index.get(binding.get('fact_id'))
        if fact is None:
            raise BundleError('Figure binding refers to an unknown fact')
        if fact['status'] not in USABLE or fact['display_value'] is None:
            raise BundleError('Figure binding refers to an unusable fact')
        item = items[key]
        if item['value'] != fact['display_value'] or item.get('display') != fact['display']:
            raise BundleError('Figure number does not match its fact')
        if binding.get('value') != fact['display_value'] or binding.get('display') != fact['display']:
            raise BundleError('Binding record differs from its fact')


def build(*, kind: str, session_slot: str, as_of: str, md_path, data_path, facts_path, summary_path,
          figures_path, machine_path, bindings: list, required_figures: list, missing: list,
          limitations: list, bundle_path) -> dict:
    figures = read_json(figures_path)
    facts_index = validate_facts(read_json(facts_path))
    check_bindings(facts_index, bindings, figures)
    figure_ids = [f['id'] for f in figures]
    manifest = {
        'schemaVersion': 1, 'symbol': SYMBOL, 'kind': kind, 'sessionSlot': session_slot,
        'asOf': as_of, 'reportDate': parse_time(as_of).astimezone(JST).date().isoformat(),
        'dataPath': str(Path(data_path).absolute()), 'dataSha256': digest(data_path),
        'sourcePath': str(Path(md_path).absolute()), 'sourceSha256': digest(md_path),
        'factsPath': str(Path(facts_path).absolute()), 'factsSha256': digest(facts_path),
        'summaryPath': str(Path(summary_path).absolute()), 'summarySha256': digest(summary_path),
        'figuresPath': str(Path(figures_path).absolute()), 'figuresSha256': digest(figures_path),
        'machinePath': str(Path(machine_path).absolute()), 'machineSha256': digest(machine_path),
        'missing': list(missing), 'limitations': list(limitations), 'bindings': bindings,
        'requiredFigures': list(required_figures), 'figureIds': figure_ids,
        'numericCheck': 'passed', 'semanticReview': 'pending', 'publicationReady': False,
        'historicalRevision': False,
    }
    write_json(bundle_path, manifest)
    return manifest


def validate(bundle_path, now: datetime | None = None) -> dict:
    """Recheck every bound input against the manifest."""
    b = read_json(bundle_path)
    if b.get('symbol') != SYMBOL or b.get('schemaVersion') != 1:
        raise BundleError('Bundle is not a BTCUSD edition manifest')
    for key in ('data', 'source', 'facts', 'summary', 'figures', 'machine'):
        if digest(b[f'{key}Path']) != b[f'{key}Sha256']:
            raise BundleError(f'{key} input changed after bundle creation')
    if require_fresh(b['asOf'], now).isoformat() != parse_time(b['asOf']).astimezone(JST).isoformat():
        raise BundleError('Bundle timestamp mismatch')
    missing_required = set(b['requiredFigures']) - set(b['figureIds'])
    if missing_required:
        raise BundleError('Required figures are missing')
    figures = read_json(b['figuresPath'])
    check_bindings(validate_facts(read_json(b['factsPath'])), b['bindings'], figures)
    return b
