"""machine.json core contract shared with XAU (P1).

The six fields keep XAU's types and meaning by reusing
``scripts.intel.validate_intel_json`` / ``normalize_intel_json`` (read only;
XAU behaviour is unchanged). BTC extensions live in other keys and may never
replace a core or pipeline key; the full document is checked against
btc/schemas/machine.schema.json by the pipeline (BtcStages.validate_machine).

Times (P9-3): ``as_of`` (collection completed), ``collected_at`` (collection
start) and ``generated_at`` are UTC with ``Z``; ``data_as_of`` is the JST date
of the collection (XAU meaning).
"""
from __future__ import annotations

from datetime import timezone

from btc import SYMBOL
from btc.common import JST, parse_time

CORE_KEYS = ('bias', 'no_trade', 'no_trade_reason', 'risk_events_next_24h', 'positioning_summary', 'confidence')
PIPELINE_KEYS = ('symbol', 'mode', 'session_slot', 'as_of', 'data_as_of', 'collected_at', 'generated_at',
                 'historical_revision')


class MachineError(ValueError):
    pass


def _intel():
    from scripts import intel  # existing module; imported, not modified
    return intel


def zulu(value) -> str:
    return parse_time(value).astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def build(core: dict, extensions: dict | None, *, mode: str, session_slot: str, collected_at: str,
          generated_at: str, as_of: str | None = None) -> dict:
    intel = _intel()
    errors = intel.validate_intel_json(core)
    if errors:
        raise MachineError('Parent machine JSON violates the six-field contract: ' + '; '.join(errors))
    extensions = dict(extensions or {})
    clash = (set(CORE_KEYS) | set(PIPELINE_KEYS)) & set(extensions)
    if clash:
        raise MachineError('BTC extensions must not replace core or pipeline keys')
    collected = parse_time(collected_at)
    machine = intel.normalize_intel_json(core)
    machine = intel.attach_pipeline_metadata(machine, collected.astimezone(JST).date().isoformat(), zulu(generated_at))
    machine.update(symbol=SYMBOL, mode=mode, session_slot=session_slot, as_of=zulu(as_of or collected_at),
                   collected_at=zulu(collected), historical_revision=False)
    machine.update(extensions)
    return machine
