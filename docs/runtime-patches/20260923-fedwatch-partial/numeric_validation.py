"""Source-preserving rate classification and explicitly anchored comparisons."""
from decimal import Decimal, InvalidOperation
import re


def number(value):
    if isinstance(value, bool) or value is None:
        raise ValueError('Missing numeric observation')
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError('Invalid numeric observation') from None
    if not result.is_finite():
        raise ValueError('Non-finite observation')
    return result


def band(value):
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*[-–−]\s*(\d+(?:\.\d+)?)\s*%?\s*', str(value))
    if not match:
        raise ValueError('Explicit rate range required')
    low, high = map(number, match.groups())
    if high <= low:
        raise ValueError('Rate range not ordered')
    return low, high


def fedwatch_facts(raw, reference=None):
    """Never infer current policy from the lowest probability bucket."""
    rows, conflicts, base = [], [], None
    unavailable = raw.get('unavailable_target_rates') or []
    partial = raw.get('completeness') == 'partial' or bool(unavailable)
    if reference:
        if not all(reference.get(k) for k in ('range', 'source_url', 'checked_at', 'effective_at', 'valid_for_date')):
            raise ValueError('Incomplete policy reference')
        if not reference['source_url'].startswith('https://www.federalreserve.gov/'):
            raise ValueError('Official policy reference required')
        base = band(reference['range'])
    for item in raw.get('target_rates') or []:
        low, high = band(item['range'])
        current = number(item['current'])
        if not 0 <= current <= 100:
            raise ValueError('Probability outside 0..100')
        shift = (low - base[0]) * 100 if base else None
        if base and high - base[1] != low - base[0]:
            raise ValueError('Rate range widths differ')
        kind = 'unverified' if shift is None else 'hold' if shift == 0 else 'hike' if shift > 0 else 'cut'
        row = {'range': item['range'], 'current_pct': float(current), 'classification': kind,
               'change_bp': float(shift) if shift is not None else None, 'comparisons': {}}
        for period in ('prev_day', 'prev_week'):
            if item.get(period) is None:
                row['comparisons'][period] = None
                continue
            prior = number(item[period])
            if not 0 <= prior <= 100:
                raise ValueError('Prior probability outside 0..100')
            delta = float(current - prior)
            row['comparisons'][period] = {'prior_pct': float(prior), 'delta_pp': delta, 'basis': 'provider_target_rates'}
            history = ((raw.get('deltas') or {}).get(period) or {})
            other = (history.get('by_range') or {}).get(item['range'])
            if other is not None and abs(number(other) - number(delta)) > Decimal('0.05'):
                conflicts.append({'range': item['range'], 'period': period, 'provider_delta_pp': delta,
                                  'history_delta_pp': other, 'history_basis': history.get('source'),
                                  'resolution': 'separate_anchors; use_provider_comparison_only'})
        key = ('hold_pct' if shift == 0 else f'{kind}_{abs(int(shift))}bp_pct') if shift is not None else None
        if key and raw.get(key) is not None and number(raw[key]) != current:
            conflicts.append({'field': key, 'raw_value': raw[key], 'recomputed_value': float(current),
                              'resolution': 'classify_against_official_current_policy'})
        rows.append(row)

    for item in unavailable:
        low, high = band(item['range'])
        if item.get('current') is not None:
            raise ValueError('Unavailable FedWatch row must have missing current probability')
        shift = (low - base[0]) * 100 if base else None
        if base and high - base[1] != low - base[0]:
            raise ValueError('Rate range widths differ')
        kind = 'unverified' if shift is None else 'hold' if shift == 0 else 'hike' if shift > 0 else 'cut'
        comparisons = {}
        for period in ('prev_day', 'prev_week'):
            prior = item.get(period)
            if prior is None:
                comparisons[period] = None
                continue
            prior_number = number(prior)
            if not 0 <= prior_number <= 100:
                raise ValueError('Prior probability outside 0..100')
            comparisons[period] = {
                'prior_pct': float(prior_number),
                'delta_pp': None,
                'basis': 'provider_target_rates',
                'current_unavailable': True,
            }
        rows.append({'range': item['range'], 'current_pct': None,
                     'classification': kind, 'change_bp': float(shift) if shift is not None else None,
                     'comparisons': comparisons, 'availability': 'unavailable'})

    known_current_total = sum(number(r['current_pct']) for r in rows if r['current_pct'] is not None)
    if not partial and rows and abs(known_current_total - 100) > Decimal('0.2'):
        raise ValueError('Probability buckets do not total 100 percent')
    distribution_status = 'partial' if partial else 'complete' if rows else 'unavailable'
    return {'status': 'partial' if partial else 'classified' if base and rows else 'unverified',
            'distribution_status': distribution_status,
            'known_current_total_pct': float(known_current_total),
            'missing_ranges': [r['range'] for r in rows if r['current_pct'] is None],
            'policy_reference': reference,
            'source': raw.get('source'), 'recorded_at': raw.get('timestamp'), 'market_observed_at': raw.get('as_of_date'),
            'rows': rows, 'conflicts': conflicts, 'limitation': 'Arithmetic classification is not independent verification of probability provenance.'}


def facts_markdown(facts):
    lines = ['\n\n## 政策確率の照合', '',
             '提供元のレンジ別確率を現行政策金利に対して分類し、同じ提供表の前日・前週欄から差を計算します。独自履歴とは比較基準を分けます。', '',
             '| 目標レンジ（%） | 分類 | 確率 | 提供表の前日差 | 提供表の前週差 |', '|---|---|---|---|---|']
    if facts.get('distribution_status') == 'partial':
        lines[2] = ('提供元のレンジ別確率を現行政策金利に対して照合します。'
                    '一部レンジのcurrent確率が欠測しているため、欠測を0%補完せず、'
                    '全体合計と政策方向別確率は未確定です。')
    for r in facts['rows']:
        kind = {'hold': '据置', 'hike': '利上げ', 'cut': '利下げ', 'unverified': '分類保留'}[r['classification']]
        if r['change_bp']:
            kind += f" {r['change_bp']:+g}bp"
        delta = []
        for period in ('prev_day', 'prev_week'):
            comparison = r['comparisons'][period]
            if comparison is None:
                delta.append('取得不可')
            elif comparison.get('delta_pp') is None:
                delta.append(f"差分算出不可（提供表 {comparison['prior_pct']:.1f}%）")
            else:
                delta.append(f"{comparison['delta_pp']:+.1f}pp")
        current = f"{r['current_pct']:.1f}%" if r['current_pct'] is not None else '取得不可'
        if r['current_pct'] is None:
            kind = '確率欠測'
        lines.append(f"| {r['range']} | {kind} | {current} | {delta[0]} | {delta[1]} |")
    if facts['policy_reference']:
        ref = facts['policy_reference']
        lines += ['', f"現行政策金利 {ref['range']}%。[Fed声明]({ref['source_url']})。発効日 {ref['effective_at']}、この版の照合日時 {ref['checked_at']}。"]
    else:
        lines += ['', '現行政策金利の照合がないため、政策変更の分類は保留します。']
    lines += ['', '確率の市場観測時刻は未確認です。分類の再計算を、確率原票の独立照合や売買方向の確定とは扱いません。']
    if facts['conflicts']:
        lines += ['', f"元入力との分類・比較基準の相違 {len(facts['conflicts'])} 件を numeric.json に記録しました。原データは保持しています。"]
    return '\n'.join(lines) + '\n'
