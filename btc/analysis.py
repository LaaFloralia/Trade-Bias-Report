"""Parent analysis JSON: input briefing, schema, validation and token expansion (design 8.2; P9).

The parent writes interpretation and selection only. Quantities and times
appear as ``{{fact:<fact_id>}}`` / ``{{event:<event_id>:start_at}}`` tokens and
are expanded by code. Text is plain: markup characters (``< > [ ] ` |``),
``javascript:`` and line breaks are rejected (A2: external and parent strings
never become HTML, links or images in the Markdown).
"""
from __future__ import annotations

import json
from pathlib import Path
import re

from btc.common import JST, parse_time
from btc.facts import fact_text
from btc.jsonschema_lite import validate as schema_validate
from btc.stages import AnalysisError
from btc.text import md_safe

SCHEMA_PATH = Path(__file__).resolve().parent / 'schemas' / 'parent-analysis.schema.json'
SCHEMA_VERSION = 'btc-parent-analysis-1.0'
TOKEN = re.compile(r'\{\{(fact|event):([a-z0-9_.:-]+?)(?::(start_at))?\}\}')
ANY_TOKEN = re.compile(r'\{\{[^}]*\}\}')
UNSAFE = re.compile(r'[<>\[\]`|\r\n\t\x00-\x08\x0b-\x1f\x7f]|javascript:|vbscript:|data:', re.I)
DIGIT = re.compile(r'[0-9０-９]')
# Fixed terms that contain digits but carry no market quantity.
ALLOWED_TERMS = re.compile(r'25Δ|8項目|[123]群|S&P\s?500|M2|10年|2年|Funding\s?8h|8h|'
                           r'[0-9０-９]{1,3}(?:日|時間|週|分|営業日|か月)')
MAX_TEXT = 600
RULES = {
    'etf_direction_flip': 'ETF 5営業日合計の向きが変わる、または閾値を外れる',
    'funding_crowding_clears': '同じ向きのFunding過密（2市場以上・極端百分位・OI増加）が解消する',
    'macro_alignment_changes': '米2年利回りと米ドル広義指数の組合せの判定が変わる',
    'event_window_ends': '停止時間が終わり、新しいデータで再評価できる',
    'source_recovers': '欠測していた情報源が回復する',
    'liquidity_cluster_removed': '同じ価格帯の観測量が次回同条件の観測で50%以上減る',
    'book_snapshot_expired': '板の観測の有効期限（90秒）を過ぎる',
    'option_expiry_passed': '対象のオプション満期を通過する',
    'option_oi_share_drops': '同じ満期・ストライクの建玉シェアが次回30%以上下がる',
    'news_verification_changes': 'ニュースの一次本文確認の状況が変わる（訂正・撤回を含む）',
    'stable_supply_turns': 'ステーブル供給の7日・30日変化の判定が変わる',
    'reference_price_conflict': '参照価格の市場間差が50bpを超える、または2市場未満になる',
}
CLAIM_KINDS = ('fact_interpretation', 'hypothesis', 'limitation')
UNUSABLE = ('missing', 'invalid', 'warming_up', 'terms_restricted', 'not_applicable')


def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding='utf-8'))


def _event_index(facts: dict) -> dict:
    return {e['id']: e for e in facts['state'].get('events', [])}


def expand(text: str, facts_index: dict, events: dict) -> str:
    def replace(match):
        kind, ident, field = match.groups()
        if kind == 'fact':
            return fact_text(facts_index[ident])
        return parse_time(events[ident]['start_at']).astimezone(JST).strftime('%Y-%m-%d %H:%M JST')
    return TOKEN.sub(replace, text)


