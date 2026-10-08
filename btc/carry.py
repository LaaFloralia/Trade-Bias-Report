"""State carried between adopted BTC editions: known news (R-05) and unresolved incidents (R-04).

Both live in the job's ``history/`` (writable by the worker) and are written only
when the parent accepts an edition (``workflow.accept_review``):

- ``btc-known-news.json``: every news item the parent assessed in an adopted
  edition, with its identifiers (canonical URL, code event cluster, primary body
  hash) and ``first_known_at``. Records are kept for matching for 180 days (at most
  5000, oldest dropped), independent of the scoring window: a re-report never
  extends the scoring period (design 7.2 carry_forward), however late it comes
  (R2-02). An adopted follow-up with new primary facts is its own record
  (``follow_up_of``) whose period starts at its own publication (R2-03).
- ``btc-incidents.json``: critical incidents (design 7.4 incident_hold). An
  incident stays open across editions, missing articles and restarts until an
  adopted edition records an official recovery tied to the same incident plus
  observations of every affected source made after the recovery notice
  (``analysis.incident_recoveries``; R2-01), or the owner releases it manually.

Both files are validated strictly on every read (R2-04). An invalid file raises
``CarryStateError``: facts turn it into the hard invalid ``carry_state_invalid``
(data_hold), acceptance skips every carry write, the CLI refuses, and the file is
never overwritten.

Owner-only operations (outside the sandbox, never by the parent)::

    python -B -m btc.carry list --root <job root>
    python -B -m btc.carry release --root <job root> --incident <incident_id> --reason <text>

``release`` marks an open incident ``released_manually`` (reason, ``released_at``,
``released_by: owner_instruction``) under the same lock; records are never deleted.

Facts read both files (``facts.build``); scoring decides; nothing here scores.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

from btc.common import UTC, parse_time

SCHEMA_VERSION = 1
KNOWN_FILE = 'btc-known-news.json'
INCIDENT_FILE = 'btc-incidents.json'
KNOWN_RETENTION_DAYS = 180
KNOWN_MAX_RECORDS = 5000
BRIEFING_DAYS = 14
SCORABLE = ('ok', 'provisional')
INCIDENT_STATUSES = ('open', 'released', 'released_manually')
OPTIONAL_STR = (str, type(None))
KNOWN_FIELDS = {'known_id': str, 'ids': list, 'first_known_at': 'time', 'adopted_as_of': 'time', 'edition_id': str,
                'mode': str, 'title': str, 'news_ids': list, 'body_hashes': list}
INCIDENT_FIELDS = {'incident_id': str, 'news_id': str, 'event_cluster_id': OPTIONAL_STR, 'url': OPTIONAL_STR,
                   'title': str, 'published_at': 'time', 'affected_source_ids': list, 'status': str,
                   'recorded_edition_id': str, 'recorded_as_of': 'time'}


class CarryStateError(ValueError):
    """A carried-state file is not exactly what this code wrote. ``code`` is a fixed error code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@contextmanager
def _locked(directory: Path):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / '.carry.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _valid_record(record, fields: dict) -> bool:
    if not isinstance(record, dict):
        return False
    for key, kind in fields.items():
        if key not in record:
            return False
        value = record[key]
        if kind == 'time':
            if not isinstance(value, str):
                return False
            try:
                parse_time(value)
            except (TypeError, ValueError):
                return False
        elif not isinstance(value, kind):
            return False
    return True


