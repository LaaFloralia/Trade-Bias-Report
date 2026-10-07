"""Fixed, network-free stages used to exercise the generic BTC workflow (Phase A)."""
from __future__ import annotations

from datetime import timedelta

from btc.common import write_json
from btc.stages import AnalysisError, ReportParts

FACTS = [
    ('F_TEST_FLOW_1D', 125.5, 'USD m', '125.50 USD m'),
    ('F_TEST_FLOW_5D', -40.25, 'USD m', '-40.25 USD m'),
]


def fake_collection(started):
    return {'symbol': 'BTCUSD', 'synthetic': True, 'collection_started_at': started.isoformat(),
            'collection_completed_at': (started + timedelta(seconds=30)).isoformat(),
            'sources': [{'source_id': 'test_source', 'status': 'ok', 'retrieved_at': started.isoformat(),
                         'observed_at': started.isoformat(), 'published_at': None,
                         'source_url': 'https://example.invalid/test', 'values': {}, 'error_kind': None,
                         'raw_sha256': [], 'attempts': 1, 'elapsed_ms': 1.0, 'notes': []}]}


def fake_collect(started_at=None):
    def collect(job, ctx, output):
        write_json(output, fake_collection(started_at or ctx.started_at))
    return collect


class FakeStages:
    def __init__(self, required_figures=('flows',)):
        self.required_figures = list(required_figures)

    def collect(self, ctx):
        return fake_collection(ctx.started_at)

    def build_facts(self, collection, ctx):
        stamp = collection['collection_started_at']
        return {'symbol': 'BTCUSD', 'as_of': stamp, 'facts': [
            {'fact_id': ident, 'value': value, 'unit': unit, 'display': display, 'display_value': value,
             'observed_at': stamp, 'retrieved_at': stamp, 'source_id': 'test_source', 'stale': False, 'status': 'ok'}
            for ident, value, unit, display in FACTS]}

    def analysis_input(self, collection, facts, ctx):
        return '# TEST 入力\n\n' + '\n'.join(f'- {f["fact_id"]}: {f["display"]}' for f in facts['facts']) + '\n'

    def analysis_schema(self):
        return {'$schema': 'https://json-schema.org/draft/2020-12/schema', 'type': 'object',
                'required': ['conclusion', 'bias', 'confidence']}

    def validate_analysis(self, analysis, facts, ctx):
        problems = [f'missing:{k}' for k in ('conclusion', 'bias', 'confidence') if k not in analysis]
        known = {f['fact_id'] for f in facts['facts']}
        problems += [f'unknown_fact:{x}' for x in analysis.get('fact_ids', []) if x not in known]
        if problems:
            raise AnalysisError(problems)
        return dict(analysis)

    def build_report(self, analysis, facts, collection, ctx):
        stamp = facts['as_of']
        rows = {f['fact_id']: f for f in facts['facts']}
        lines = [f'{f["fact_id"]}: {f["display"]}。観測: {stamp}' for f in facts['facts']]
        conclusion = f'TEST 結論: {analysis["conclusion"]}'
        condition = '反証・無効化条件：TEST の合成条件が崩れた場合'
        md = '\n'.join([
            '# BTCUSD TEST レポート', '', '## 0. 判断', '', '```yaml', 'symbol: BTCUSD', f'mode: {ctx.mode}',
            f'session_slot: {ctx.session_slot}', f'collected_at: {stamp}', '```', '', conclusion, '',
            condition, '', '## 1. ファクト', '', '| id | value |', '|---|---|',
            *[f'| {f["fact_id"]} | {f["display"]} |' for f in facts['facts']], '',
            *[x + '\n' for x in lines], '## 2. 長い表', '',
            '| ' + ' | '.join(f'列{i}' for i in range(14)) + ' |', '|' + '---|' * 14,
            '| ' + ' | '.join(f'値{i}の説明文' for i in range(14)) + ' |', ''])
        figures = [{'id': 'flows', 'type': 'diverging_bars', 'title': 'TEST フロー', 'unit': 'USD m',
                    'subtitle': 'USD m / 合成値', 'caption': '合成値による表示検証です。',
                    'source_label': 'TEST', 'source_url': 'https://example.invalid/',
                    'items': [{'label': ident, 'value': rows[ident]['value'], 'display': rows[ident]['display'],
                               'source_quote': line, 'tone': 'negative' if rows[ident]['value'] < 0 else 'neutral'}
                              for ident, line in zip(rows, lines)]}]
        bindings = [{'figure': 'flows', 'label': ident, 'fact_id': ident, 'value': rows[ident]['value'],
                     'display': rows[ident]['display']} for ident in rows]
        summary = {'eyebrow': 'BTCUSD TEST', 'short_title': 'チャート外分析 TEST', 'report_date': stamp[:10],
                   'status': '合成データによる検証',
                   'conclusion': {'title': '今回の見立て', 'text': analysis['conclusion'], 'source_quote': conclusion},
                   'conditions': [{'title': '反証・無効化条件', 'text': 'TEST の合成条件が崩れた場合',
                                   'source_quote': condition}]}
        core = {'bias': analysis['bias'], 'no_trade': False, 'no_trade_reason': None,
                'risk_events_next_24h': [], 'positioning_summary': 'TEST', 'confidence': analysis['confidence']}
        return ReportParts(markdown=md, summary=summary, figures=figures, bindings=bindings, machine_core=core,
                           machine_extensions={'btc': {'test': True}}, required_figures=self.required_figures)
