"""Accept an explicit parent review; never launches an AI or grants publication.

Same discipline as XAU chart-intel/report_review.review_report (P1):
reviewer "parent-ciel", independentProcess false, every CHECKS criterion with
a boolean and concrete evidence, imagesReviewed bound to the render record by
sha256 (both overviews, every figure, PC and mobile source samples), and the
review bound to the current bytes of HTML, bundle, render record, machine.json
and the BTC fact file. Passing also requires ``issues == []``.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from btc.acceptance import CHECKS, AcceptanceError, validate_edition
from btc.common import digest, read_json, write_json

HASH_KEYS = ('htmlSha256', 'bundleSha256', 'renderSha256', 'machineSha256', 'factsSha256')


class ReviewError(ValueError):
    pass


def bound_hashes(html_path, bundle_path, render_path) -> dict:
    bundle = read_json(bundle_path)
    return {'htmlSha256': digest(html_path), 'bundleSha256': digest(bundle_path),
            'renderSha256': digest(render_path), 'machineSha256': digest(bundle['machinePath']),
            'factsSha256': digest(bundle['factsPath'])}


def review_report(html_path, bundle_path, render_path, *, evidence_path, now=None) -> tuple[str, bool]:
    html_path, bundle_path, render_path = map(Path, (html_path, bundle_path, render_path))
    try:
        bundle = validate_edition(html_path, bundle_path, now)
    except AcceptanceError as error:
        raise ReviewError(str(error)) from None
    if bundle.get('machineSha256') != digest(bundle['machinePath']):
        raise ReviewError('machine.json changed after bundle creation')
    evidence = read_json(evidence_path)
    render = read_json(render_path)
    hashes = bound_hashes(html_path, bundle_path, render_path)
    if any(evidence.get(key) != value for key, value in hashes.items()):
        raise ReviewError('Parent review bound to different bytes')
    if bundle.get('semanticAuditRequired') is True:
        audit_path = html_path.with_suffix('.semantic-audit.json')
        audit_hash = digest(audit_path)
        ack = evidence.get('semanticAudit') or {}
        if (audit_hash != bundle.get('semanticAuditSha256') or ack.get('sha256') != audit_hash
                or ack.get('reviewed') is not True or not isinstance(ack.get('notes'), str) or not ack['notes'].strip()):
            raise ReviewError('Parent must inspect and acknowledge the current semantic audit')
        if any(pair.get('result') == 'deterministic_error' for pair in read_json(audit_path).get('pairs', [])):
            raise ReviewError('Unmatched source quotes cannot be accepted')
    if evidence.get('independentProcess') is not False or evidence.get('reviewer') != 'parent-ciel':
        raise ReviewError('Parent review cannot claim independent acceptance')
    if not isinstance(evidence.get('reviewedAt'), str) or not evidence['reviewedAt']:
        raise ReviewError('reviewedAt is required')
    if render.get('status') != 'passed' or render.get('htmlSha256') != hashes['htmlSha256'] \
            or render.get('bundleSha256') != hashes['bundleSha256']:
        raise ReviewError('Browser evidence mismatch')
    if {v.get('label') for v in render.get('viewports', [])} != {'desktop', 'wide', 'mobile'}:
        raise ReviewError('Desktop/wide/mobile render evidence is missing')
    images = {x['path']: x for x in render.get('images', [])}
    for item in images.values():
        if digest(item['path']) != item['sha256']:
            raise ReviewError('Render image changed')
    reviewed = evidence.get('imagesReviewed')
    if not isinstance(reviewed, list) or not reviewed or len(reviewed) != len({x.get('path') for x in reviewed}):
        raise ReviewError('Unique inspected images required')
    for item in reviewed:
        if item.get('path') not in images or item.get('sha256') != images[item['path']]['sha256']:
            raise ReviewError('Inspected image not bound to render')
    names = {Path(x['path']).name for x in reviewed}
    required = {Path(p).name for p in images if 'overview' in Path(p).name or '-figure-' in Path(p).name}
    if (not required <= names or not any(n.startswith('desktop-source-') for n in names)
            or not any(n.startswith('mobile-source-') for n in names)):
        raise ReviewError('All overviews, every figure and PC/mobile source samples required')
    checks = evidence.get('checks')
    if (not isinstance(checks, dict) or set(checks) != set(CHECKS)
            or any(not isinstance(v, dict) or not isinstance(v.get('evidence'), str) or not v['evidence'].strip()
                   or type(v.get('passed')) is not bool for v in checks.values())):
        raise ReviewError('Every criterion needs concrete evidence and result')
    if evidence.get('status') not in ('parent_passed', 'changes_requested'):
        raise ReviewError('Review status must be parent_passed or changes_requested')
    if not isinstance(evidence.get('issues'), list):
        raise ReviewError('issues must be a list')
    passed = (evidence['status'] == 'parent_passed' and all(v['passed'] for v in checks.values())
              and evidence['issues'] == [])
    result = {**evidence, **hashes, 'status': 'parent_passed' if passed else 'changes_requested',
              'independentProcess': False, 'independentReview': 'not_performed', 'publicationReady': False,
              'recordedAt': datetime.now().astimezone().isoformat()}
    target = html_path.with_suffix('.parent-review.json')
    write_json(target, result)
    return str(target), passed