def _read(path: Path) -> list[dict]:
    """Strict read (R2-04): schema_version, record keys and types, enumerations. Raises CarryStateError."""
    if not path.is_file():
        return []
    kind = 'incidents' if path.name == INCIDENT_FILE else 'known_news'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CarryStateError(f'{kind}_json_invalid') from None
    if not isinstance(data, dict) or set(data) != {'schema_version', 'records'} or not isinstance(data['records'], list):
        raise CarryStateError(f'{kind}_structure_invalid')
    if data['schema_version'] != SCHEMA_VERSION:
        raise CarryStateError(f'{kind}_schema_version_unknown')
    for record in data['records']:
        if kind == 'incidents':
            if not _valid_record(record, INCIDENT_FIELDS) or record['status'] not in INCIDENT_STATUSES:
                raise CarryStateError('incidents_record_invalid')
        elif not _valid_record(record, KNOWN_FIELDS) or not isinstance(record.get('follow_up_of'), OPTIONAL_STR) \
                or not all(isinstance(x, str) for x in record['ids'] + record['body_hashes'] + record['news_ids']):
            raise CarryStateError('known_news_record_invalid')
    ids = [r['incident_id' if kind == 'incidents' else 'known_id'] for r in data['records']]
    if len(ids) != len(set(ids)):
        raise CarryStateError(f'{kind}_duplicate_id')
    return data['records']


def _write(path: Path, records: list[dict]) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump({'schema_version': SCHEMA_VERSION, 'records': records}, stream, ensure_ascii=False, indent=1)
            stream.write('\n')
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)  # never leave the temporary file behind
        raise


def check(history_dir) -> None:
    """Validate both files; raises CarryStateError."""
    _read(Path(history_dir) / KNOWN_FILE)
    _read(Path(history_dir) / INCIDENT_FILE)


def item_identifiers(item: dict) -> list[str]:
    """Code-side identifiers of one news item: canonical URL, code event cluster, primary body hash."""
    ids = [f'url:{item["url"]}'] if item.get('url') else []
    if item.get('event_cluster_id'):
        ids.append(f'cluster:{item["event_cluster_id"]}')
    sha = (item.get('body') or {}).get('text_sha256')
    if sha:
        ids.append(f'body:{sha}')
    return ids


def _new_known_id(identifiers: list[str], parent: str | None) -> str:
    return 'k-' + hashlib.sha256(json.dumps([sorted(identifiers), parent]).encode()).hexdigest()[:12]


def best_match(identifiers, links, known: list[dict]) -> dict | None:
    """The known record an item belongs to (R2-03): a URL or body-hash match beats a cluster or parent link;
    ties go to the latest ``first_known_at`` (an adopted follow-up over its original event)."""
    ids, links = set(identifiers), set(links or [])
    strong = [k for k in known if any(x.startswith(('url:', 'body:')) for x in ids & set(k['ids']))]
    weak = [k for k in known if k not in strong and (ids & set(k['ids']) or k['known_id'] in links)]
    for group in (strong, weak):
        if group:
            return max(group, key=lambda k: parse_time(k['first_known_at']))
    return None


def is_follow_up(item: dict, assessment: dict, match: dict | None) -> bool:
    """Parent flag + a primary body unknown to the matched record + published after it became known."""
    sha = (item.get('body') or {}).get('text_sha256')
    return bool(match is not None and assessment.get('follow_up_new_facts') and sha
                and sha not in match.get('body_hashes', [])
                and parse_time(item['published_at']) > parse_time(match['first_known_at']))


# --------------------------------------------------------------------- read

def known_news(history_dir, as_of: datetime) -> list[dict]:
    """Every retained known record adopted at or before ``as_of`` (matching and link validation; R2-02).
    The scoring window is applied by scoring from ``first_known_at``, not here."""
    as_of = as_of.astimezone(UTC)
    return [r for r in _read(Path(history_dir) / KNOWN_FILE) if parse_time(r['adopted_as_of']) <= as_of]


def open_incidents(history_dir) -> list[dict]:
    return [r for r in _read(Path(history_dir) / INCIDENT_FILE) if r['status'] == 'open']


# ---------------------------------------------------------- record (accept)

