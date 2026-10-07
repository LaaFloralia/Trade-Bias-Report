"""Helpers shared by source adapters."""
from __future__ import annotations

from datetime import datetime, timezone
import math

from btc.fetch import FetchError, Response, SourceResult

UTC = timezone.utc


def z(value: datetime) -> str:
    """UTC RFC 3339 with ``Z`` (machine/facts convention, P9-3)."""
    if value.tzinfo is None:
        raise ValueError('naive_timestamp')
    text = value.astimezone(UTC).isoformat()
    return text.replace('+00:00', 'Z')


def from_ms(value) -> str:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        raise FetchError('invalid_payload') from None
    if number <= 0:
        raise FetchError('invalid_payload')
    return z(datetime.fromtimestamp(number / 1000, UTC))


def from_s(value) -> str:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        raise FetchError('invalid_payload') from None
    if number <= 0:
        raise FetchError('invalid_payload')
    return z(datetime.fromtimestamp(number, UTC))


def num(value, *, allow_none: bool = False):
    """Finite float from a provider value; empty / N/A / NaN are invalid (or None)."""
    if value is None or (isinstance(value, str) and value.strip() in ('', '-', 'N/A', 'n/a', '.')):
        if allow_none:
            return None
        raise FetchError('missing_field')
    if isinstance(value, bool):
        raise FetchError('invalid_payload')
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise FetchError('invalid_payload') from None
    if not math.isfinite(result):
        raise FetchError('invalid_payload')
    return result


def ok(source_id: str, responses, values: dict, *, observed_at: str | None = None,
       published_at: str | None = None, status: str = 'ok', notes=(), timestamp_quality: str = 'source_observed') -> SourceResult:
    """Normalised result. ``responses`` is a list or a ``{part: Response}`` dict.

    ``values['raw_parts']`` maps each part to its own body hash so that every
    observed fact can cite the one response it came from (design 5.1).
    """
    if isinstance(responses, dict):
        parts = {name: r for name, r in responses.items() if r is not None}
    else:
        parts = {f'r{i}': r for i, r in enumerate(responses) if r is not None}
    responses = list(parts.values())
    last = max(responses, key=lambda r: r.retrieved_at)
    values = dict(values)
    values.setdefault('raw_parts', {name: r.raw_sha256 for name, r in parts.items()})
    values.setdefault('timestamp_quality', timestamp_quality)
    starts = [r.started_at for r in responses if r.started_at]
    values.setdefault('retrieval_started_at', z(datetime.fromisoformat(min(starts))) if starts else None)
    return SourceResult(source_id=source_id, status=status, retrieved_at=z(datetime.fromisoformat(last.retrieved_at)),
                        observed_at=observed_at, published_at=published_at, source_url=responses[0].url,
                        values=values, raw_sha256=[r.raw_sha256 for r in responses],
                        attempts=sum(r.attempts for r in responses),
                        elapsed_ms=round(sum(r.elapsed_ms for r in responses), 1), notes=list(notes))


def json_of(response: Response):
    data = response.json()
    if data in (None, {}, []):
        raise FetchError('empty_payload')
    return data
