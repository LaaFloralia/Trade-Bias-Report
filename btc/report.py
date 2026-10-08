"""Markdown, summary, figures, bindings and machine.json for one BTCUSD edition (design 8; P9).

Everything here is code-rendered from the frozen facts, the code's scoring
and the parent's validated analysis. Parent text arrives already checked
(no markup characters, numbers only through fact tokens) and expanded.
External strings (headlines, excerpts, provider labels, parsed calendar
names) were neutralised when they entered the facts (btc.text.md_safe).

Figure items quote the Markdown line that prints the same number; each
numeric item is bound to one fact (value == display_value, display ==
display). Figures without data are omitted and the reason is written in the
same section.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re

from btc import SYMBOL, scoring
from btc.analysis import RULES
from btc.common import JST, parse_time, read_json
from btc.facts import fact_text
from btc.stages import ReportParts
from btc.text import md_safe

UTC = timezone.utc
SCHEMA_VERSION = 'btc-offchart-1.0'
FEATURE_VERSION = 'btc-facts-1.0'
LIQUIDITY_NOTE = '清算ヒートマップは未使用（有料・推定モデル）。代わりに観測した板とオプション建玉を表示'
YAML_KEYS = ('schema_version', 'asset', 'mode', 'session_slot', 'edition', 'as_of', 'collected_at', 'generated_at',
             'valid_until', 'bias', 'direction_status', 'candidate_direction', 'direction_strength', 'confidence',
             'confidence_band', 'score_total', 'no_trade', 'no_trade_reason', 'trade_gate', 'coverage_present',
             'coverage_required')
HEADINGS = ('## 0. 判断', '## 1. データ範囲と欠測', '## 2. バイアスと固定8項目スコア', '## 3. 比率・ポジショニング',
            '## 4. 流動性の観測と仮説', '## 5. ETF・現物需要・オンチェーン', '## 6. マクロ・イベント',
            '## 7. ニュースと価格反応', '## 8. シナリオ・反証・再評価条件',
            '## 9. 前回との差分（Weeklyは週次振り返りを追加）', '## 10. ファクト一覧と来歴')
USABLE = ('ok', 'provisional', 'partial', 'stale')
KIND_LABEL = {'fact_interpretation': '解釈', 'hypothesis': '仮説', 'limitation': '制約'}
VENUE_LABEL = {'binance': 'Binance', 'bybit': 'Bybit', 'okx': 'OKX', 'kraken': 'Kraken', 'bitstamp': 'Bitstamp',
               'deribit': 'Deribit'}
MARKET_LABEL = {'spot': '現物', 'linear_perp': '無期限'}
STATUS_LABEL = {'ok': '確認済み', 'provisional': '速報', 'partial': '一部', 'stale': '古い', 'missing': '欠測',
                'invalid': '不正', 'conflict': '矛盾', 'warming_up': '履歴不足', 'not_applicable': '対象外',
                'terms_restricted': '規約により未使用'}
ETF_STATE_LABEL = {'ok': '全銘柄の数値がそろい合計の照合も成立（採点に使う）',
                   'incomplete': '全銘柄の値が未確定（採点にも正常な充足にも数えない）',
                   'conflict': '提供元の合計と銘柄の合計が一致しない（両方を残し採点しない・データ保留）',
                   'lagged': '期待日の行がなく1営業日遅れ（正常な充足に数えない）',
                   'stale': '期待日から2営業日以上遅れ（古い）',
                   'unknown': '米取引日の暦が収録範囲外（未確認）', 'missing': '欠測'}
CODE_VERIFICATION = {'primary_body_retrieved': '公式の本文取得済み', 'secondary_body_retrieved': '本文取得済み',
                     'headline_only': '見出しのみ'}
VERIFICATION = {'primary_confirmed': '一次本文で確認', 'secondary_body_confirmed': '報道本文で確認',
                'headline_only': '見出しのみ', 'disputed': '異論あり', 'retracted': '撤回'}
IMPACT = {'supportive': '上向き材料', 'adverse': '下向き材料', 'mixed': '混在', 'unknown': '不明'}
GROUP = {'macro': 'マクロ', 'spot_demand': '現物需要', 'btc_specific_event': 'BTC固有イベント', 'context_only': '文脈のみ'}
GATE_LABEL = {'clear': '停止条件なし', 'scheduled_pause': 'イベント停止中', 'data_hold': 'データ保留',
              'incident_hold': '障害停止', 'expired': '期限切れ'}
CODE_LIMITATIONS = (
    'スコアと確度は未校正の序数で、的中率や期待収益ではない',
    'Funding・比率・建玉はBinance・Bybit・OKXの線形BTC無期限だけで、市場全体ではない',
    'オプションはDeribit BTC inverse optionsだけ。建玉は保有残高で、売買の向きやディーラーのガンマを示さない',
    'ETFの対象日はNYSEの休場表（2028年末まで）で決める。収録範囲外の日は米営業日を判定せず未確認とする',
    'ニュースの価格反応は公表時刻付近の値動きで、因果を示さない',
)


# ----------------------------------------------------------------- helpers

def zs(value) -> str:
    return parse_time(value).astimezone(UTC).isoformat().replace('+00:00', 'Z')


def jst(value) -> str:
    return parse_time(value).astimezone(JST).strftime('%Y-%m-%d %H:%M JST')


def hm(value) -> str:
    return parse_time(value).astimezone(JST).strftime('%H:%M')


def md_day(value) -> str:
    return parse_time(value).astimezone(JST).strftime('%m/%d')


def yaml_value(value) -> str:
    return json.dumps(value, ensure_ascii=False)


class Doc:
    def __init__(self):
        self.lines: list[str] = []

    def add(self, *lines: str) -> None:
        self.lines.extend(lines)

    def item(self, text: str) -> str:
        """A bullet; returns the exact text so summaries and figures can quote it."""
        self.lines.append('- ' + text)
        return text

    def table(self, header: list[str], rows: list[list]) -> None:
        self.lines.append('| ' + ' | '.join(header) + ' |')
        self.lines.append('|' + '---|' * len(header))
        for row in rows:
            self.lines.append('| ' + ' | '.join('—' if c is None or c == '' else str(c) for c in row) + ' |')
        self.lines.append('')

    def text(self) -> str:
        return '\n'.join(self.lines).rstrip('\n') + '\n'


class Figures:
    def __init__(self, index: dict):
        self.index = index
        self.figures: list[dict] = []
        self.bindings: list[dict] = []
        self.omitted: list[str] = []

    def add(self, figure: dict, items: list[tuple]) -> bool:
        """items: (label, fact or None, quote, extra dict). Numeric items need a usable fact."""
        out = []
        for label, fact, quote, extra in items:
            item = {'label': label, 'source_quote': quote, **(extra or {})}
            if fact is not None:
                if fact['status'] not in USABLE or fact['display_value'] is None:
                    continue
                item.update(value=fact['display_value'], display=fact['display'])
                item.setdefault('tone', 'negative' if fact['display_value'] < 0 else 'neutral')
                self.bindings.append({'figure': figure['id'], 'label': label, 'fact_id': fact['fact_id'],
                                      'value': fact['display_value'], 'display': fact['display']})
            out.append(item)
        if not out:
            return False
        self.figures.append({**figure, 'items': out})
        return True


def _claim_text(analysis: dict, where: str, claim: dict) -> str:
    return f'{analysis["expanded"][where]}（{KIND_LABEL[claim["claim_kind"]]}）'


def _pick(facts: list, *, category: str, provider: str, metric: str, instrument: str | None = None):
    for fact in facts:
        parts = fact['logical_id'].split('.')
        if (fact['category'] == category and parts[2] == provider and fact['metric'] == metric
                and (instrument is None or parts[3] == instrument)):
            return fact
    return None


def direction_label(ev: dict) -> str:
    if ev['direction_status'] == 'withheld':
        return f'BTCは方向付与保留（{ev["direction_reason_label"]}）'
    side = '上向き' if ev['candidate_direction'] == 'up' else '下向き'
    return f'BTCは{side}（確度 {ev["direction_strength"]}）'


def report_kind(mode: str, slot: str) -> str:
    return 'weekly' if mode == 'weekly' else f'daily_{slot}'


# ----------------------------------------------------------------- freshness at finalize

def finalize_freshness(facts: dict, analysis: dict, finalize_at: datetime, hard_invalid: list) -> dict:
    expired = [f['fact_id'] for f in facts['facts']
               if f['status'] in USABLE and f['valid_until'] and parse_time(f['valid_until']) < finalize_at]
    referenced = set()
    for value in _walk_fact_ids(analysis):
        referenced.add(value)
    return {'as_of_check': 'data_hold' if hard_invalid else 'passed', 'finalize_checked_at': zs(finalize_at),
            'expired_at_finalize': expired, 'referenced_expired': sorted(referenced & set(expired)),
            'note': '収集時点で鮮度を検査し、完成時刻に有効期限を過ぎた値を列挙する。期限切れの値は採点を変えず、'
                    '目的地の根拠なら目的地を失効にする。'}


def _walk_fact_ids(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ('fact_ids', 'level_fact_ids') and isinstance(value, list):
                yield from value
            else:
                yield from _walk_fact_ids(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_fact_ids(value)


def destination(analysis: dict, index: dict, finalize_at: datetime) -> dict:
    choice = analysis['destination_choice']
    out = {'choice': choice, 'status': 'none', 'valid_until': None}
    if choice['kind'] == 'none':
        return out
    limits = [parse_time(index[f]['valid_until']) for f in choice['level_fact_ids'] if index[f]['valid_until']]
    until = min(limits) if limits else None
    out['valid_until'] = until
    out['status'] = 'expired' if until is not None and until < finalize_at else 'active'
    return out


# ----------------------------------------------------------------- sections

def section_decision(doc: Doc, ev: dict, analysis: dict, machine_yaml: dict) -> dict:
    doc.add(HEADINGS[0], '', '```yaml', *[f'{k}: {yaml_value(machine_yaml[k])}' for k in YAML_KEYS], '```', '')
    label = direction_label(ev)
    verdict = doc.item(f'判断: {label}。NO-TRADE: ' + (f'あり（{ev["no_trade_reason"]}）' if ev['no_trade'] else 'なし'))
    score = doc.item(f'整合度: {ev["total"]}点・{ev["band"]}（confidence {ev["confidence"]:.2f}、未校正の序数で的中率ではない）。'
                     f'上限制御: {"・".join(ev["caps"]) or "なし"}')
    gate = ev['trade_gate']
    if gate['active_windows']:
        names = '、'.join(w['label'] for w in gate['active_windows'])
        stop = doc.item(f'停止時間: 停止中（{names}）')
    elif gate['upcoming_windows']:
        stop = doc.item('停止時間: ' + '、'.join(w['label'] for w in gate['upcoming_windows']))
    elif ev['calendar_next_24h'] == 'ok':
        stop = doc.item('停止時間: 次24時間の停止時間なし（公式予定の確認範囲内）')
    else:
        stop = doc.item('停止時間: 公式予定の一部が未確認のため「停止時間なし」とは言えない')
    recheck = doc.item(f'次に確認すること: {jst(gate["recheck_at"])}に再評価（{ev["recheck_reason"]}）。'
                       f'判断の有効期限は{jst(ev["valid_until"])}')
    thesis = doc.item('要旨: ' + _claim_text(analysis, 'thesis', analysis['thesis']))
    doc.item('用途: 研究用の入力候補（research_only）。自動売買の実行は無効（execution_enabled=false）。'
             '親のレビューは独立レビューではない')
    doc.add('')
    return {'verdict': verdict, 'score': score, 'stop': stop, 'recheck': recheck, 'thesis': thesis, 'label': label}


def section_coverage(doc: Doc, facts: dict, ev: dict, fresh: dict, index: dict) -> dict:
    st = facts['state']
    col = st['collection']
    doc.add(HEADINGS[1], '')
    elapsed = (col.get('elapsed_ms') or 0) / 1000
    doc.item(f'収集: {jst(col["started_at"])}〜{jst(col["completed_at"])}（所要{elapsed:.0f}秒・上限540秒）'
             + ('。時間上限で打ち切った情報源あり' if col.get('budget_exhausted') else ''))
    ref = st.get('reference_price', {})
    ref_fact = index.get(ref.get('fact_id') or '')
    if ref_fact and ref_fact['value'] is not None:
        venues = '・'.join(VENUE_LABEL.get(index[v]['venue'], index[v]['venue']) for v in ref['venue_fact_ids'])
        ref_line = doc.item(f'参照価格: {ref_fact["display"]}（{venues}の中央値、{jst(ref_fact["retrieved_at"])}取得、'
                            f'市場間差{ref["spread_bps"]:.1f}bp）')
    else:
        ref_line = doc.item('参照価格: 欠測（2市場以上の同時刻の気配がない）')
    missing = [scoring.BUNDLE_LABELS[k] for k, v in ev['bundles'].items() if not v]
    coverage = doc.item(f'採点入力の充足: {ev["coverage_present"]}/8（欠け: {"・".join(missing) or "なし"}）。'
                        '充足は入力の有無で、内容の信頼度ではない')
    doc.add('', '| source_id | 状態 | 取得 | 理由 |', '|---|---|---|---|')
    for h in st['source_health']:
        doc.add(f'| {h["source_id"]} | {STATUS_LABEL.get(h["status"], h["status"])} | {jst(h["checked_at"])} | '
                f'{h["reason"] or "—"} |')
    doc.add('')
    unused = [f'{t["source_id"]}（{t["reason"]}）' for t in st.get('terms_restricted', []) + st.get('not_used', [])]
    doc.item('未使用の情報源: ' + ('、'.join(unused) or 'なし'))
    doc.item(f'完成時の鮮度: 収集時点の検査は{"合格" if fresh["as_of_check"] == "passed" else "データ保留"}。'
             f'完成時刻{jst(fresh["finalize_checked_at"])}に有効期限を過ぎた値は{len(fresh["expired_at_finalize"])}件'
             f'（板・気配など短期の観測）。分析が参照した期限切れ: {"、".join(fresh["referenced_expired"]) or "なし"}')
    doc.item('CME: 契約別の公式満期・メンテナンス時刻はMacから取得できない（403）ため未確認。最終金曜の推定で停止時間を作らない')
    doc.add('')
    return {'reference': ref_line, 'coverage': coverage}


def section_score(doc: Doc, facts: dict, ev: dict, index: dict) -> dict:
    doc.add(HEADINGS[2], '')
    lines = {}
    for key, g in ev['groups'].items():
        support = '、'.join(fact_text(index[f]) for f in g['fact_ids'] if f in index)
        lines[key] = doc.item(f'{scoring.GROUP_LABELS[key]}: {scoring.STATE_TEXT[g["state"]]}（{g["reason_code"]}）'
                              + (f'。根拠: {support}' if support else ''))
    doc.item(f'方向: {direction_label(ev)}（{"・".join(ev["direction_reason_codes"])}）')
    doc.add('', '| 項目 | key | 点 | 状態 | 理由 |', '|---|---|---|---|---|')
    for item in ev['items']:
        doc.add(f'| {scoring.ITEM_LABELS[item["key"]]} | {item["key"]} | {item["points"]:+d} | {item["status"]} | '
                f'{item["reason_code"]} |')
    doc.add('')
    doc.item(f'合計: {ev["total"]}点で{ev["band"]}（High: 5点以上かつ3群、Med: 4点以上かつ2群、Med-cautious: 3点かつ2群、'
             '他はLow。確度「低」は必ずLow）')
    gate = ev['trade_gate']
    doc.item(f'trade_gate: {gate["status"]}（{GATE_LABEL[gate["status"]]}。{"・".join(gate["reason_codes"]) or "理由なし"}）')
    doc.item(f'policy: {scoring.POLICY_VERSION}（policy_hash {scoring.POLICY_HASH[:12]}）')
    doc.add('')
    return lines


def section_positioning(doc: Doc, facts: dict, analysis: dict, index: dict, figs: Figures) -> dict:
    st = facts['state']
    F = facts['facts']
    doc.add(HEADINGS[3], '')
    summary = doc.item('ポジショニング要約: ' + _claim_text(analysis, 'positioning_summary', analysis['positioning_summary']))
    domain = doc.item('比率の解釈: ' + _claim_text(analysis, 'three_domains.positioning', analysis['three_domains']['positioning']))
    funding_items, oi_items, dist = [], [], []
    for v in st.get('derivatives', {}).get('venues', []):
        name = VENUE_LABEL[v['venue']]
        f8 = _pick(F, category='derivatives', provider=v['venue'], metric='funding_equiv_8h')
        perc = index.get(v.get('funding_percentile_fact_id') or '')
        if f8:
            line = doc.item(f'{name} 確定Funding（8h換算）: {f8["display"]}' + (f'（過去の順位: {fact_text(perc)}）' if perc else ''))
            funding_items.append((f'{name} 確定Funding（8h換算）', f8, line, None))
            if perc and perc['value'] is not None:
                dist.append((f'{name} Funding', perc, line, None))
        realized = _pick(F, category='derivatives', provider=v['venue'], metric='funding_realized_24h')
        if realized:
            doc.item(f'{name} 確定Funding 24時間合計: {realized["display"]}（{STATUS_LABEL[realized["status"]]}）')
        current = _pick(F, category='derivatives', provider=v['venue'], metric='funding_current_predicted')
        if current:
            doc.item(f'{name} 現在/予定Funding（確定値ではない）: {current["display"]}')
        oi = index.get(v['oi_fact_id'])
        chg = index.get(v.get('oi_change_fact_id') or '')
        if oi:
            line = doc.item(f'{name} OI（片側）: {oi["display"]}、OI 24時間変化: {chg["display"] if chg else "—"}')
            if chg:
                oi_items.append((f'{name} OI 24時間変化', chg, line, None))
    weighted = _pick(F, category='derivatives', provider='composite', metric='funding_weighted_8h')
    if weighted:
        doc.item(f'建玉加重Funding（8h換算、{weighted["population"]}）: {weighted["display"]}')
    ratios = st.get('ratios', {})
    for key, label, short in (('account_long_share', 'Binance全口座のロング比率', 'Binance全口座'),
                              ('top_position_long_share', 'Binance上位トレーダー建玉のロング比率', 'Binance上位建玉')):
        fact = index.get(ratios.get(key) or '')
        perc = index.get(ratios.get(f'{key}_percentile') or '')
        if fact:
            line = doc.item(f'{label}: {fact["display"]}' + (f'（{fact_text(perc)}）' if perc else ''))
            if perc and perc['value'] is not None:
                dist.append((short, perc, line, None))
    taker = index.get(ratios.get('taker') or '')
    if taker:
        doc.item(f'Binance無期限のテイカー売買（24時間）: {taker["display"]}')
    for prefix, label in (('am', 'Asset Manager'), ('lf', 'Leveraged Funds'), ('dealer', 'Dealer')):
        fact = index.get(st.get('cot', {}).get(prefix) or '')
        perc = index.get(st.get('cot', {}).get(f'{prefix}_percentile') or '')
        if fact:
            line = doc.item(f'CME BTC先物 {label} ネット/建玉: {fact["display"]}' + (f'（{fact_text(perc)}）' if perc else ''))
            if perc and perc['value'] is not None and prefix in ('am', 'lf'):
                dist.append((f'CME {prefix.upper()}', perc, line, None))
    fgi = st.get('fgi', {})
    fgi_fact = index.get(fgi.get('fact_id') or '')
    if fgi_fact:
        extras = [index[fgi[k]]['display'] for k in ('diff_1d', 'diff_7d') if fgi.get(k)]
        perc = index.get(fgi.get('percentile') or '')
        line = doc.item(f'Crypto Fear & Greed Index: {fgi_fact["display"]}'
                        + (f'、1日差{extras[0]}' if extras else '') + (f'、7日差{extras[1]}' if len(extras) > 1 else '')
                        + (f'、{fact_text(perc)}' if perc else ''))
    dvol = _pick(F, category='options', provider='deribit', metric='dvol')
    if dvol:
        perc = _pick(F, category='options', provider='deribit', metric='dvol_percentile')
        line = doc.item(f'Deribit DVOL: {dvol["display"]}' + (f'（{fact_text(perc)}）' if perc else ''))
        if perc and perc['value'] is not None:
            dist.append(('DVOL', perc, line, None))
    for tenor in ('7d', '30d'):
        fact = index.get(st.get('options', {}).get('skew', {}).get(f'skew_25d_{tenor}') or '')
        if fact:
            perc = index.get(st['options']['skew'].get(f'skew_25d_{tenor}_percentile') or '')
            doc.item(f'25Δスキュー（{tenor}目標、put−call）: {fact["display"]}' + (f'（{fact_text(perc)}）' if perc else ''))
    doc.add('')
    note = []
    if not figs.add({'id': 'derivatives_bias_funding', 'type': 'diverging_bars', 'title': '確定Funding（8h換算）',
                     'unit': '%', 'subtitle': '% / venue別・直近の確定値（予定値ではない）',
                     'caption': '線形BTC無期限の直近確定Fundingを8時間あたりに換算。市場全体の値ではない。',
                     'source_label': 'Binance・Bybit・OKX 公開API', 'source_url': 'https://www.binance.com/en/futures/funding-history/perpetual/funding-fee-history'},
                    funding_items):
        note.append('図なし（確定Funding）: 2市場以上の確定値がない')
    if not figs.add({'id': 'derivatives_bias_oi', 'type': 'bars', 'title': 'OIの24時間変化', 'unit': '%',
                     'subtitle': '% / venue別・片側OI', 'caption': 'OIの増減は新規建てと決済の差で、売買の向きを示さない。',
                     'source_label': 'Binance・Bybit・OKX 公開API', 'source_url': 'https://www.bybit.com/'},
                    oi_items):
        note.append('図なし（OI 24時間変化）: 比較できる24時間前の観測がない（履歴不足）')
    if not figs.add({'id': 'positioning_distribution', 'type': 'bars', 'title': '比率・建玉・心理の履歴順位',
                     'unit': 'パーセンタイル', 'subtitle': '0〜100 / 母集団と比較窓は項目ごとに異なる',
                     'caption': '各項目は別の母集団・別の比較窓での順位（窓と件数は本文）。同じ軸で強弱を比べない。'
                                '履歴不足の項目は出さない。AMはAsset Manager、LFはLeveraged Funds。FGIは本文に出典付きで記載。',
                     'source_label': 'Binance・CFTC・Deribit', 'source_url': 'https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm'},
                    dist):
        note.append('図なし（履歴順位）: 比較に必要な件数の履歴がまだない')
    for line in note:
        doc.item(line)
    if note:
        doc.add('')
    return {'summary': summary, 'domain': domain}


def section_liquidity(doc: Doc, facts: dict, analysis: dict, index: dict, figs: Figures, dest: dict,
                      finalize_at: datetime) -> dict:
    st = facts['state']
    doc.add(HEADINGS[4], '')
    fixed = doc.item(LIQUIDITY_NOTE)
    domain = doc.item('流動性の解釈: ' + _claim_text(analysis, 'three_domains.liquidity', analysis['three_domains']['liquidity']))
    items = []
    ref = index.get(st.get('reference_price', {}).get('fact_id') or '')
    if ref and ref['value'] is not None:
        line = doc.item(f'参照価格（観測）: {ref["display"]}')
        items.append(('参照価格', ref, line, {'tone': 'reference'}))
    walls = st.get('liquidity', {}).get('walls', [])
    for n, w in enumerate(walls, 1):
        price, share = index[w['price_fact_id']], index[w['share_fact_id']]
        side = '買い板' if w['side'] == 'bid' else '売り板'
        venue = f'{VENUE_LABEL.get(w["venue"], w["venue"])}{MARKET_LABEL.get(w["market"], "")}'
        line = doc.item(f'観測板{n} {venue}{side}: {fact_text(price)}、帯の量は片側100bp量の{share["display"]}')
        if n <= 3:
            short = f'{VENUE_LABEL.get(w["venue"], w["venue"])}{"買" if w["side"] == "bid" else "売"}{n}'
            items.append((short, price, line, {'tone': 'caution'}))
    if not walls:
        doc.item('観測板の厚い価格帯: 条件（同じ25bp帯が3回中2回以上、片側100bp量の20%以上）を満たす帯なし')
    book_ids = st.get('liquidity', {}).get('book_fact_ids', [])
    for bid_id, ask_id in zip(book_ids[::2], book_ids[1::2]):  # last snapshot of each venue: bid, ask
        bid, ask = index[bid_id], index[ask_id]
        doc.item(f'{VENUE_LABEL.get(bid["venue"], bid["venue"])} {bid["instrument"]} 板の厚み±100bp: 買い{bid["display"]}・'
                 f'売り{ask["display"] if ask else "—"}（{jst(bid["period_end"] or bid["retrieved_at"])}観測）')
    opt = st.get('options', {})
    if opt.get('status') == 'ok':
        tot, pcr = index[opt['total_fact_id']], index[opt['pcr_fact_id']]
        doc.item(f'オプション建玉合計: {tot["display"]}（{opt["scope"]}）、PCR（建玉）: {pcr["display"]}')
        expiries = opt.get('expiries', [])
        drawn = max(expiries, key=lambda e: e['share_pct'] or 0) if expiries else None  # largest within 7 days
        for e in expiries:
            share, epcr, mp = index[e['share_fact_id']], index[e['pcr_fact_id']], index[e['max_pain_fact_id']]
            doc.item(f'満期 {e["label"]}: 建玉シェア{share["display"]}、PCR {epcr["display"]}')
            day = md_day(e['expiry'])
            line = doc.item(f'Max Pain {day}満期（参考）: {mp["display"]}')
            if e is drawn:
                items.append((f'MaxPain {day}', mp, line, {'tone': 'reference'}))
            for rank, c in enumerate(e['top_strikes'], 1):
                line = doc.item(f'建玉{rank}位 {day}満期: {fact_text(index[c["fact_id"]])}')
                if e is drawn:
                    items.append((f'建玉{rank}位 {day}', index[c['fact_id']], line, None))
    else:
        doc.item('オプション建玉: 欠測（Deribitを取得できず）')
    choice = dest['choice']
    if choice['kind'] == 'none':
        dline = doc.item('目的地: なし（親の選択。観測板・オプション建玉から目的地を置かない）')
    elif dest['status'] == 'expired':
        dline = doc.item(f'目的地: 失効（根拠の観測の有効期限{jst(dest["valid_until"])}を完成時刻{jst(finalize_at)}が過ぎた。'
                         f'親の選択は{"上" if choice["side"] == "up" else "下"}側の'
                         f'{"観測板" if choice["kind"] == "observed_book_cluster" else "オプション建玉"}）')
    else:
        levels = '、'.join(fact_text(index[f]) for f in choice['level_fact_ids'])
        dline = doc.item(f'目的地（仮説・到達保証ではない）: {"上" if choice["side"] == "up" else "下"}側の'
                         f'{"観測板" if choice["kind"] == "observed_book_cluster" else "オプション建玉"} {levels}'
                         f'（有効期限{jst(dest["valid_until"])}）')
    doc.item('目的地の根拠: ' + _claim_text(analysis, 'destination_choice.rationale', choice['rationale']))
    doc.item('目的地の反対ケース: ' + _claim_text(analysis, 'destination_choice.counter_case', choice['counter_case']))
    if choice['invalidation_rule_ids']:
        doc.item('目的地の無効化: ' + '、'.join(f'{r}（{RULES[r]}）' for r in choice['invalidation_rule_ids']))
    doc.add('')
    drawn = figs.add({'id': 'observed_depth', 'type': 'price_map', 'title': '観測板・オプション建玉の価格帯',
                      'unit': 'USD（板はUSDT建てを含む）', 'subtitle': '観測板・推定ではない／清算ヒートマップではない',
                      'caption': '観測時点の板の厚い帯とオプション建玉の集中。価格が引き寄せられる保証はなく、清算位置の推定でもない。'
                                 'Max Painは参考線。',
                      'source_label': '各取引所の公開板・Deribit', 'source_url': 'https://www.deribit.com/'},
                     items if len(items) >= 2 else [])
    if not drawn:
        doc.item('図なし（価格帯）: 参照価格と比較できる観測帯・建玉がない')
        doc.add('')
    return {'fixed': fixed, 'domain': domain, 'destination': dline}


def section_spot(doc: Doc, facts: dict, analysis: dict, index: dict, figs: Figures) -> dict:
    st = facts['state']
    F = facts['facts']
    doc.add(HEADINGS[5], '')
    etf = st.get('etf', {})
    lines = {}
    items = []
    if etf.get('trade_date'):
        lines['etf_date'] = doc.item(
            f'ETF対象日: {etf["trade_date"]}（期待される最新の米営業日 {etf["expected_trade_date"]}、遅れ{etf["lag_business_days"]}営業日）。'
            'Farsideの速報で、全銘柄合計の検算済み値だけを使う')
        arrival = etf.get('arrival', {})
        doc.item(f'ETF到着記録（{etf["expected_trade_date"]}）: 行あり={arrival.get("row_present")}・全銘柄数値={arrival.get("full_numeric")}・'
                 f'前回から変化={arrival.get("changed_since_previous")}')
        fallback = etf.get('lagged_fallback')
        if fallback:
            five_window = etf.get('five_day_window') or []
            state_text = (f'期待日 {fallback["expected_trade_date"]} の行は全銘柄の値が未確定。5営業日合計は前営業日 '
                          f'{fallback["used_trade_date"]} までの窓（{five_window[0]}〜{five_window[-1]}）で算出'
                          '（1営業日遅れ・正常な充足に数えない）')
        else:
            state_text = ETF_STATE_LABEL.get(etf.get('status'), etf.get('status'))
        lines['etf_state'] = doc.item(f'ETFの状態（{etf["expected_trade_date"]}）: {state_text}')
        for fact in F:
            if fact['metric'] in ('etf_reported_total_usd', 'etf_known_partial_sum_usd') and fact['status'] == 'conflict':
                label = '提供元の合計' if fact['metric'] == 'etf_reported_total_usd' else '銘柄の合計（既知分）'
                doc.item(f'ETF照合不一致 {fact["observation_date"]}: {label} {fact["display"]}')
        flows = [index[f] for f in etf['flow_fact_ids']][-10:]
        for fact in flows:
            day = fact['observation_date']
            line = doc.item(f'ETF日次フロー {day}: {fact["display"]}（{STATUS_LABEL[fact["status"]]}）')
            items.append((day, fact, line, None))
        five = index.get(etf.get('five_day_fact_id') or '')
        if five:
            line = doc.item(f'ETF 5営業日合計（{five["coverage"]["note"] or "—"}）: {five["display"]}')
            lines['etf_5d'] = line
            items.append(('5営業日合計', five, line, None))
    else:
        lines['etf_date'] = doc.item('ETF: 欠測（Farsideを取得できず）')
    stable = st.get('stable', {})
    if stable.get('total_fact_id'):
        total = index[stable['total_fact_id']]
        d7, d30 = index[stable['change_7d']], index[stable['change_30d']]
        old = '' if stable.get('status') == 'ok' else f'（{STATUS_LABEL.get(total["status"], total["status"])}・採点に使わない）'
        lines['stable'] = doc.item(f'ステーブル供給（DefiLlama・全ステーブル合計）{old}: {total["display"]}、7日変化{d7["display"]}、'
                                   f'30日変化{d30["display"]}')
        for fact in F:
            if fact['metric'] == 'peg_deviation_bps' and fact['category'] == 'onchain':
                doc.item(f'{fact["instrument"].upper()} のドルからの乖離: {fact["display"]}')
    else:
        lines['stable'] = doc.item('ステーブル供給: 欠測（DefiLlamaを取得できず）')
    dom = index.get(st.get('dominance') or '')
    if dom:
        doc.item(f'BTCドミナンス: {dom["display"]}')
    for metric, label in (('fee_fastest', '最速'), ('fee_hour', '1時間以内')):
        fact = _pick(F, category='onchain', provider='mempool_space', metric=metric)
        if fact:
            doc.item(f'BTC送金手数料の目安（{label}、mempool.space）: {fact["display"]}')
    doc.item('取引所の入出金・主体ラベル: 未取得（無料で継続取得できる提供元がない）')
    doc.add('')
    if not figs.add({'id': 'etf_flows', 'type': 'diverging_bars', 'title': '米現物BTC ETFの日次フロー', 'unit': '百万USD',
                     'subtitle': f'百万USD / 米営業日・Farside速報（対象日 {etf.get("trade_date") or "欠測"}）',
                     'caption': '全銘柄合計の検算が通った日だけ。未公表日は出さず0にしない。速報値で後から訂正されうる。',
                     'source_label': 'Farside Investors', 'source_url': 'https://farside.co.uk/btc/'}, items):
        doc.item('図なし（ETFフロー）: 検算済みの日次合計がない')
        doc.add('')
    return lines


def section_macro(doc: Doc, facts: dict, ev: dict, index: dict, figs: Figures) -> dict:
    st = facts['state']
    doc.add(HEADINGS[6], '')
    macro = st.get('macro', {})
    if macro.get('status') not in (None, 'unknown') and macro.get('change_fact_ids'):
        dy, du = (index[f] for f in macro['change_fact_ids'])
        group = doc.item(f'金利・ドル群: {scoring.STATE_TEXT[macro["status"]]}（米2年利回り {dy["display"]}、米ドル広義指数（FRED） {du["display"]}）')
    else:
        group = doc.item(f'金利・ドル群: データ不足（{macro.get("reason", "inputs_missing")}）')
    for key, label in (('ust_2y', '米2年国債利回り'), ('ust_10y', '米10年国債利回り'), ('ust_real_10y', '米10年実質利回り')):
        if macro.get(key):
            doc.item(f'{label}（米財務省）: {index[macro[key]]["display"]}')
    fred_labels = {'DTWEXBGS': '米ドル広義指数（FRED）', 'VIXCLS': 'VIX（FRED）', 'NASDAQCOM': 'NASDAQ総合（現物指数終値）',
                   'SP500': 'S&P 500（現物指数終値）', 'WALCL': 'FRB総資産', 'WDTGAL': '財務省一般口座（TGA）',
                   'RRPONTSYD': '翌日物リバースレポ', 'M2SL': 'M2'}
    for sid, label in fred_labels.items():
        if macro.get(sid):
            change = index.get(macro.get(f'{sid}_change') or '')
            doc.item(f'{label}: {index[macro[sid]]["display"]}' + (f'、前回比{change["display"]}' if change else ''))
    if macro.get('net_liquidity'):
        doc.item(f'ネット流動性の代理値: {index[macro["net_liquidity"]]["display"]}')
    if not any(macro.get(k) for k in fred_labels):
        doc.item('FRED: 欠測（認証情報または取得の失敗。米ドル広義指数がないため金利・ドル群は判定しない）')
    cal = st.get('calendar', {})
    ok = lambda key: '確認済み' if cal.get(key) == 'ok' else '未確認'  # noqa: E731
    cal_line = doc.item(f'公式予定の確認: BLS（親が確認したキャッシュ、{cal.get("coverage_end")}まで）・Fed {ok("fed")}・BEA {ok("bea")}'
                        + (f'。{"、".join(cal.get("reasons") or [])}' if cal.get('reasons') else ''))
    items = []
    events = [e for e in st.get('events', []) if parse_time(e['start_at']) >= ev['as_of']]
    for e in events[:12]:
        window = e.get('stop_window')
        stop = f'、停止{hm(window["start_at"])}〜{hm(window["end_at"])} JST' if window else ''
        source = scoring.SOURCE_LABELS.get(e['source_id'], e['source_id'])
        status = scoring.STATUS_LABELS.get(e['status'], e['status'])
        line = doc.item(f'{jst(e["start_at"])} {e["name"]}（{source}、{status}、重要度{e["importance"]}{stop}）')
        if e['importance'] in ('high', 'critical') and len(items) < 8:
            detail = f'停止 {hm(window["start_at"])}〜{hm(window["end_at"])} JST' if window else '停止時間なし（表示のみ）'
            items.append((e['name'], None, line, {'date': parse_time(e['start_at']).astimezone(JST).strftime('%Y-%m-%d'),
                                                   'time': f'{hm(e["start_at"])} JST', 'detail': detail,
                                                   'tone': 'caution' if window else 'neutral'}))
    if not events:
        doc.item('今後8日の公式予定: 取得範囲内に掲載なし' if cal.get('next_24h') == 'ok' else '今後8日の公式予定: 未確認')
    risk = ev['risk_events_next_24h']
    doc.item('次24時間の高重要イベント: ' + ('、'.join(risk) if risk else 'なし（確認範囲内）'))
    doc.add('')
    if not figs.add({'id': 'event_timeline', 'type': 'timeline', 'title': '今後の高重要イベントと停止時間',
                     'subtitle': 'JST / 公式予定（FOMCの時刻は慣例）', 'caption': '停止時間は研究用の初期案。発表直前に公式予定を再確認する。',
                     'source_label': 'Fed・BEA・BLS（親確認キャッシュ）・Deribit',
                     'source_url': 'https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm'}, items):
        doc.item('図なし（イベント）: 今後8日に高重要の公式予定が見つからない、または予定が未確認')
        doc.add('')
    return {'group': group, 'calendar': cal_line}


def section_news(doc: Doc, facts: dict, analysis: dict, ev: dict, index: dict, figs: Figures) -> dict:
    st = facts['state']
    news = st.get('news', {})
    doc.add(HEADINGS[7], '')
    if news.get('status') == 'missing':
        line = doc.item('ニュース: 欠測（RSSを取得できず）')
        doc.add('')
        return {'news': line}
    sel = news.get('selection') or {}
    if sel.get('mode') == 'jev':
        method = f'Jevの判定・{md_safe(sel.get("model") or "モデル不明")}・応答{sel.get("elapsed_ms", "—")}ms'
    else:
        reason = {'disabled': '無効', 'timeout': '時間切れ', 'triage_failed': '失敗', 'triage_unavailable': '利用不可',
                  'triage_unreadable': '応答を読めず', 'no_candidates': '候補なし'}.get(sel.get('reason'), '利用不可')
        method = f'キーワード規則（Jev: {reason}）'
    model = ''
    line = doc.item(f'ニュースの取得: 一般RSS {news["feeds_ok"]["general"]}/5・公式RSS {news["feeds_ok"]["official"]}/3が成功。'
                    f'候補{news["candidate_count"]}件から{len(news["items"])}件を選別（{method}{model}）。'
                    f'期間 {jst(news["window_start"])}〜{jst(news["window_end"])}（{news["lookback_hours"]:g}時間）')
    doc.item('見出しは未確認の手掛かり。本文を取得できたものだけ「本文取得済み」。価格反応は公表時刻付近の動きで因果ではない'
             '（Binance BTCUSDT 1分足、USDT建て）')
    assessed = {a['news_id']: (i, a) for i, a in enumerate(analysis['news_assessments'])}
    counted = set(ev['groups']['btc_specific_event']['components'].get('news_ids', []))
    candidates = []
    for n in news['items']:
        doc.item(f'{n["id"]}（{n["publisher"]}、{jst(n["published_at"])}）: {n["title"]}'
                 f'（{CODE_VERIFICATION.get(n["code_verification"], n["code_verification"])}）')
        if n['id'] in assessed:
            i, a = assessed[n['id']]
            doc.item(f'{n["id"]} 親の評価: {GROUP[a["group_id"]]}・{IMPACT[a["impact"]]}・重要度{a["importance"]}・'
                     f'{VERIFICATION[a["verification"]]}' + ('・BTC固有イベント群に算入' if n['id'] in counted else '')
                     + '。' + _claim_text(analysis, f'news_assessments[{i}].interpretation', a['interpretation']))
        facts_r = [index[f] for f in n.get('fact_ids', [])]
        if facts_r:
            parts = dict(zip(('前5分', '後1分', '後5分'), facts_r))
            doc.item(f'{n["id"]} 公表前5分{parts["前5分"]["display"]}・公表後1分{parts["後1分"]["display"]}')
            after5 = parts.get('後5分')
            if after5 is not None:
                label = f'{n["publisher"]} {hm(n["published_at"])}'
                rline = doc.item(f'{n["id"]} 公表後5分の反応（{label} JST公表）: {after5["display"]}')
                priority = 0 if n['id'] in assessed and assessed[n['id']][1]['importance'] in ('critical', 'high') else 1
                candidates.append((priority, n['published_at'], (label, after5, rline, None)))
    doc.item('BTC固有イベント群に算入したニュース: ' + ('、'.join(sorted(counted)) or 'なし'))
    doc.add('')
    chosen = sorted(candidates, key=lambda c: (c[0], -parse_time(c[1]).timestamp()))[:3]
    if not figs.add({'id': 'news_reaction', 'type': 'diverging_bars', 'title': 'ニュース公表後5分の価格反応', 'unit': 'bp',
                     'subtitle': 'bp / Binance BTCUSDT 1分足', 'caption': '公表時刻付近の値動きで、ニュースが原因とは限らない。',
                     'source_label': 'Binance 公開API', 'source_url': 'https://www.binance.com/en/trade/BTC_USDT'},
                    [c[2] for c in chosen]):
        doc.item('図なし（価格反応）: 公表後5分の確定足がそろったニュースがない')
        doc.add('')
    return {'news': line}


def section_scenarios(doc: Doc, analysis: dict, ev: dict) -> dict:
    doc.add(HEADINGS[8], '')
    lines = {'reevaluate': [], 'limitations': []}
    for i, s in enumerate(analysis['scenarios']):
        doc.item(f'シナリオ {s["id"]} 条件: ' + '、'.join(f'{r}（{RULES[r]}）' for r in s['condition_rule_ids']))
        doc.item(f'シナリオ {s["id"]} 想定: ' + _claim_text(analysis, f'scenarios[{i}].expected_effect', s['expected_effect']))
        doc.item(f'シナリオ {s["id"]} 反対ケース: ' + _claim_text(analysis, f'scenarios[{i}].counter_case', s['counter_case']))
        doc.item(f'シナリオ {s["id"]} 無効化: ' + '、'.join(f'{r}（{RULES[r]}）' for r in s['invalidation_rule_ids']))
    for i, c in enumerate(analysis['counter_cases']):
        doc.item('反証: ' + _claim_text(analysis, f'counter_cases[{i}]', c))
    for i, r in enumerate(analysis['reevaluation_conditions']):
        lines['reevaluate'].append(doc.item(f'再評価 {r["rule_id"]}: {RULES[r["rule_id"]]}。'
                                            + _claim_text(analysis, f'reevaluation_conditions[{i}].explanation', r['explanation'])))
    for i, c in enumerate(analysis['limitations']):
        lines['limitations'].append(doc.item('制約: ' + _claim_text(analysis, f'limitations[{i}]', c)))
    for text in CODE_LIMITATIONS:
        lines['limitations'].append(doc.item('制約（コード）: ' + text))
    doc.add('')
    return lines


def _compare(previous: dict, machine: dict) -> list[str]:
    out = []
    pb, cb = previous.get('btc', {}), machine['btc']
    pairs = (('方向', pb.get('direction_status'), cb['direction_status']),
             ('候補方向', pb.get('candidate_direction'), cb['candidate_direction']),
             ('確度', pb.get('direction_strength'), cb['direction_strength']),
             ('band', (pb.get('score') or {}).get('band'), cb['score']['band']),
             ('整合度', (pb.get('score') or {}).get('total'), cb['score']['total']),
             ('NO-TRADE', previous.get('no_trade'), machine['no_trade']),
             ('trade_gate', (pb.get('trade_gate') or {}).get('status'), cb['trade_gate']['status']),
             ('ETF対象日', (pb.get('etf') or {}).get('trade_date'), cb['etf']['trade_date']))
    for label, before, after in pairs:
        out.append(f'{label}: {before} → {after}' + ('（変化なし）' if before == after else '（変化）'))
    return out


def weekly_review(root, as_of: datetime) -> list[str]:
    """Daily editions of the closing week, including withheld, rejected and failed runs (design 9)."""
    start = as_of - timedelta(days=7)
    counts, directions, seen = {}, {}, set()
    base = root / 'reports' / 'daily' / 'editions'
    for path in sorted(base.glob('*/*.edition.json')) if base.is_dir() else []:
        try:
            record = read_json(path)
            collected = parse_time(record['collected_at'])
        except (OSError, KeyError, ValueError, TypeError):
            continue
        if not start <= collected <= as_of:
            continue
        seen.add(record.get('work_path') or str(path.parent))
        status = record.get('review_status') if record.get('status') == 'succeeded_local' else record.get('status')
        counts[status] = counts.get(status, 0) + 1
        if record.get('review_status') == 'parent_passed':
            try:
                m = read_json(record['outputs']['json_path'])
                key = m['btc']['direction_status'] if m['btc']['direction_status'] == 'withheld' else m['btc']['candidate_direction']
                directions[key] = directions.get(key, 0) + 1
            except (OSError, KeyError, ValueError, TypeError):
                directions['読めない'] = directions.get('読めない', 0) + 1
    failed = 0
    log = root / 'logs' / 'runs.jsonl'
    if log.is_file():
        starts = set()
        for raw in log.read_text(encoding='utf-8').splitlines():
            try:
                rec = json.loads(raw)
                stamp = parse_time(rec.get('started_at') or rec.get('collected_at'))
            except (ValueError, TypeError):
                continue
            if rec.get('mode') == 'daily' and rec.get('status') in ('failed', 'needs_attention') \
                    and not rec.get('edition_path') and start <= stamp <= as_of:
                starts.add(rec.get('started_at'))
        failed = len(starts)
    lines = [f'週次振り返りの対象: {start.astimezone(JST).strftime("%Y-%m-%d %H:%M")}〜{jst(as_of)}のDaily版（保存済みの当時版だけ）',
             'Daily版の状態: ' + ('、'.join(f'{k}={v}' for k, v in sorted(counts.items())) or 'なし'),
             '親レビュー通過版の方向: ' + ('、'.join(f'{k}={v}' for k, v in sorted(directions.items())) or 'なし'),
             f'版を作る前に失敗・要確認になった実行: {failed}件',
             '値動きとの一致は因果や売買収益の証明ではない。当時未公開のデータで補完しない']
    return lines


def section_previous(doc: Doc, ctx, machine: dict, as_of: datetime) -> None:
    doc.add(HEADINGS[9], '')
    previous = None
    for p in ctx.previous_editions:
        try:
            previous = read_json(p['machine_path'])
            prev_info = p
            break
        except (OSError, KeyError, ValueError):
            continue
    if previous is None:
        doc.item('前回版: なし（このジョブの親レビュー通過版がまだない）')
    else:
        doc.item(f'前回版: {jst(prev_info["collected_at"])}（{prev_info.get("session_slot")}）。前回の方向を一致点として加点しない')
        for line in _compare(previous, machine):
            doc.item(line)
    if ctx.mode == 'weekly':
        for line in weekly_review(ctx.root, as_of):
            doc.item(line)
    doc.add('')


def section_facts(doc: Doc, facts: dict, provenance: dict) -> None:
    doc.add(HEADINGS[10], '')
    for key in ('collector_version', 'feature_version', 'policy_hash', 'input_manifest_sha256', 'analysis_sha256',
                'source_catalog_sha256'):
        doc.item(f'{key}: {provenance[key]}')
    doc.add('')
    rows = []
    for f in facts['facts']:
        value = 'null' if f['value'] is None else repr(f['value'])
        if f['value'] is None:
            value = f'null ({f["missing_reason"]})'
        when = f['observed_at'] or f['observation_date'] or f['period_end'] or '—'
        rows.append([f['fact_id'], value, f['unit'], f['status'], when, f['published_at'], f['retrieved_at'],
                     f['source_id'], f['method_id']])
    doc.table(['fact_id', 'value', 'unit', 'status', 'observed_at/date', 'published_at', 'retrieved_at', 'source_id',
               'method_id'], rows)


# ----------------------------------------------------------------- machine.json

def _claim(analysis: dict, where: str, claim: dict) -> dict:
    return {'text': analysis['expanded'][where], 'fact_ids': list(claim['fact_ids']), 'claim_kind': claim['claim_kind']}


def _rule(rule_id: str, fact_ids: list) -> dict:
    return {'rule_id': rule_id, 'fact_ids': list(fact_ids),
            'explanation': {'text': RULES[rule_id], 'fact_ids': [], 'claim_kind': 'limitation'}}


def _window(w: dict) -> dict:
    return {'start_at': w['start_at'], 'end_at': w['end_at'], 'event_ids': list(w['event_ids']), 'rule_id': w['rule_id']}


def machine_parts(facts: dict, analysis: dict, ev: dict, fresh: dict, dest: dict, ctx, analysis_sha: str,
                  collection: dict) -> tuple[dict, dict]:
    from btc.collect import source_catalog_sha256
    st = facts['state']
    gate = ev['trade_gate']
    core = {'bias': ev['bias'], 'no_trade': ev['no_trade'], 'no_trade_reason': ev['no_trade_reason'],
            'risk_events_next_24h': list(ev['risk_events_next_24h']),
            'positioning_summary': analysis['expanded']['positioning_summary'], 'confidence': ev['confidence']}
    choice = dest['choice']
    if choice['kind'] == 'none' or dest['status'] == 'expired':
        prefix = '目的地は失効（根拠の観測の有効期限を完成時刻が過ぎた）。' if dest['status'] == 'expired' else ''
        destination = {'side': 'none', 'kind': 'none', 'level_fact_ids': [], 'valid_until': None,
                       'rationale': {**_claim(analysis, 'destination_choice.rationale', choice['rationale']),
                                     'text': prefix + analysis['expanded']['destination_choice.rationale']},
                       'counter_case': _claim(analysis, 'destination_choice.counter_case', choice['counter_case']),
                       'invalidation_rules': [_rule(r, []) for r in choice['invalidation_rule_ids']]}
    else:
        destination = {'side': choice['side'], 'kind': choice['kind'], 'level_fact_ids': list(choice['level_fact_ids']),
                       'valid_until': zs(dest['valid_until']) if dest['valid_until'] else None,
                       'rationale': _claim(analysis, 'destination_choice.rationale', choice['rationale']),
                       'counter_case': _claim(analysis, 'destination_choice.counter_case', choice['counter_case']),
                       'invalidation_rules': [_rule(r, choice['level_fact_ids']) for r in choice['invalidation_rule_ids']]}
    by_cat = lambda *cats: [f['fact_id'] for f in facts['facts'] if f['category'] in cats]  # noqa: E731
    news_items = {n['id']: n for n in st.get('news', {}).get('items', [])}
    news = []
    for i, a in enumerate(analysis['news_assessments']):
        n = news_items[a['news_id']]
        reaction = dict(n['reaction'] or {'status': 'missing', 'market_id': None, 'reaction_basis': 'unknown',
                                          'event_at': None, 'baseline_at': None, 'return_before_5m_bps': None,
                                          'return_after_1m_bps': None, 'return_after_5m_bps': None,
                                          'timing_error_bound_seconds': 60, 'confounded': False,
                                          'confound_event_ids': [], 'note': '反応を計測していない（件数上限）'})
        for key in ('raw_sha256', 'bars', 'bars_required', 'bars_missing', 'bars_duplicated'):
            reaction.pop(key, None)  # kept in facts/data (recomputable); not part of the machine reaction record
        reaction['fact_ids'] = list(n.get('fact_ids', []))
        news.append({'id': n['id'], 'event_cluster_id': n['event_cluster_id'], 'title': n['title'], 'url': n['url'],
                     'published_at': n['published_at'], 'first_seen_at': n['first_seen_at'] or n['published_at'],
                     'verification': a['verification'], 'group_id': a['group_id'], 'impact': a['impact'],
                     'importance': a['importance'], 'fact_ids': list(dict.fromkeys(a['fact_ids'] + n.get('fact_ids', []))),
                     'reaction': reaction})
    venues = [v['venue'] for v in st.get('derivatives', {}).get('venues', [])]
    etf = st.get('etf', {})
    etf_status = {'ok': 'provisional', 'lagged': 'partial', 'incomplete': 'partial', 'conflict': 'conflict',
                  'stale': 'stale'}.get(etf.get('status'), 'missing')
    options = st.get('options', {})
    expiry_ids = [x for e in options.get('expiries', []) for x in (e['share_fact_id'], e['pcr_fact_id'], e['max_pain_fact_id'])]
    groups = {k: {'state': g['state'], 'direction': g['direction'], 'reason_code': g['reason_code'],
                  'fact_ids': list(g['fact_ids']), 'components': g.get('components', {})} for k, g in ev['groups'].items()}
    scope = ['デリバティブはBinance・Bybit・OKXの線形BTC無期限のみ', 'オプションはDeribit BTC inverse optionsのみ',
             'ETFはFarside速報（全銘柄合計の検算済み日だけ）', 'CMEの満期・取引時間は未確認']
    btc = {
        'direction_status': ev['direction_status'],
        'direction_reason_codes': list(ev['direction_reason_codes']),
        'candidate_direction': ev['candidate_direction'],
        'confidence_kind': 'uncalibrated_ordinal',
        'score': {'policy_version': scoring.POLICY_VERSION,
                  'items': [{k: i[k] for k in ('key', 'points', 'status', 'reason_code', 'fact_ids', 'group_id')}
                            for i in ev['items']],
                  'total': ev['total'], 'band': ev['band'], 'coverage_ratio': ev['coverage_ratio']},
        'trade_gate': {'status': gate['status'], 'reason_codes': list(dict.fromkeys(gate['reason_codes'])),
                       'active_windows': [_window(w) for w in gate['active_windows']],
                       'upcoming_windows': [_window(w) for w in gate['upcoming_windows']],
                       'recheck_at': zs(gate['recheck_at'])},
        'source_health': st['source_health'],
        'facts': facts['facts'],
        'coverage': {'required': 8, 'present': ev['coverage_present'],
                     'missing': [k for k, v in ev['bundles'].items() if not v], 'scope_notes': scope},
        'reference_price': {'fact_id': st['reference_price']['fact_id'],
                            'venue_fact_ids': list(st['reference_price']['venue_fact_ids']),
                            'status': st['reference_price']['status']},
        'etf': {'flow_fact_ids': list(etf.get('flow_fact_ids', [])), 'holdings_fact_ids': [],
                'universe': list(etf.get('universe', [])), 'trade_date': etf.get('trade_date'), 'status': etf_status},
        'derivatives': {'fact_ids': by_cat('derivatives'), 'venue_scope': venues, 'oi_convention': 'single_sided',
                        'coverage': {'included_components': venues, 'expected_components': ['binance', 'bybit', 'okx'],
                                     'observed_count': len(venues), 'expected_count': 3,
                                     'note': '線形BTC無期限・片側OI'}},
        'options': {'fact_ids': by_cat('options'), 'underlying_scope': options.get('scope', 'Deribit BTC inverse options のみ'),
                    'expiry_fact_ids': expiry_ids, 'skew_method_id': 'skew_25d_bracket_linear'},
        'liquidity': {'book_fact_ids': list(st.get('liquidity', {}).get('book_fact_ids', [])), 'liquidation_fact_ids': [],
                      'heatmap_status': 'unavailable', 'destination': destination},
        'onchain': {'fact_ids': by_cat('onchain'), 'exchange_flow_status': 'missing', 'entity_label_status': 'missing'},
        'macro': {'fact_ids': list(st.get('macro', {}).get('fact_ids', [])) + list(st.get('macro', {}).get('change_fact_ids', [])),
                  'window': st.get('macro', {}).get('window', 'unknown'),
                  'proxy_labels': list(st.get('macro', {}).get('proxy_labels', []))},
        'events': [{k: e[k] for k in ('id', 'name', 'category', 'start_at', 'end_at', 'timezone', 'importance', 'status',
                                      'source_id', 'source_url', 'published_at', 'verified_at', 'stop_window')}
                   for e in st.get('events', [])],
        'news': news,
        'analysis': {
            'thesis': _claim(analysis, 'thesis', analysis['thesis']),
            'three_domains': {k: _claim(analysis, f'three_domains.{k}', analysis['three_domains'][k])
                              for k in ('liquidity', 'positioning', 'bias')},
            'scenarios': [{'id': s['id'], 'condition_rule_ids': list(s['condition_rule_ids']), 'fact_ids': list(s['fact_ids']),
                           'expected_effect': _claim(analysis, f'scenarios[{i}].expected_effect', s['expected_effect']),
                           'counter_case': _claim(analysis, f'scenarios[{i}].counter_case', s['counter_case']),
                           'invalidation_rule_ids': list(s['invalidation_rule_ids'])}
                          for i, s in enumerate(analysis['scenarios'])],
            'counter_cases': [_claim(analysis, f'counter_cases[{i}]', c) for i, c in enumerate(analysis['counter_cases'])],
            'reevaluation_conditions': [{'rule_id': r['rule_id'], 'fact_ids': list(r['fact_ids']),
                                         'explanation': _claim(analysis, f'reevaluation_conditions[{i}].explanation',
                                                               r['explanation'])}
                                        for i, r in enumerate(analysis['reevaluation_conditions'])],
            'limitations': [_claim(analysis, f'limitations[{i}]', c) for i, c in enumerate(analysis['limitations'])],
        },
        'provenance': {'collector_version': collection.get('collector_version') or 'unknown',
                       'feature_version': FEATURE_VERSION, 'policy_hash': scoring.POLICY_HASH,
                       'input_manifest_sha256': facts['input_manifest_sha256'], 'analysis_sha256': analysis_sha,
                       'source_catalog_sha256': source_catalog_sha256()},
        'direction_strength': ev['direction_strength'],
        'direction_label': direction_label(ev),
        'groups': groups,
        'confidence_caps': list(ev['caps']),
        'freshness': {k: fresh[k] for k in ('as_of_check', 'finalize_checked_at', 'expired_at_finalize', 'note')},
        'terms_restricted': [dict(t) for t in st.get('terms_restricted', [])],
    }
    generated = zs(ctx.generated_at)
    extensions = {'schema_version': SCHEMA_VERSION, 'asset': SYMBOL, 'report_kind': report_kind(ctx.mode, ctx.session_slot),
                  'edition_id': ctx.work_dir.name, 'available_at': generated, 'valid_until': zs(ev['valid_until']),
                  'research_only': True, 'execution_enabled': False, 'btc': btc}
    return core, extensions


# ----------------------------------------------------------------- entry

def _window_label(w: dict, events: dict) -> str:
    names = '・'.join(events[i]['name'] for i in w['event_ids'] if i in events)
    start = parse_time(w['start_at']).astimezone(JST)
    return f'{start.strftime("%m-%d %H:%M")}〜{hm(w["end_at"])} JST（{names}）'


def build(analysis: dict, facts: dict, collection: dict, ctx) -> ReportParts:
    finalize_at = parse_time(ctx.generated_at).astimezone(UTC)
    index = {f['fact_id']: f for f in facts['facts']}
    ev = scoring.evaluate(facts, mode=ctx.mode, assessments=analysis['news_assessments'], finalize_at=finalize_at,
                          recoveries=analysis.get('incident_recoveries') or [])
    events = {e['id']: e for e in facts['state'].get('events', [])}
    for key in ('active_windows', 'upcoming_windows'):
        for w in ev['trade_gate'][key]:
            w['label'] = _window_label(w, events)
    gate = ev['trade_gate']
    recheck = parse_time(gate['recheck_at'])
    if any(parse_time(w['end_at']) == recheck for w in gate['active_windows']):
        ev['recheck_reason'] = '停止時間の終了'
    elif any(parse_time(w['start_at']) == recheck for w in gate['upcoming_windows']):
        ev['recheck_reason'] = '次の停止時間の開始'
    else:
        ev['recheck_reason'] = '判断の有効期限'
    ev['calendar_next_24h'] = facts['state'].get('calendar', {}).get('next_24h')
    fresh = finalize_freshness(facts, analysis, finalize_at, ev['hard_invalid'])
    dest = destination(analysis, index, finalize_at)
    raw = {k: v for k, v in analysis.items() if k != 'expanded'}
    analysis_sha = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    core, extensions = machine_parts(facts, analysis, ev, fresh, dest, ctx, analysis_sha, collection)
    yaml_values = yaml_mapping(core, extensions, mode=ctx.mode, session_slot=ctx.session_slot, as_of=facts['as_of'],
                               collected_at=facts['collected_at'], generated_at=zs(ctx.generated_at))
    collected_jst = parse_time(facts['collected_at']).astimezone(JST)
    doc = Doc()
    doc.add(f'# BTCUSD チャート外分析 {ctx.mode.capitalize()} — {collected_jst.strftime("%Y-%m-%d %H:%M")} JST', '')
    figs = Figures(index)
    s0 = section_decision(doc, ev, analysis, yaml_values)
    s1 = section_coverage(doc, facts, ev, fresh, index)
    s2 = section_score(doc, facts, ev, index)
    s3 = section_positioning(doc, facts, analysis, index, figs)
    s4 = section_liquidity(doc, facts, analysis, index, figs, dest, finalize_at)
    s5 = section_spot(doc, facts, analysis, index, figs)
    s6 = section_macro(doc, facts, ev, index, figs)
    section_news(doc, facts, analysis, ev, index, figs)
    s8 = section_scenarios(doc, analysis, ev)
    machine_preview = {**core, **extensions, 'no_trade': ev['no_trade']}
    section_previous(doc, ctx, machine_preview, finalize_at)
    section_facts(doc, facts, extensions['btc']['provenance'])
    markdown = doc.text()
    # Figures appear in reading order: ETF, derivatives, depth, distribution, events, news.
    order = ['etf_flows', 'derivatives_bias_funding', 'derivatives_bias_oi', 'observed_depth',
             'positioning_distribution', 'event_timeline', 'news_reaction']
    figures = sorted(figs.figures, key=lambda f: order.index(f['id']))
    ids = [f['id'] for f in figures]
    missing_figures = [x for x in order if x not in ids]
    metrics = []
    ref = index.get(facts['state'].get('reference_price', {}).get('fact_id') or '')
    if ref and ref['value'] is not None:
        metrics.append({'label': '参照価格', 'value': ref['display'], 'context': s1['reference'].split('（', 1)[1].rstrip('）'),
                        'source_quote': s1['reference']})
    if s5.get('etf_5d'):
        five = index[facts['state']['etf']['five_day_fact_id']]
        metrics.append({'label': 'ETF 5営業日合計', 'value': five['display'], 'context': 'Farside速報・全銘柄合計',
                        'source_quote': s5['etf_5d']})
    metrics.append({'label': '採点入力の充足', 'value': f'{ev["coverage_present"]}/8',
                    'context': '入力の有無で、内容の信頼度ではない', 'source_quote': s1['coverage']})
    metrics.append({'label': '整合度', 'value': f'{ev["total"]}点・{ev["band"]}', 'context': '未校正の序数で的中率ではない',
                    'source_quote': s0['score']})
    status = (('方向付与保留' if ev['direction_status'] == 'withheld' else direction_label(ev).removeprefix('BTCは'))
              + ('・NO-TRADE' if ev['no_trade'] else '・停止条件なし'))
    summary = {
        'eyebrow': f'BTCUSD {ctx.mode.upper()}', 'short_title': f'BTCUSD チャート外分析 {ctx.mode.capitalize()}',
        'report_date': collected_jst.strftime('%Y-%m-%d'), 'status': status,
        'conclusion': {'title': s0['label'], 'text': s0['verdict'], 'source_quote': s0['verdict']},
        'metrics': metrics,
        'conditions': [{'title': '停止時間', 'text': s0['stop'], 'source_quote': s0['stop']},
                       {'title': '次に確認すること', 'text': s0['recheck'], 'source_quote': s0['recheck']}]
        + [{'title': '再評価の条件', 'text': x, 'source_quote': x} for x in s8['reevaluate'][:2]],
        'evidence': [{'title': scoring.GROUP_LABELS[k], 'text': s2[k], 'source_quote': s2[k]} for k in scoring.GROUPS]
        + [{'title': '流動性', 'text': s4['domain'], 'source_quote': s4['domain']},
           {'title': '比率・ポジショニング', 'text': s3['domain'], 'source_quote': s3['domain']},
           {'title': '目的地', 'text': s4['destination'], 'source_quote': s4['destination']},
           {'title': '金利・ドル', 'text': s6['group'], 'source_quote': s6['group']}],
        'limitations': [{'title': '清算ヒートマップ', 'text': s4['fixed'], 'source_quote': s4['fixed']}]
        + [{'title': '制約', 'text': x, 'source_quote': x} for x in s8['limitations'][:4]],
        'source_note': '数値はコードが収集したファクトから展開。出典・観測時刻・欠測は全文の第1章と第10章を参照。',
    }
    return ReportParts(markdown=markdown, summary=summary, figures=figures, bindings=figs.bindings, machine_core=core,
                       machine_extensions=extensions, required_figures=ids,
                       missing=[f'figure:{x}' for x in missing_figures] + [f'bundle:{k}' for k, v in ev['bundles'].items() if not v],
                       limitations=list(CODE_LIMITATIONS))


def yaml_mapping(core: dict, ext: dict, *, mode: str, session_slot: str, as_of: str, collected_at: str,
                 generated_at: str) -> dict:
    btc = ext['btc']
    return {'schema_version': ext['schema_version'], 'asset': ext['asset'], 'mode': mode, 'session_slot': session_slot,
            'edition': ext['edition_id'], 'as_of': as_of, 'collected_at': collected_at, 'generated_at': generated_at,
            'valid_until': ext['valid_until'], 'bias': core['bias'], 'direction_status': btc['direction_status'],
            'candidate_direction': btc['candidate_direction'], 'direction_strength': btc['direction_strength'],
            'confidence': core['confidence'], 'confidence_band': btc['score']['band'], 'score_total': btc['score']['total'],
            'no_trade': core['no_trade'], 'no_trade_reason': core['no_trade_reason'], 'trade_gate': btc['trade_gate']['status'],
            'coverage_present': btc['coverage']['present'], 'coverage_required': btc['coverage']['required']}


def machine_yaml(machine: dict) -> dict:
    btc = machine['btc']
    return {'schema_version': machine['schema_version'], 'asset': machine['asset'], 'mode': machine['mode'],
            'session_slot': machine['session_slot'], 'edition': machine['edition_id'], 'as_of': machine['as_of'],
            'collected_at': machine['collected_at'], 'generated_at': machine['generated_at'],
            'valid_until': machine['valid_until'], 'bias': machine['bias'], 'direction_status': btc['direction_status'],
            'candidate_direction': btc['candidate_direction'], 'direction_strength': btc['direction_strength'],
            'confidence': machine['confidence'], 'confidence_band': btc['score']['band'],
            'score_total': btc['score']['total'], 'no_trade': machine['no_trade'],
            'no_trade_reason': machine['no_trade_reason'], 'trade_gate': btc['trade_gate']['status'],
            'coverage_present': btc['coverage']['present'], 'coverage_required': btc['coverage']['required']}


class ReportError(ValueError):
    pass


def check_markdown(markdown: str, machine: dict) -> None:
    """MD structure and the decision block must match machine.json (P9-4); mismatch rejects the render."""
    import yaml
    lines = markdown.split('\n')
    kind = machine['mode'].capitalize()
    if not re.fullmatch(rf'# BTCUSD チャート外分析 {kind} — \d{{4}}-\d{{2}}-\d{{2}} \d{{2}}:\d{{2}} JST', lines[0]):
        raise ReportError('markdown_h1_invalid')
    if lines[0].startswith('---') or markdown.startswith('---'):
        raise ReportError('front_matter_not_allowed')
    headings = [line for line in lines if line.startswith('## ')]
    if tuple(headings) != HEADINGS:
        raise ReportError('markdown_headings_invalid')
    start = lines.index(HEADINGS[0])
    if lines[start + 1] != '' or lines[start + 2] != '```yaml':
        raise ReportError('decision_block_missing')
    end = lines.index('```', start + 3)
    block = lines[start + 3:end]
    keys = [line.split(':', 1)[0] for line in block]
    if tuple(keys) != YAML_KEYS:
        raise ReportError('decision_block_keys_invalid')
    parsed = yaml.safe_load('\n'.join(block))
    expected = machine_yaml(machine)
    for key in YAML_KEYS:
        a, b = parsed[key], expected[key]
        if isinstance(a, bool) or isinstance(b, bool) or a is None or b is None or isinstance(a, str) or isinstance(b, str):
            same = a == b and type(a) is type(b)
        else:
            same = float(a) == float(b)
        if not same:
            raise ReportError(f'decision_block_mismatch:{key}')
    if LIQUIDITY_NOTE not in markdown:
        raise ReportError('liquidity_note_missing')