def record_known(history_dir, facts: dict, analysis: dict, *, edition_id: str, mode: str) -> int:
    """Add every assessed item of an adopted edition to the known set.

    A re-report is merged into its matched record (period unchanged). An accepted follow-up becomes its own
    record with ``follow_up_of`` and its own ``first_known_at`` (R2-03).
    """
    items = {n['id']: n for n in facts['state'].get('news', {}).get('items', [])}
    as_of = facts['as_of']
    path = Path(history_dir) / KNOWN_FILE
    added = 0
    with _locked(Path(history_dir)):
        records = _read(path)
        for a in analysis.get('news_assessments', []):
            item = items.get(a['news_id'])
            if item is None:
                continue
            ids = item_identifiers(item)
            hashes = [x[5:] for x in ids if x.startswith('body:')]
            match = best_match(ids, a.get('known_event_ids'), records)
            published = parse_time(item['published_at']).astimezone(UTC)
            if match is None or is_follow_up(item, a, match):
                parent = match['known_id'] if match is not None else None
                records.append({'known_id': _new_known_id(ids, parent), 'ids': ids, 'first_known_at': published.isoformat(),
                                'adopted_as_of': as_of, 'edition_id': edition_id, 'mode': mode,
                                'title': item['title'][:200], 'news_ids': [item['id']], 'body_hashes': hashes,
                                'follow_up_of': parent})
                added += 1
                continue
            match['ids'] = list(dict.fromkeys(match['ids'] + ids))
            match['body_hashes'] = list(dict.fromkeys(match['body_hashes'] + hashes))
            match['news_ids'] = list(dict.fromkeys(match['news_ids'] + [item['id']]))[-50:]
            if published < parse_time(match['first_known_at']):
                match['first_known_at'] = published.isoformat()
        cutoff = parse_time(as_of) - timedelta(days=KNOWN_RETENTION_DAYS)
        records = [r for r in records if parse_time(r['first_known_at']) >= cutoff]
        records.sort(key=lambda r: parse_time(r['first_known_at']))
        records = records[-KNOWN_MAX_RECORDS:]
        _write(path, records)
    return added


def record_incidents(history_dir, facts: dict, analysis: dict, evaluation: dict, *, edition_id: str) -> dict:
    """Persist new critical incidents and the releases this adopted edition validated."""
    path = Path(history_dir) / INCIDENT_FILE
    new = evaluation.get('incidents_new', [])
    released = {r['incident_id']: r for r in evaluation.get('incidents_released', [])}
    with _locked(Path(history_dir)):
        records = _read(path)
        known = {r['incident_id'] for r in records}
        for incident in new:
            if incident['incident_id'] not in known:
                records.append(dict(incident, status='open', recorded_edition_id=edition_id,
                                    recorded_as_of=facts['as_of']))
        for record in records:
            if record['status'] == 'open' and record['incident_id'] in released:
                record.update(status='released', released_edition_id=edition_id, released_as_of=facts['as_of'],
                              recovery=released[record['incident_id']])
        _write(path, records)
    return {'opened': [i['incident_id'] for i in new if i['incident_id'] not in known], 'released': sorted(released)}


# ------------------------------------------------------------------ checks

def scorable(fact: dict | None) -> bool:
    return bool(fact) and fact['status'] in SCORABLE and not fact.get('stale') and fact.get('value') is not None


def _observed_after(fact: dict | None, notice, index: dict, seen=None) -> bool:
    """An exchange observation strictly after the recovery notice (R2-01). Derived facts: every input must pass.
    Date-only facts (``source_date`` or no ``observed_at``) never count."""
    if not fact:
        return False
    seen = seen or set()
    if fact['fact_id'] in seen:
        return False
    seen = seen | {fact['fact_id']}
    if fact['source_id'] == 'computed':
        sources = fact.get('source_fact_ids') or []
        return bool(sources) and all(_observed_after(index.get(s), notice, index, seen) for s in sources)
    if fact.get('timestamp_quality') == 'source_date' or not fact.get('observed_at'):
        return False
    return parse_time(fact['observed_at']) > notice


def _observed_sources(fact: dict, index: dict) -> set:
    if fact['source_id'] != 'computed':
        return {fact['source_id']}
    out = set()
    for s in fact.get('source_fact_ids') or []:
        if s in index:
            out |= _observed_sources(index[s], index)
    return out


