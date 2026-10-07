"""Shared helpers: time, hashing, atomic private writes, path containment."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

JST = ZoneInfo('Asia/Tokyo')
UTC = timezone.utc
FILE_MODE = 0o600
DIR_MODE = 0o700


class JobError(RuntimeError):
    """An expected, labelled failure. The message is our own text, never external."""

    def __init__(self, category: str, message: str | None = None):
        super().__init__(message or category)
        self.category = category


def now_jst() -> datetime:
    return datetime.now(JST)


def now_utc() -> datetime:
    return datetime.now(UTC)


def parse_time(value) -> datetime:
    """Offset-aware ISO 8601 only; naive timestamps are rejected (P5)."""
    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        except ValueError:
            raise ValueError('invalid_timestamp') from None
    else:
        raise ValueError('invalid_timestamp')
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError('naive_timestamp')
    return stamp


def iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError('naive_timestamp')
    return value.isoformat()


def iso_jst(value) -> str:
    return parse_time(value).astimezone(JST).isoformat()


def iso_utc(value) -> str:
    return parse_time(value).astimezone(UTC).isoformat()


def digest(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def ensure_dir(path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    try:
        path.chmod(DIR_MODE)
    except PermissionError:
        pass
    return path


def write_bytes(path, data: bytes) -> Path:
    """Write via a private temporary file and os.replace (P1)."""
    path = Path(path)
    ensure_dir(path.parent)
    fd, name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, FILE_MODE)
        os.replace(name, path)
    except BaseException:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass
        raise
    return path


def write_text(path, text: str) -> Path:
    return write_bytes(path, text.encode('utf-8'))


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'


def write_json(path, value) -> Path:
    return write_text(path, dumps(value))


def contained(path, root) -> Path:
    """Absolute path inside root, without '..' and without symlinked components."""
    path, root = Path(path).absolute(), Path(root).absolute()
    if '..' in path.parts or not path.is_relative_to(root):
        raise JobError('path_outside_job', 'Input must be inside the BTCUSD job root')
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise JobError('symlink_input', 'Symlink input prohibited')
    return path