def _check_claim(claim: dict, where: str, facts_index: dict, events: dict, problems: list) -> None:
    text = claim['text']
    if len(text) > MAX_TEXT:
        problems.append(f'{where}: text_too_long')
    if UNSAFE.search(text):
        problems.append(f'{where}: markup_or_unsafe_characters')
    tokens = TOKEN.findall(text)
    stripped = TOKEN.sub(' ', text)
    if ANY_TOKEN.search(stripped):
        problems.append(f'{where}: malformed_token')
    if DIGIT.search(ALLOWED_TERMS.sub(' ', stripped)):
        problems.append(f'{where}: raw_number_outside_tokens')
    ids = set(claim['fact_ids'])
    for fact_id in claim['fact_ids']:
        if fact_id not in facts_index:
            problems.append(f'{where}: unknown_fact:{fact_id}')
    for kind, ident, field in tokens:
        if kind == 'fact':
            if ident not in facts_index:
                problems.append(f'{where}: unknown_fact_token:{ident}')
            elif ident not in ids:
                problems.append(f'{where}: token_fact_not_listed:{ident}')
        elif ident not in events or field != 'start_at':
            problems.append(f'{where}: unknown_event_token:{ident}')
    if claim['claim_kind'] == 'fact_interpretation':
        if not claim['fact_ids']:
            problems.append(f'{where}: interpretation_without_facts')
        unusable = [f for f in claim['fact_ids'] if f in facts_index and facts_index[f]['status'] in UNUSABLE]
        if unusable:
            # A missing / warming-up / restricted fact can only appear in a hypothesis or limitation.
            problems.append(f'{where}: interpretation_of_unusable_fact:{unusable[0]}')


def _claims(analysis: dict):
    yield 'thesis', analysis['thesis']
    for key in ('liquidity', 'positioning', 'bias'):
        yield f'three_domains.{key}', analysis['three_domains'][key]
    yield 'positioning_summary', analysis['positioning_summary']
    for i, a in enumerate(analysis['news_assessments']):
        yield f'news_assessments[{i}].interpretation', a['interpretation']
    for i, s in enumerate(analysis['scenarios']):
        yield f'scenarios[{i}].expected_effect', s['expected_effect']
        yield f'scenarios[{i}].counter_case', s['counter_case']
    yield 'destination_choice.rationale', analysis['destination_choice']['rationale']
    yield 'destination_choice.counter_case', analysis['destination_choice']['counter_case']
    for i, c in enumerate(analysis['counter_cases']):
        yield f'counter_cases[{i}]', c
    for i, r in enumerate(analysis['reevaluation_conditions']):
        yield f'reevaluation_conditions[{i}].explanation', r['explanation']
    for i, c in enumerate(analysis['limitations']):
        yield f'limitations[{i}]', c


def _norm(text: str) -> str:
    return re.sub(r'\s+', ' ', str(text or '')).strip().lower()