def recovery_problems(incident: dict, recovery: dict, facts: dict) -> list[str]:
    """Release needs an official (primary body) recovery notice after the incident and, for every affected
    source, a fresh observation made after that notice. Without named sources the parent cannot release."""
    problems = []
    items = {n['id']: n for n in facts['state'].get('news', {}).get('items', [])}
    index = {f['fact_id']: f for f in facts['facts']}
    affected = incident.get('affected_source_ids') or []
    if not affected:
        problems.append('recovery_affected_sources_unspecified')
    item = items.get(recovery.get('news_id'))
    notice = None
    if item is None:
        problems.append('recovery_news_unknown')
    else:
        notice = parse_time(item['published_at'])
        if item['code_verification'] != 'primary_body_retrieved':
            problems.append('recovery_not_official_body')
        if notice <= parse_time(incident['published_at']):
            problems.append('recovery_not_after_incident')
        if item.get('url') == incident.get('url'):
            problems.append('recovery_is_incident_article')
    fact_ids = recovery.get('fact_ids') or []
    if not fact_ids:
        problems.append('recovery_fresh_data_missing')
    facts_used = [index.get(f) for f in fact_ids]
    if any(not scorable(f) for f in facts_used):
        problems.append('recovery_data_not_fresh')
    if notice is not None and any(not _observed_after(f, notice, index) for f in facts_used):
        problems.append('recovery_data_not_observed_after_notice')
    covered = set()
    for f in facts_used:
        if scorable(f) and notice is not None and _observed_after(f, notice, index):
            covered |= _observed_sources(f, index)
    missing = [s for s in affected if s not in covered]
    if missing:
        problems.append('recovery_affected_source_not_fresh:' + ','.join(missing))
    return problems


# ------------------------------------------------------------ owner operations

MAX_REASON = 500


def release_manually(history_dir, incident_id: str, reason: str, *, now: datetime | None = None) -> dict:
    """Owner instruction only: close one open incident without a code-verified recovery. Never deletes."""
    reason = ' '.join((reason or '').split())
    if not reason or len(reason) > MAX_REASON or re.search(r'[\x00-\x1f\x7f]', reason):
        raise ValueError('reason_invalid')
    path = Path(history_dir) / INCIDENT_FILE
    if not path.is_file():
        raise ValueError('incident_unknown')
    with _locked(Path(history_dir)):
        records = _read(path)
        record = next((r for r in records if r['incident_id'] == incident_id), None)
        if record is None:
            raise ValueError('incident_unknown')
        if record['status'] != 'open':
            raise ValueError(f'incident_not_open:{record["status"]}')
        record.update(status='released_manually', release_reason=reason,
                      released_at=(now or datetime.now(UTC)).astimezone(UTC).isoformat(),
                      released_by='owner_instruction')
        _write(path, records)
    return dict(record)


LIST_FIELDS = ('incident_id', 'published_at', 'title', 'affected_source_ids', 'recorded_edition_id', 'recorded_as_of')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='python -B -m btc.carry', description='BTCUSD carried state (operations).')
    sub = parser.add_subparsers(dest='command', required=True)
    lst = sub.add_parser('list', help='read-only: print the open incidents as JSON')
    lst.add_argument('--root', required=True, help='job root (contains history/)')
    rel = sub.add_parser('release', help='owner instruction only: release one open incident manually')
    rel.add_argument('--root', required=True, help='job root (contains history/)')
    rel.add_argument('--incident', required=True)
    rel.add_argument('--reason', required=True)
    args = parser.parse_args(argv)
    history = Path(args.root).expanduser() / 'history'
    if not history.is_dir():
        print(json.dumps({'status': 'refused', 'error': 'history_missing'}))
        return 2
    try:
        check(history)
        if args.command == 'list':
            incidents = [{k: r.get(k) for k in LIST_FIELDS} for r in open_incidents(history)]
            print(json.dumps({'status': 'ok', 'open_incidents': incidents}, ensure_ascii=False, indent=1))
            return 0
        record = release_manually(history, args.incident, args.reason)
    except CarryStateError as error:
        print(json.dumps({'status': 'invalid', 'error': error.code}))
        return 2
    except ValueError as error:
        print(json.dumps({'status': 'refused', 'error': str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps({'status': 'released_manually', 'incident_id': record['incident_id'],
                      'released_at': record['released_at']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
