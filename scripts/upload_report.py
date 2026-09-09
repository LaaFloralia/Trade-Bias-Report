"""Publish a reviewed report transactionally; CLI defaults to dry-run.

The legacy upload() wrapper still returns None on failure so analysis can finish,
but the CLI exits nonzero. No unaudited HTML can replace the published edition.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.report_release import BUCKET, SITE_BASE, ReleaseError, mode_path, release, recover


def object_path(mode):
    return mode_path(mode)


def storage_url(base, path):
    return f"{base}/storage/v1/object/public/{BUCKET}/{path}"


def reader_url(mode):
    mode_path(mode)
    return f"{SITE_BASE}/{mode}"


def upload(html_path, mode, *, publish=False, directory=None):
    html_path = Path(html_path)
    try:
        import json
        if json.loads(html_path.with_suffix('.bundle.json').read_text())['kind'] != mode:
            raise ReleaseError('Report kind mismatch')
        result = release(html_path, html_path.with_suffix('.bundle.json'),
                         html_path.with_suffix('.acceptance.json'),
                         directory or html_path.parent / 'publication', publish=publish)
        if result['kind'] != mode:
            raise ReleaseError('Report kind mismatch')
        if result['publication'] == 'succeeded':
            print(f"URL: {reader_url(mode)}")
            return reader_url(mode)
        print('[upload] Dry-run validated; publication not attempted')
    except Exception as error:
        # Do not expose arbitrary file contents, remote responses, or secrets.
        print(f'[upload] WARN: {type(error).__name__}; publication not confirmed')
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('html_path', type=Path)
    ap.add_argument('--mode', choices=['daily', 'weekly'], required=True)
    ap.add_argument('--publish', action='store_true', help='Write externally only after explicit publication approval')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--recover', action='store_true', help='Recover the previous edition from an interrupted publication journal')
    ap.add_argument('--state-dir', type=Path)
    args = ap.parse_args(argv)
    if args.publish and args.dry_run:
        ap.error('--publish and --dry-run are mutually exclusive')
    path = args.html_path.expanduser().absolute()
    try:
        import json
        if args.recover:
            if not args.publish or not args.state_dir:
                raise ReleaseError('Recovery requires --publish and --state-dir')
            print(json.dumps(recover(args.state_dir), ensure_ascii=False))
            return 0
        bundle = json.loads(path.with_suffix('.bundle.json').read_text())
        if bundle['kind'] != args.mode:
            raise ReleaseError('Report kind mismatch')
        result = release(path, path.with_suffix('.bundle.json'), path.with_suffix('.acceptance.json'),
                         args.state_dir or path.parent / 'publication', publish=args.publish)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        print(f'[upload] ERROR: {type(error).__name__}; publication not confirmed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