def validate(analysis: dict, facts: dict, *, edition_id: str) -> dict:
    """Schema + semantic checks; returns the analysis with an ``expanded`` copy of every claim."""
    problems = [f'schema: {e}' for e in schema_validate(analysis, schema())]
    if problems:
        raise AnalysisError(problems[:40])
    facts_index = {f['fact_id']: f for f in facts['facts']}
    events = _event_index(facts)
    news = {n['id']: n for n in facts['state'].get('news', {}).get('items', [])}
    if analysis['edition_id'] != edition_id:
        problems.append('edition_id_mismatch')
    if analysis['input_manifest_sha256'] != facts['input_manifest_sha256']:
        problems.append('input_manifest_mismatch')
    if analysis['requested_status'] == 'changes_requested':
        problems.append('parent_requested_changes')
        problems += [f'parent_issue:{i["code"]}' for i in analysis['issues']][:20]
    elif analysis['issues']:
        problems.append('issues_must_be_empty_when_ready')
    for where, claim in _claims(analysis):
        _check_claim(claim, where, facts_index, events, problems)
    seen = set()
    for i, a in enumerate(analysis['news_assessments']):
        where = f'news_assessments[{i}]'
        item = news.get(a['news_id'])
        if item is None:
            problems.append(f'{where}: unknown_news:{a["news_id"]}')
            continue
        if a['news_id'] in seen:
            problems.append(f'{where}: duplicate_news')
        seen.add(a['news_id'])
        if a['event_cluster_id'] != item['event_cluster_id']:
            problems.append(f'{where}: cluster_mismatch')
        body_ok = (item.get('body') or {}).get('status') == 'retrieved'
        if a['verification'] == 'primary_confirmed' and item['code_verification'] != 'primary_body_retrieved':
            problems.append(f'{where}: primary_confirmed_without_official_body')
        if a['verification'] == 'secondary_body_confirmed' and not body_ok:
            problems.append(f'{where}: body_confirmed_without_body')
        if a['importance'] in ('critical', 'high') and a['verification'] not in ('primary_confirmed', 'secondary_body_confirmed'):
            problems.append(f'{where}: high_importance_requires_body_check')
        if (a['verification'] in ('headline_only', 'disputed', 'retracted') and a['impact'] in ('supportive', 'adverse')
                and a['interpretation']['claim_kind'] == 'fact_interpretation'):
            # Headlines are unverified leads (P4): a direction may only be stated as a hypothesis.
            problems.append(f'{where}: unverified_news_stated_as_fact')
        source = ' '.join([item['title'], item.get('excerpt', ''), (item.get('body') or {}).get('excerpt', '')])
        if not a['source_quote'].strip() or _norm(a['source_quote']) not in _norm(source):
            problems.append(f'{where}: source_quote_not_in_retrieved_text')
        if UNSAFE.search(a['source_quote']):
            problems.append(f'{where}: source_quote_unsafe_characters')
        for fact_id in a['fact_ids']:
            if fact_id not in facts_index:
                problems.append(f'{where}: unknown_fact:{fact_id}')
    ids = set()
    for i, s in enumerate(analysis['scenarios']):
        if s['id'] in ids or not re.fullmatch(r'[a-z0-9_-]{1,40}', s['id']):
            problems.append(f'scenarios[{i}]: invalid_or_duplicate_id')
        ids.add(s['id'])
        for rule in s['condition_rule_ids'] + s['invalidation_rule_ids']:
            if rule not in RULES:
                problems.append(f'scenarios[{i}]: unknown_rule:{rule}')
        if not s['invalidation_rule_ids']:
            problems.append(f'scenarios[{i}]: invalidation_required')
        for fact_id in s['fact_ids']:
            if fact_id not in facts_index:
                problems.append(f'scenarios[{i}]: unknown_fact:{fact_id}')
    for i, r in enumerate(analysis['reevaluation_conditions']):
        if r['rule_id'] not in RULES:
            problems.append(f'reevaluation_conditions[{i}]: unknown_rule:{r["rule_id"]}')
        for fact_id in r['fact_ids']:
            if fact_id not in facts_index:
                problems.append(f'reevaluation_conditions[{i}]: unknown_fact:{fact_id}')
    problems += destination_problems(analysis['destination_choice'], facts, facts_index)
    if problems:
        raise AnalysisError(problems[:60])
    out = json.loads(json.dumps(analysis))
    expanded = {}
    for where, claim in _claims(analysis):
        expanded[where] = expand(claim['text'], facts_index, events)
    out['expanded'] = expanded
    return out


def destination_problems(choice: dict, facts: dict, facts_index: dict) -> list[str]:
    problems = []
    if (choice['side'] == 'none') != (choice['kind'] == 'none'):
        problems.append('destination: side_and_kind_must_both_be_none_or_both_set')
    for rule in choice['invalidation_rule_ids']:
        if rule not in RULES:
            problems.append(f'destination: unknown_rule:{rule}')
    if choice['kind'] == 'none':
        if choice['level_fact_ids']:
            problems.append('destination: levels_without_kind')
        return problems
    if not choice['level_fact_ids']:
        problems.append('destination: levels_required')
    if not choice['invalidation_rule_ids']:
        problems.append('destination: invalidation_required')
    st = facts['state']
    allowed = ({w['price_fact_id'] for w in st.get('liquidity', {}).get('walls', [])}
               if choice['kind'] == 'observed_book_cluster' else
               {c['fact_id'] for c in st.get('options', {}).get('clusters', [])} |
               {e['max_pain_fact_id'] for e in st.get('options', {}).get('expiries', [])})
    ref = facts_index.get(st.get('reference_price', {}).get('fact_id') or '')
    for fact_id in choice['level_fact_ids']:
        if fact_id not in allowed:
            problems.append(f'destination: level_not_allowed_for_kind:{fact_id}')
            continue
        level = facts_index[fact_id]['value']
        if ref is None or ref['value'] is None or level is None:
            problems.append('destination: reference_price_or_level_missing')
        elif (choice['side'] == 'up') != (level > ref['value']):
            problems.append(f'destination: level_on_wrong_side:{fact_id}')
    return problems


