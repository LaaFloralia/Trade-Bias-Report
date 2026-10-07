"""The BTCUSD analysis stages plugged into the generic workflow (btc.stages.Stages)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys

from btc import analysis, facts as facts_mod, report, scoring
from btc.common import JobError, parse_time
from btc.jsonschema_lite import validate as schema_validate
from btc.stages import ReportParts, RunContext

MACHINE_SCHEMA = Path(__file__).resolve().parent / 'schemas' / 'machine.schema.json'
FACTS_SCHEMA = Path(__file__).resolve().parent / 'schemas' / 'facts.schema.json'


def last_success(ctx: RunContext) -> datetime | None:
    """Collection start of this job's latest parent-passed edition of the same mode (news lookback, design 9)."""
    from btc.workflow import Job, previous_passed_editions
    found = previous_passed_editions(Job(root=ctx.root, python=Path(sys.executable)), ctx.mode, limit=1,
                                     before=ctx.started_at)
    return parse_time(found[0]['collected_at']) if found else None


def _schema(path: Path) -> dict:
    import json
    return json.loads(path.read_text(encoding='utf-8'))


class BtcStages:
    def collect_context(self, ctx: RunContext) -> dict:
        from btc.collect import collect_context
        return collect_context(ctx.mode, started_at=ctx.started_at, last_success=last_success(ctx))

    def finish_collection(self, context: dict, scores: dict | None, selection: dict, ctx: RunContext) -> dict:
        from btc.collect import finish_collection
        return finish_collection(context, scores, selection)

    def build_facts(self, collection: dict, ctx: RunContext) -> dict:
        built = facts_mod.build(collection, mode=ctx.mode, session_slot=ctx.session_slot,
                                collection_path=ctx.work_dir / 'collection.json', history_dir=ctx.history_dir)
        errors = schema_validate(built, _schema(FACTS_SCHEMA))
        if errors:
            raise JobError('facts_invalid', 'Facts violate btc/schemas/facts.schema.json: ' + '; '.join(errors[:5]))
        return built

    def analysis_input(self, collection: dict, facts: dict, ctx: RunContext) -> str:
        preview = scoring.evaluate(facts, mode=ctx.mode, assessments=None)
        return analysis.briefing(facts, preview, edition_id=ctx.work_dir.name, mode=ctx.mode,
                                 session_slot=ctx.session_slot, previous=ctx.previous_editions)

    def analysis_schema(self) -> dict:
        return analysis.schema()

    def validate_analysis(self, parent: dict, facts: dict, ctx: RunContext) -> dict:
        return analysis.validate(parent, facts, edition_id=ctx.work_dir.name)

    def build_report(self, validated: dict, facts: dict, collection: dict, ctx: RunContext) -> ReportParts:
        return report.build(validated, facts, collection, ctx)

    def validate_machine(self, machine: dict, parts: ReportParts) -> None:
        """machine.json schema (own validator, unknown keywords are errors) and MD decision block consistency."""
        errors = schema_validate(machine, _schema(MACHINE_SCHEMA))
        if errors:
            raise JobError('machine_invalid', 'machine.json violates btc/schemas/machine.schema.json: '
                           + '; '.join(errors[:5]))
        try:
            report.check_markdown(parts.markdown, machine)
        except report.ReportError as error:
            raise JobError('markdown_machine_mismatch', str(error)) from None
