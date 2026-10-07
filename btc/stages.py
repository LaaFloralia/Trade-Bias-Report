"""Plug-in boundary between the generic workflow and the BTC analysis content.

The workflow (btc/workflow.py) owns states, folders, hashes, locking, render
and review. Everything that depends on the design (sources, fact IDs,
scoring, analysis JSON fields, Markdown headings, figures) is supplied by an
object implementing ``Stages``. ``get_stages()`` returns the configured
implementation: ``btc.pipeline.BtcStages``. ``BTC_STAGES=pending`` selects
``PendingStages`` (refuses to run; used by entry-chain tests so that no
network is contacted).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import os
from pathlib import Path
from typing import Protocol

from btc.common import JobError


@dataclass
class RunContext:
    root: Path
    mode: str  # daily | weekly
    session_slot: str  # am | pm | weekly (P8)
    started_at: datetime  # JST, collection start for prepare
    work_dir: Path
    history_dir: Path
    previous_editions: list = field(default_factory=list)  # parent-passed editions of this job only
    generated_at: datetime | None = None  # render time (finalize); set by the workflow before build_report


@dataclass
class ReportParts:
    markdown: str  # complete MD (front matter block first, fixed headings)
    summary: dict  # renderer summary; every item quotes ``markdown`` exactly
    figures: list  # renderer figures; numeric items bound below
    bindings: list  # [{'figure', 'label', 'fact_id', 'value', 'display'}]
    machine_core: dict  # the six XAU-compatible fields
    machine_extensions: dict = field(default_factory=dict)
    required_figures: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    limitations: list = field(default_factory=list)


class AnalysisError(ValueError):
    """Parent analysis rejected; ``problems`` are our own fixed descriptions."""

    def __init__(self, problems: list[str]):
        super().__init__('; '.join(problems) or 'analysis rejected')
        self.problems = list(problems)


class Stages(Protocol):
    def collect_context(self, ctx: RunContext) -> dict:
        """Context phase (the collect worker, the only process holding the FRED key).

        Returns a JSON-safe dict with ``collection_started_at``, ``phases`` and
        normalised ``sources`` records; the news record carries headline
        candidates for the entry's triage.
        """

    def finish_collection(self, context: dict, scores: dict | None, selection: dict, ctx: RunContext) -> dict:
        """News detail, quotes and books (sandboxed worker without secrets).

        Returns the full collection: ``symbol``, ``collection_started_at``,
        ``collection_completed_at`` and normalised ``sources`` records only.
        """

    def build_facts(self, collection: dict, ctx: RunContext) -> dict:
        """Deterministic facts (no network). Returns {'symbol','as_of','facts':[...]}."""

    def analysis_input(self, collection: dict, facts: dict, ctx: RunContext) -> str:
        """Markdown the parent reads before writing the analysis JSON."""

    def analysis_schema(self) -> dict:
        """JSON Schema of the parent-written analysis JSON."""

    def validate_analysis(self, analysis: dict, facts: dict, ctx: RunContext) -> dict:
        """Reject (AnalysisError) or return the normalised analysis."""

    def build_report(self, analysis: dict, facts: dict, collection: dict, ctx: RunContext) -> ReportParts:
        """Markdown, summary, figures, bindings and machine fields."""


class PendingStages:
    """Phase A placeholder: the workflow runs end to end only with a real implementation."""

    def _pending(self, *args, **kwargs):
        raise JobError('pipeline_not_configured', 'BTC analysis stages are not implemented yet')

    collect_context = finish_collection = build_facts = analysis_input = analysis_schema = validate_analysis = build_report = _pending


def get_stages() -> Stages:
    if os.environ.get('BTC_STAGES') == 'pending':
        return PendingStages()
    from btc.pipeline import BtcStages
    return BtcStages()