def _table_row(*cells) -> str:
    return '| ' + ' | '.join(md_safe(c) if c is not None else '—' for c in cells) + ' |'


def briefing(facts: dict, preview: dict, *, edition_id: str, mode: str, session_slot: str,
             previous: list | None = None) -> str:
    """analysis-input.md: what the parent reads before writing the analysis JSON."""
    st = facts['state']
    lines = [
        '# BTCUSD チャート外分析 親の入力資料', '',
        f'- edition_id: {edition_id}',
        f'- input_manifest_sha256: {facts["input_manifest_sha256"]}',
        f'- mode / session_slot: {mode} / {session_slot}',
        f'- as_of: {facts["as_of"]}（{parse_time(facts["as_of"]).astimezone(JST).strftime("%Y-%m-%d %H:%M JST")}）',
        '', '## 書き方（コードが検査する）', '',
        '- 親が書くのは解釈と選択だけ。数値・時刻・合計・百分位・閾値はコードが持つ。',
        '- 数値は {{fact:<fact_id>}}、イベント時刻は {{event:<event_id>:start_at}} で書く。トークン外の数字は拒否'
        '（例外: 25Δ・8項目・1〜3群・S&P 500・M2・2年・10年・8h・期間表現の日/時間/週/分/営業日）。',
        '- 使ったトークンの fact_id は同じ claim の fact_ids に列挙する。fact_interpretation は fact_ids が1件以上必要。',
        '- 文字 < > [ ] ` | と改行・javascript: は使わない（HTML・リンク・画像・表として解釈させないため）。',
        '- news_assessments は下表の news_id だけ。primary_confirmed は「公式の本文取得済み」、secondary_body_confirmed は'
        '「本文取得済み」の項目だけ。high/critical は本文確認が必要。source_quote は見出しか抜粋の原文の一部。',
        '- btc_specific_event 群に数えるのは、一次本文確認済み・high/critical・supportive/adverse・期間内の項目だけ（コードが判定）。',
        '- destination_choice は観測板の壁（observed_book_cluster）かオプション建玉（option_oi_cluster）の fact だけ。'
        'なければ side/kind とも none。価格を新しく書かない。',
        '- scenarios・counter_cases・reevaluation_conditions・limitations は各1件以上。rule_id は下の一覧から選ぶ。',
        '- 不一致を見つけたら requested_status=changes_requested と issues を書く（facts は直さない）。', '',
        '## rule_id 一覧', '',
    ]
    lines += [f'- {k}: {v}' for k, v in RULES.items()]
    lines += ['', '## コードの判定（btc_specific_event は親の選択前の仮表示）', '']
    for key, g in preview['groups'].items():
        lines.append(f'- {key}: {g["state"]}（{g["reason_code"]}）')
    lines += [f'- direction: {preview["direction_status"]} / {preview["candidate_direction"]} / 確度 {preview["direction_strength"]}',
              f'- score: {preview["total"]}点 / band {preview["band"]} / confidence {preview["confidence"]}',
              f'- trade_gate: {preview["trade_gate"]["status"]} {preview["trade_gate"]["reason_codes"]}',
              f'- coverage: {preview["coverage_present"]}/8 {preview["bundles"]}', '']
    for item in preview['items']:
        lines.append(f'  - {item["key"]}: {item["points"]}（{item["status"]}・{item["reason_code"]}）')
    lines += ['', '## イベント（event_id）', '', '| event_id | 名称 | 開始 | 重要度 | 状態 | 停止時間 |', '|---|---|---|---|---|---|']
    for e in st.get('events', []):
        window = e.get('stop_window')
        lines.append(_table_row(e['id'], e['name'], parse_time(e['start_at']).astimezone(JST).strftime('%Y-%m-%d %H:%M JST'),
                                e['importance'], e['status'],
                                f'{parse_time(window["start_at"]).astimezone(JST).strftime("%H:%M")}〜'
                                f'{parse_time(window["end_at"]).astimezone(JST).strftime("%H:%M")} JST' if window else None))
    lines += ['', '## ニュース（news_id）', '',
              '| news_id | cluster | 配信元 | 公表 | コード確認 | 見出し | 抜粋 |', '|---|---|---|---|---|---|---|']
    for n in st.get('news', {}).get('items', []):
        excerpt = (n.get('body') or {}).get('excerpt') or n.get('excerpt', '')
        lines.append(_table_row(n['id'], n['event_cluster_id'], n['publisher'],
                                parse_time(n['published_at']).astimezone(JST).strftime('%m-%d %H:%M JST'),
                                n['code_verification'], n['title'], excerpt[:200]))
    lines += ['', '## 目的地の候補（level_fact_ids）', '']
    for w in st.get('liquidity', {}).get('walls', []):
        lines.append(f'- observed_book_cluster: {w["price_fact_id"]}（{w["venue"]} {w["side"]}）')
    for c in st.get('options', {}).get('clusters', []):
        lines.append(f'- option_oi_cluster: {c["fact_id"]}')
    for e in st.get('options', {}).get('expiries', []):
        lines.append(f'- option_oi_cluster（Max Pain・参考）: {e["max_pain_fact_id"]}')
    if previous:
        lines += ['', '## 前回版', '']
        for p in previous:
            lines.append(f'- {p.get("collected_at")} {p.get("session_slot")}: {p.get("md_path")}')
    lines += ['', '## ファクト一覧', '', '| fact_id | 表示 | status | 時点 | source |', '|---|---|---|---|---|']
    for f in facts['facts']:
        when = f['observed_at'] or f['observation_date'] or f['period_end'] or f['retrieved_at']
        lines.append(_table_row(f['fact_id'], f['display'], f['status'], when, f['source_id']))
    lines += ['', '## JSON の骨組み', '', '```json', json.dumps(skeleton(edition_id, facts), ensure_ascii=False, indent=1), '```', '']
    return '\n'.join(lines)


def skeleton(edition_id: str, facts: dict) -> dict:
    ref = facts['state'].get('reference_price', {}).get('fact_id')
    claim = {'text': '…{{fact:' + (ref or 'FACT_ID') + '}}…', 'fact_ids': [ref] if ref else [], 'claim_kind': 'fact_interpretation'}
    return {'schema_version': SCHEMA_VERSION, 'edition_id': edition_id,
            'input_manifest_sha256': facts['input_manifest_sha256'], 'requested_status': 'ready_for_validation',
            'thesis': claim, 'three_domains': {'liquidity': claim, 'positioning': claim, 'bias': claim},
            'positioning_summary': claim, 'news_assessments': [],
            'scenarios': [{'id': 'base', 'condition_rule_ids': ['etf_direction_flip'], 'fact_ids': [],
                           'expected_effect': claim, 'counter_case': claim, 'invalidation_rule_ids': ['event_window_ends']}],
            'destination_choice': {'side': 'none', 'kind': 'none', 'level_fact_ids': [], 'rationale': claim,
                                   'counter_case': claim, 'invalidation_rule_ids': []},
            'counter_cases': [claim], 'reevaluation_conditions': [{'rule_id': 'source_recovers', 'fact_ids': [],
                                                                   'explanation': claim}],
            'limitations': [{'text': '…', 'fact_ids': [], 'claim_kind': 'limitation'}], 'issues': []}
