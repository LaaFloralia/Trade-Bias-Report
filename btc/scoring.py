"""Three groups, direction strength, fixed 8-item score, trade gate (design 7; P9).

Policy ``btc-offchart-1.0``. Code owns every number here; the parent only
selects which confirmed BTC-specific news clusters belong to the
``btc_specific_event`` group (validated in btc/analysis.py).

P9 overrides: strength 1 group = 低, 2 = 中, 3 = 高; any opposite group =
保留（材料混在）; no directional group = 保留 with 根拠なし（閾値未満） or
データ不足（unknown）; hard invalid / data_hold = 保留. bias = sign × 0.25 /
0.5 / 0.75; confidence High .75 / Med .60 / Med-cautious .40 / Low .20 with
caps, and the band is lowered to match a capped confidence.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json

from btc.common import JST, parse_time

UTC = timezone.utc
POLICY_VERSION = 'btc-offchart-1.0'
GROUPS = ('macro', 'spot_demand', 'btc_specific_event')
GROUP_LABELS = {'macro': 'マクロ（金利・ドル）', 'spot_demand': '現物需要（ETF・ステーブル供給）',
                'btc_specific_event': 'BTC固有の一次イベント'}
ITEM_KEYS = ('macro_alignment', 'etf_alignment', 'btc_event_alignment', 'stable_supply_alignment',
             'leverage_crowding', 'options_tail_risk', 'data_quality', 'event_risk')
ITEM_LABELS = {'macro_alignment': '金利・ドル群', 'etf_alignment': 'ETF需要', 'btc_event_alignment': 'BTC固有の一次イベント',
               'stable_supply_alignment': 'ステーブル供給', 'leverage_crowding': 'レバレッジ集中',
               'options_tail_risk': 'オプションの反対側保険', 'data_quality': 'データの充足と矛盾', 'event_risk': 'イベント近接'}
ITEM_GROUPS = {'macro_alignment': 'macro', 'etf_alignment': 'spot_demand', 'btc_event_alignment': 'btc_specific_event',
               'stable_supply_alignment': 'spot_demand', 'leverage_crowding': 'leverage', 'options_tail_risk': 'options',
               'data_quality': 'quality', 'event_risk': 'event_risk'}
BUNDLES = ('price', 'calendar', 'macro', 'etf', 'derivatives', 'options', 'news', 'stable_supply')
BUNDLE_LABELS = {'price': '参照価格', 'calendar': '公式カレンダー', 'macro': 'マクロ', 'etf': 'ETF',
                 'derivatives': 'デリバティブ', 'options': 'オプション', 'news': 'ニュース', 'stable_supply': 'ステーブル供給'}
BAND_CONFIDENCE = {'High': 0.75, 'Med': 0.60, 'Med-cautious': 0.40, 'Low': 0.20}
BAND_ORDER = ('High', 'Med', 'Med-cautious', 'Low')
STRENGTH_BIAS = {'低': 0.25, '中': 0.5, '高': 0.75}
ETF_THRESHOLD = 500e6
ETF_LAST_DAY_OPPOSITE = 250e6
FUNDING_CROWDED = 0.0003   # 0.03 % per 8 h
OI_CROWDED_PCT = 5.0
SKEW_PP = 5.0
EVENT_WINDOW_HOURS = {'daily': 36, 'weekly': 168}
VALIDITY_HOURS = {'daily': 4, 'weekly': 24}
SIGN = {'supportive': 1, 'adverse': -1}
POLICY = {
    'version': POLICY_VERSION, 'macro': {'ust2y_bp': 5, 'usd_pct': 0.30},
    'etf': {'five_day_usd': ETF_THRESHOLD, 'last_day_opposite_usd': ETF_LAST_DAY_OPPOSITE},
    'stable': {'d7_pct': 0.5, 'd30_pct': 1.0}, 'leverage': {'funding_8h': FUNDING_CROWDED, 'percentile': [90, 10],
                                                              'oi_24h_pct': OI_CROWDED_PCT, 'venues': 2},
    'options': {'skew_pp': SKEW_PP, 'percentile': [90, 10]}, 'bands': {'High': [5, 3], 'Med': [4, 2], 'Med-cautious': [3, 2]},
    'confidence': BAND_CONFIDENCE, 'caps': {'withheld': 0.20, 'provisional_etf_only': 0.60, 'manual_calendar_cache': 0.60,
                                            'unassessed_item_5_or_6': 0.60},
    'stop_windows': {'macro_release': [-15, 30], 'fomc_statement': [-30, 90], 'fomc_press': [-15, 'statement+120'],
                     'deribit_big_expiry': '16:30-17:30 JST, share>=20%'},
    'validity_hours': VALIDITY_HOURS,
}
POLICY_HASH = hashlib.sha256(json.dumps(POLICY, sort_keys=True).encode()).hexdigest()


def _f(index: dict, fact_id: str | None) -> dict | None:
    return index.get(fact_id) if fact_id else None


SCORABLE = ('ok', 'provisional')


def scorable(fact: dict | None) -> bool:
    """A fact may enter scoring only when ok/provisional, not stale at as_of, and valued (R-02).

    partial / stale / conflict / missing facts stay displayable but are unknown for scoring.
    """
    return bool(fact) and fact['status'] in SCORABLE and not fact.get('stale') and fact.get('value') is not None


def _val(index: dict, fact_id: str | None):
    fact = _f(index, fact_id)
    return fact['value'] if scorable(fact) else None


def bundles(facts: dict) -> dict:
    """Normal coverage from the state and expiry of each bundle's required facts (R-01, R-02)."""
    st = facts['state']
    index = {f['fact_id']: f for f in facts['facts']}
    news = st.get('news', {})
    feeds_ok = news.get('feeds_ok') or {}
    stable = st.get('stable', {})
    ref = st.get('reference_price', {})
    venues = st.get('derivatives', {}).get('venues', [])
    opts = st.get('options', {})
    etf = st.get('etf', {})
    out = {
        'price': ref.get('status') == 'ok' and scorable(_f(index, ref.get('fact_id'))),
        'calendar': st.get('calendar', {}).get('next_24h') == 'ok',
        'macro': st.get('macro', {}).get('status') not in (None, 'unknown'),
        # ETF counts only when the expected session's table is complete and reconciled (design 7.3).
        'etf': etf.get('status') == 'ok' and bool(etf.get('expected_day', {}).get('reconciled'))
        and not etf.get('conflict_dates'),
        'derivatives': sum(1 for v in venues if scorable(_f(index, v.get('funding_fact_id')))
                           and scorable(_f(index, v.get('oi_fact_id')))) >= 2,
        'options': opts.get('status') == 'ok' and scorable(_f(index, opts.get('total_fact_id'))),
        'news': feeds_ok.get('general', 0) >= 2 and feeds_ok.get('official', 0) >= 2,
        'stable_supply': stable.get('status') == 'ok' and scorable(_f(index, stable.get('total_fact_id')))
        and _val(index, stable.get('change_7d')) is not None and _val(index, stable.get('change_30d')) is not None,
    }
    return out


def hard_invalid(facts: dict, as_of: datetime) -> list[str]:
    st = facts['state']
    reasons = []
    if st.get('reference_price', {}).get('status') != 'ok':
        reasons.append('reference_price_unavailable')
    if st.get('calendar', {}).get('next_24h') != 'ok':
        reasons.append('calendar_coverage_unknown')
    for fact in facts['facts']:
        stamp = fact.get('observed_at')
        if stamp and parse_time(stamp) > as_of + timedelta(seconds=5):
            reasons.append('future_observation')
            break
    if any(f['status'] == 'invalid' for f in facts['facts']):
        reasons.append('invalid_fact')
    if st.get('carry', {}).get('status') == 'invalid':
        # Carried known-news / incident state unreadable: never treat it as "nothing known, nothing open" (R2-04).
        reasons.append('carry_state_invalid')
    if st.get('etf', {}).get('conflict_dates'):
        # Provider total vs. fund sum mismatch: both values kept, neither scored, data_hold (design 4.1, 7.4).
        reasons.append('etf_total_mismatch')
    return reasons


def etf_component(facts: dict) -> dict:
    index = {f['fact_id']: f for f in facts['facts']}
    etf = facts['state'].get('etf', {})
    five = _f(index, etf.get('five_day_fact_id'))
    last = _f(index, etf.get('last_day_fact_id'))
    ids = [x['fact_id'] for x in (five, last) if x]
    if etf.get('status') not in ('ok', 'lagged') or not scorable(five):
        return {'state': 'unknown', 'direction': 0, 'reason_code': f'etf_{etf.get("status", "missing")}', 'fact_ids': ids,
                'provisional': False}
    total = five['value']
    state = 'supportive' if total >= ETF_THRESHOLD else 'adverse' if total <= -ETF_THRESHOLD else 'neutral'
    if state != 'neutral' and scorable(last) and \
            abs(last['value']) >= ETF_LAST_DAY_OPPOSITE and (last['value'] > 0) != (total > 0):
        state = 'mixed'
    return {'state': state, 'direction': SIGN.get(state, 0), 'reason_code': f'etf_5d_{state}', 'fact_ids': ids,
            'provisional': five['status'] == 'provisional', 'lagged': etf.get('status') == 'lagged'}


def supply_component(facts: dict) -> dict:
    index = {f['fact_id']: f for f in facts['facts']}
    st = facts['state'].get('stable', {})
    d7, d30 = _val(index, st.get('change_7d')), _val(index, st.get('change_30d'))
    ids = [x for x in (st.get('change_7d'), st.get('change_30d')) if x]
    if d7 is None or d30 is None:
        return {'state': 'unknown', 'direction': 0, 'reason_code': 'stable_supply_missing', 'fact_ids': ids}
    if d7 >= 0.5 and d30 >= 1.0 and not st.get('depeg') and not facts['state'].get('usdt_depeg'):
        state = 'supportive'
    elif d7 <= -0.5 and d30 <= -1.0:
        state = 'adverse'
    else:
        state = 'neutral'
    return {'state': state, 'direction': SIGN.get(state, 0), 'reason_code': f'stable_supply_{state}', 'fact_ids': ids}


def macro_group(facts: dict) -> dict:
    m = facts['state'].get('macro', {})
    state = m.get('status', 'unknown')
    return {'state': state, 'direction': SIGN.get(state, 0), 'reason_code': m.get('reason', 'macro_unknown'),
            'fact_ids': m.get('change_fact_ids', []),
            'components': {'window': m.get('window', 'unknown'), 'common_date': m.get('common_date'),
                           'previous_date': m.get('previous_date')}}


def spot_group(facts: dict) -> dict:
    etf, supply = etf_component(facts), supply_component(facts)
    comps = {'etf': etf, 'stable_supply': supply}
    ids = etf['fact_ids'] + supply['fact_ids']
    if etf['direction'] and supply['direction'] and etf['direction'] != supply['direction']:
        return {'state': 'mixed', 'direction': 0, 'reason_code': 'etf_and_supply_opposite', 'fact_ids': ids, 'components': comps}
    if etf['state'] == 'mixed':
        return {'state': 'mixed', 'direction': 0, 'reason_code': 'etf_last_day_opposite', 'fact_ids': ids, 'components': comps}
    if etf['direction']:
        return {'state': etf['state'], 'direction': etf['direction'], 'reason_code': 'etf_direction', 'fact_ids': ids,
                'components': comps}
    if supply['direction']:
        return {'state': supply['state'], 'direction': supply['direction'], 'reason_code': 'supply_only_weak',
                'fact_ids': ids, 'components': comps}
    if etf['state'] == 'unknown':
        return {'state': 'unknown', 'direction': 0, 'reason_code': etf['reason_code'], 'fact_ids': ids, 'components': comps}
    return {'state': 'neutral', 'direction': 0, 'reason_code': 'below_thresholds', 'fact_ids': ids, 'components': comps}


def _item_ids(item: dict) -> set:
    ids = {f'url:{item["url"]}'} if item.get('url') else set()
    if item.get('event_cluster_id'):
        ids.add(f'cluster:{item["event_cluster_id"]}')
    sha = (item.get('body') or {}).get('text_sha256')
    if sha:
        ids.add(f'body:{sha}')
    return ids


def known_match(item: dict, assessment: dict, known: list[dict]) -> dict | None:
    """The adopted known event this item re-reports: shared URL / code cluster / primary body hash,
    or a ``known_event_ids`` link set by the parent (different URL or updated headline). A URL or body
    match beats a cluster or link match; ties go to the latest ``first_known_at`` (R2-03)."""
    from btc import carry
    return carry.best_match(_item_ids(item), assessment.get('known_event_ids'), known)


def eligible_news(facts: dict, assessments: list | None, mode: str, as_of: datetime, carried) -> list[dict]:
    """Parent-selected clusters that pass the code's factual conditions (design 8.2).

    Known events (adopted earlier, ``state.news.carried``; R-05) never extend the scoring period:
    the window is measured from ``first_known_at``, whatever URL or headline re-reports it. A follow-up
    counts as new only when the parent marks ``follow_up_new_facts`` and the code sees a primary body
    that differs from every known body of that event and was published after it became known.
    ``carried`` as a set of URLs (legacy unit-test input) excludes those items outright.
    """
    items = {n['id']: n for n in facts['state'].get('news', {}).get('items', [])}
    known = facts['state'].get('news', {}).get('carried') or []
    if isinstance(carried, list):
        known = carried
    legacy = carried if isinstance(carried, (set, frozenset)) else set()
    window = timedelta(hours=EVENT_WINDOW_HOURS[mode])
    out = []
    for a in assessments or []:
        item = items.get(a['news_id'])
        if item is None or a['group_id'] != 'btc_specific_event' or a['impact'] not in ('supportive', 'adverse'):
            continue
        if a['importance'] not in ('high', 'critical') or a['verification'] != 'primary_confirmed':
            continue
        if item['code_verification'] != 'primary_body_retrieved':
            continue
        if item.get('url') in legacy:
            continue
        published = parse_time(item['published_at'])
        effective, carry = published, None
        match = known_match(item, a, known)
        if match is not None:
            from btc import carry as carried_state
            first = parse_time(match['first_known_at'])
            if carried_state.is_follow_up(item, a, match, known):
                carry = {'known_id': match['known_id'], 'kind': 'follow_up_new_facts'}
            else:
                effective = min(published, first)
                carry = {'known_id': match['known_id'], 'kind': 'carry_forward', 'first_known_at': match['first_known_at']}
        if not (as_of - window <= effective and published <= as_of):
            continue
        out.append({**a, 'url': item.get('url'), 'carry': carry})
    return out


def event_group(facts: dict, assessments: list | None, mode: str, as_of: datetime, carried: set) -> dict:
    news = facts['state'].get('news', {})
    if news.get('status') == 'missing':
        return {'state': 'unknown', 'direction': 0, 'reason_code': 'news_unavailable', 'fact_ids': [], 'components': {}}
    if assessments is None:
        return {'state': 'neutral', 'direction': 0, 'reason_code': 'pending_parent_selection', 'fact_ids': [],
                'components': {'clusters': []}}
    chosen = eligible_news(facts, assessments, mode, as_of, carried)
    clusters: dict[str, set] = {}
    for a in chosen:
        clusters.setdefault(a['event_cluster_id'], set()).add(a['impact'])
    impacts = set().union(*clusters.values()) if clusters else set()
    ids = sorted({x for a in chosen for x in a.get('fact_ids', [])})
    comps = {'clusters': sorted(clusters), 'news_ids': [a['news_id'] for a in chosen],
             'carry_forward': [{'news_id': a['news_id'], **a['carry']} for a in chosen if a.get('carry')]}
    if not impacts:
        return {'state': 'neutral', 'direction': 0, 'reason_code': 'no_confirmed_btc_event', 'fact_ids': ids, 'components': comps}
    if impacts == {'supportive'}:
        return {'state': 'supportive', 'direction': 1, 'reason_code': 'confirmed_supportive_event', 'fact_ids': ids,
                'components': comps}
    if impacts == {'adverse'}:
        return {'state': 'adverse', 'direction': -1, 'reason_code': 'confirmed_adverse_event', 'fact_ids': ids,
                'components': comps}
    return {'state': 'mixed', 'direction': 0, 'reason_code': 'opposite_confirmed_events', 'fact_ids': ids, 'components': comps}


def incident_id(item: dict) -> str:
    """Identity of one incident publication: code cluster (normalised title), URL and ``published_at`` as stored on
    the item. The identical publication keeps its id across editions (a closed incident stays closed, R3-02); the
    same title and URL with another publication time is a new incident and holds again (R3-02b, fail closed)."""
    key = f'{item.get("event_cluster_id")}|{item.get("url")}|{item.get("published_at")}'
    return 'inc-' + hashlib.sha256(key.encode()).hexdigest()[:12]


def new_incidents(facts: dict, assessments: list | None) -> list[dict]:
    """Critical adverse/mixed incidents confirmed from a primary body in this edition (design 7.4)."""
    items = {n['id']: n for n in facts['state'].get('news', {}).get('items', [])}
    out = []
    for a in assessments or []:
        item = items.get(a['news_id'])
        if item and a['importance'] == 'critical' and a['verification'] == 'primary_confirmed' \
                and item['code_verification'] == 'primary_body_retrieved' and a['impact'] in ('adverse', 'mixed'):
            ident = incident_id(item)
            if any(x['incident_id'] == ident for x in out):
                continue
            out.append({'incident_id': ident, 'news_id': item['id'], 'event_cluster_id': item.get('event_cluster_id'),
                        'url': item.get('url'), 'title': item.get('title', '')[:200], 'published_at': item['published_at'],
                        'affected_source_ids': sorted(set(a.get('affected_source_ids') or []))})
    return out


def incident_status(facts: dict, assessments: list | None, recoveries: list | None = None) -> dict:
    """Open incidents = this edition's new ones + persisted open ones (R-04), minus validly released ones.

    A persisted incident is released only by ``recoveries`` entries that pass ``carry.recovery_problems``
    (official primary-body recovery after the incident + fresh data for every affected source).
    """
    from btc import carry
    current = new_incidents(facts, assessments)
    persisted = facts['state'].get('incidents', {}).get('open', [])
    by_id = {r['incident_id']: r for r in recoveries or []}
    released, rejected, still = [], [], []
    for inc in persisted:
        rec = by_id.get(inc['incident_id'])
        if rec is None:
            still.append(inc)
            continue
        problems = carry.recovery_problems(inc, rec, facts)
        if problems:
            rejected.append({'incident_id': inc['incident_id'], 'problems': problems})
            still.append(inc)
        else:
            released.append(dict(rec))
    open_ids = {i['incident_id'] for i in still}
    # An incident already closed (recovery or owner's manual release) never re-opens from the same article (R3-02).
    closed = set(facts['state'].get('incidents', {}).get('released_ids') or []) | {r['incident_id'] for r in released}
    new = [i for i in current if i['incident_id'] not in open_ids and i['incident_id'] not in closed]
    return {'new': new, 'open': still + new, 'released': released, 'rejected': rejected}


def critical_incident(facts: dict, assessments: list | None, recoveries: list | None = None) -> bool:
    return bool(incident_status(facts, assessments, recoveries)['open'])


def windows(facts: dict) -> list[dict]:
    raw = [dict(e['stop_window']) for e in facts['state'].get('events', []) if e.get('stop_window')]
    raw.sort(key=lambda w: w['start_at'])
    merged = []
    for w in raw:
        if merged and parse_time(w['start_at']) <= parse_time(merged[-1]['end_at']):
            last = merged[-1]
            if parse_time(w['end_at']) > parse_time(last['end_at']):
                last['end_at'] = w['end_at']
            last['event_ids'] = sorted(set(last['event_ids']) | set(w['event_ids']))
            last['rule_id'] = last['rule_id'] if last['rule_id'] == w['rule_id'] else 'merged_windows'
        else:
            merged.append(w)
    return merged


def leverage_item(facts: dict, candidate: int) -> dict:
    index = {f['fact_id']: f for f in facts['facts']}
    venues = facts['state'].get('derivatives', {}).get('venues', [])
    same, unassessed, ids = [], False, []
    for v in venues:
        funding = _f(index, v.get('funding_fact_id'))
        if not scorable(funding):
            continue
        ids.append(funding['fact_id'])
        if funding['value'] * candidate > 0 and abs(funding['value']) >= FUNDING_CROWDED:
            perc = _f(index, v.get('funding_percentile_fact_id'))
            if not scorable(perc):
                unassessed = True
                continue
            ids.append(perc['fact_id'])
            extreme = perc['value'] >= 90 if candidate > 0 else perc['value'] <= 10
            if extreme:
                same.append(v)
    if len(same) >= 2:
        now_total, base_total = 0.0, 0.0
        for v in same:
            oi, chg = _f(index, v.get('oi_fact_id')), _f(index, v.get('oi_change_fact_id'))
            if not scorable(oi) or not scorable(chg):
                return {'points': 0, 'status': 'unassessed', 'reason_code': 'oi_change_missing', 'fact_ids': ids}
            ids += [oi['fact_id'], chg['fact_id']]
            now_total += oi['value']
            base_total += oi['value'] / (1 + chg['value'] / 100)
        change = 100 * (now_total / base_total - 1)
        if change >= OI_CROWDED_PCT:
            return {'points': -1, 'status': 'warning', 'reason_code': 'same_side_funding_crowding_with_oi_growth',
                    'fact_ids': ids}
        return {'points': 0, 'status': 'neutral', 'reason_code': 'oi_growth_below_threshold', 'fact_ids': ids}
    if unassessed:
        return {'points': 0, 'status': 'unassessed', 'reason_code': 'funding_percentile_warming_up', 'fact_ids': ids}
    return {'points': 0, 'status': 'neutral', 'reason_code': 'no_same_side_crowding', 'fact_ids': ids}


def options_item(facts: dict, candidate: int) -> dict:
    index = {f['fact_id']: f for f in facts['facts']}
    skew = facts['state'].get('options', {}).get('skew', {})
    for tenor in ('30d', '7d'):
        value = _val(index, skew.get(f'skew_25d_{tenor}'))
        if value is None:
            continue
        perc = _val(index, skew.get(f'skew_25d_{tenor}_percentile'))
        ids = [x for x in (skew.get(f'skew_25d_{tenor}'), skew.get(f'skew_25d_{tenor}_percentile')) if x]
        if perc is None:
            return {'points': 0, 'status': 'unassessed', 'reason_code': f'skew_{tenor}_percentile_warming_up', 'fact_ids': ids}
        hit = (value >= SKEW_PP and perc >= 90) if candidate > 0 else (value <= -SKEW_PP and perc <= 10)
        return {'points': -1 if hit else 0, 'status': 'warning' if hit else 'neutral',
                'reason_code': f'skew_{tenor}_{"opposite_protection" if hit else "below_threshold"}', 'fact_ids': ids}
    return {'points': 0, 'status': 'unassessed', 'reason_code': 'skew_unavailable', 'fact_ids': []}


def evaluate(facts: dict, *, mode: str, assessments: list | None = None, finalize_at: datetime | None = None,
             carried=None, recoveries: list | None = None) -> dict:
    """Full decision state. ``assessments`` None = before the parent's selection (preview).

    ``carried`` defaults to the known events in ``state.news.carried`` (R-05); ``recoveries`` are the
    parent's ``incident_recoveries`` checked against persisted incidents (R-04).
    """
    as_of = parse_time(facts['as_of']).astimezone(UTC)
    finalize_at = (finalize_at or as_of).astimezone(UTC)
    groups = {'macro': macro_group(facts), 'spot_demand': spot_group(facts),
              'btc_specific_event': event_group(facts, assessments, mode, as_of, carried)}
    present = bundles(facts)
    invalid = hard_invalid(facts, as_of)
    directions = [g['direction'] for g in groups.values() if g['direction']]
    if invalid:
        strength, status, reason_label, candidate = '保留', 'withheld', 'データ保留', 0
        reason_codes = invalid
    elif 1 in directions and -1 in directions:
        strength, status, reason_label, candidate = '保留', 'withheld', '材料混在', 0
        reason_codes = ['opposite_groups']
    elif not directions:
        unknown = any(g['state'] == 'unknown' for g in groups.values())
        strength, status, candidate = '保留', 'withheld', 0
        reason_label = 'データ不足（unknown）' if unknown else '根拠なし（閾値未満）'
        reason_codes = ['group_unknown' if unknown else 'below_thresholds']
    else:
        candidate = directions[0]
        strength = {1: '低', 2: '中', 3: '高'}[len(directions)]
        status, reason_label = 'available', None
        reason_codes = [f'{len(directions)}_group_{"up" if candidate > 0 else "down"}']
    # Fixed 8 items.
    items = []
    etf, supply = groups['spot_demand']['components'].get('etf', {}), groups['spot_demand']['components'].get('stable_supply', {})
    opposite_supply = etf.get('direction') and supply.get('direction') and etf['direction'] != supply['direction']

    def align(key, direction, ids, weight, dedup=False, mixed=False):
        if not candidate:
            return {'key': key, 'points': 0, 'status': 'direction_unset', 'reason_code': 'direction_unset',
                    'fact_ids': ids, 'group_id': ITEM_GROUPS[key]}
        if dedup:
            return {'key': key, 'points': 0, 'status': 'deduplicated', 'reason_code': 'etf_component_active',
                    'fact_ids': ids, 'group_id': ITEM_GROUPS[key]}
        if mixed:
            return {'key': key, 'points': 0, 'status': 'mixed', 'reason_code': 'etf_and_supply_opposite',
                    'fact_ids': ids, 'group_id': ITEM_GROUPS[key]}
        if direction == candidate:
            return {'key': key, 'points': weight, 'status': 'aligned', 'reason_code': 'same_direction', 'fact_ids': ids,
                    'group_id': ITEM_GROUPS[key]}
        if direction == -candidate:
            return {'key': key, 'points': -weight, 'status': 'opposed', 'reason_code': 'opposite_direction',
                    'fact_ids': ids, 'group_id': ITEM_GROUPS[key]}
        return {'key': key, 'points': 0, 'status': 'neutral', 'reason_code': 'neutral_mixed_or_unknown', 'fact_ids': ids,
                'group_id': ITEM_GROUPS[key]}

    items.append(align('macro_alignment', groups['macro']['direction'], groups['macro']['fact_ids'], 2))
    items.append(align('etf_alignment', etf.get('direction', 0), etf.get('fact_ids', []), 2, mixed=bool(opposite_supply)))
    items.append(align('btc_event_alignment', groups['btc_specific_event']['direction'],
                       groups['btc_specific_event']['fact_ids'], 2))
    items.append(align('stable_supply_alignment', supply.get('direction', 0), supply.get('fact_ids', []), 1,
                       dedup=bool(etf.get('direction')) and not opposite_supply, mixed=bool(opposite_supply)))
    for key, fn in (('leverage_crowding', leverage_item), ('options_tail_risk', options_item)):
        if candidate:
            items.append({'key': key, 'group_id': ITEM_GROUPS[key], **fn(facts, candidate)})
        else:
            items.append({'key': key, 'points': 0, 'status': 'direction_unset', 'reason_code': 'direction_unset',
                          'fact_ids': [], 'group_id': ITEM_GROUPS[key]})
    count = sum(present.values())
    dq = 0 if count == 8 else -1 if count == 7 else -2
    items.append({'key': 'data_quality', 'points': dq, 'status': 'warning' if dq else 'neutral',
                  'reason_code': f'bundles_{count}_of_8', 'fact_ids': [], 'group_id': 'quality'})
    merged = windows(facts)
    active = [w for w in merged if parse_time(w['start_at']) <= as_of <= parse_time(w['end_at'])]
    incidents = incident_status(facts, assessments, recoveries)
    incident = bool(incidents['open'])
    high_soon = [e for e in facts['state'].get('events', []) if e['importance'] in ('high', 'critical')
                 and as_of < parse_time(e['start_at']) <= as_of + timedelta(hours=2)]
    er = -2 if active or incident else -1 if high_soon else 0
    items.append({'key': 'event_risk', 'points': er, 'status': 'warning' if er else 'neutral',
                  'reason_code': 'in_stop_window' if active else 'critical_incident' if incident else
                  'high_event_within_2h' if high_soon else 'no_event_pressure', 'fact_ids': [], 'group_id': 'event_risk'})
    total = sum(i['points'] for i in items)
    supporting = len(directions) if candidate else 0
    if total >= 5 and supporting == 3:
        band = 'High'
    elif total >= 4 and supporting >= 2:
        band = 'Med'
    elif total == 3 and supporting >= 2:
        band = 'Med-cautious'
    else:
        band = 'Low'
    if strength == '低':
        band = 'Low'
    confidence = BAND_CONFIDENCE[band]
    caps = []
    if status == 'withheld':
        caps.append('withheld_max_0.20')
        confidence = min(confidence, 0.20)
    if candidate and etf.get('direction') == candidate and etf.get('provisional') and not supply.get('direction') == candidate:
        caps.append('provisional_etf_only_max_0.60')
        confidence = min(confidence, 0.60)
    if facts['state'].get('calendar', {}).get('bls_cache') in ('ok', 'beyond_coverage'):
        caps.append('manual_calendar_cache_max_0.60')
        confidence = min(confidence, 0.60)
    if candidate and any(i['status'] == 'unassessed' for i in items if i['key'] in ('leverage_crowding', 'options_tail_risk')):
        caps.append('unassessed_item_5_or_6_max_0.60')
        confidence = min(confidence, 0.60)
    if invalid:
        caps.append('hard_invalid_zero')
        confidence = 0.0
    # Lower the band to the highest one whose ordinal confidence fits under the cap (design 7.3).
    allowed = [name for name in BAND_ORDER[BAND_ORDER.index(band):] if BAND_CONFIDENCE[name] <= confidence + 1e-9]
    band = allowed[0] if allowed else 'Low'
    confidence = 0.0 if invalid else min(confidence, BAND_CONFIDENCE[band])
    bias = 0.0 if status == 'withheld' else candidate * STRENGTH_BIAS[strength]
    # Validity and gate.
    next_high = [parse_time(e['start_at']) for e in facts['state'].get('events', [])
                 if e['importance'] in ('high', 'critical') and parse_time(e['start_at']) > as_of]
    valid_until = min([as_of + timedelta(hours=VALIDITY_HOURS[mode])] + next_high)
    if incident:
        gate, gate_codes = 'incident_hold', (['critical_incident_confirmed'] if incidents['new'] else []) + (
            ['incident_unresolved'] if len(incidents['open']) > len(incidents['new']) else [])
    elif invalid:
        gate, gate_codes = 'data_hold', invalid
    elif active:
        gate, gate_codes = 'scheduled_pause', [w['rule_id'] for w in active]
    elif finalize_at > valid_until:
        gate, gate_codes = 'expired', ['valid_until_passed_at_finalize']
    else:
        gate, gate_codes = 'clear', []
    upcoming = [w for w in merged if as_of < parse_time(w['start_at']) <= as_of + timedelta(hours=24)]
    recheck = min([parse_time(w['end_at']) for w in active] + [parse_time(w['start_at']) for w in upcoming] + [valid_until])
    quality_fail = bool(invalid)
    no_trade = band == 'Low' or gate != 'clear' or quality_fail or status == 'withheld'
    reason = None
    if no_trade:
        if gate == 'incident_hold':
            reason = '障害停止: 一次本文で確認した重大な障害・侵害があり、公式復旧と新しいデータの確認まで停止'
        elif gate == 'data_hold':
            reason = 'データ保留: ' + '、'.join(REASON_TEXT.get(c, c) for c in invalid)
        elif gate == 'scheduled_pause':
            names = [e['name'] for e in facts['state'].get('events', []) if any(e['id'] in w['event_ids'] for w in active)]
            reason = 'イベント停止: ' + '、'.join(names) + 'の停止時間内'
        elif gate == 'expired':
            reason = '期限切れ: 判断の有効期限を過ぎて完成した'
        elif status == 'withheld':
            reason = f'方向付与保留（{reason_label}）: ' + '・'.join(
                f'{GROUP_LABELS[k]}={STATE_TEXT[g["state"]]}' for k, g in groups.items())
        else:
            reason = f'方向付与保留（方向の根拠が弱い）: 確度{strength}・整合度{total}点・{band}'
    risk = risk_events(facts, as_of)
    return {
        'policy_version': POLICY_VERSION, 'policy_hash': POLICY_HASH, 'as_of': as_of, 'finalize_at': finalize_at,
        'groups': groups, 'bundles': present, 'coverage_present': count, 'coverage_required': 8,
        'hard_invalid': invalid, 'direction_status': status, 'direction_strength': strength,
        'direction_reason_label': reason_label, 'direction_reason_codes': reason_codes,
        'candidate_direction': 'up' if candidate > 0 else 'down' if candidate < 0 else 'none',
        'items': items, 'total': total, 'band': band, 'confidence': round(confidence, 3), 'caps': caps,
        'bias': bias, 'valid_until': valid_until, 'trade_gate': {
            'status': gate, 'reason_codes': gate_codes, 'active_windows': active, 'upcoming_windows': upcoming,
            'recheck_at': recheck},
        'no_trade': no_trade, 'no_trade_reason': reason, 'risk_events_next_24h': risk,
        'coverage_ratio': round(count / 8, 3), 'incident': incident,
        'incidents_open': incidents['open'], 'incidents_new': incidents['new'],
        'incidents_released': incidents['released'], 'incidents_rejected': incidents['rejected'],
    }


REASON_TEXT = {'reference_price_unavailable': '参照価格が2市場で成立しない',
               'calendar_coverage_unknown': '公式カレンダーの次24時間が未確認',
               'future_observation': '未来時刻の観測がある', 'invalid_fact': '不正な値がある',
               'etf_total_mismatch': 'ETFの提供元合計と銘柄合計が一致しない',
               'carry_state_invalid': '採用済みの既知ニュース・未解決障害の記録が読めない（記録は変更していない）'}
STATE_TEXT = {'supportive': '上向き', 'adverse': '下向き', 'neutral': '閾値未満', 'mixed': '材料混在', 'unknown': 'データ不足'}
SOURCE_LABELS = {'bls_schedule_cache': 'BLS公式予定（親確認済みキャッシュ）', 'fed_calendar': 'Fed公式',
                 'bea_calendar': 'BEA公式', 'deribit_options': 'Deribit'}
STATUS_LABELS = {'confirmed': '確認済み', 'tentative': '日付は公式・時刻は慣例'}


def risk_events(facts: dict, as_of: datetime) -> list[str]:
    out = []
    for e in facts['state'].get('events', []):
        t = parse_time(e['start_at'])
        if e['importance'] in ('high', 'critical') and as_of < t <= as_of + timedelta(hours=24):
            out.append(f'{t.astimezone(JST).strftime("%Y-%m-%d %H:%M")} JST {e["name"]}'
                       f'（{SOURCE_LABELS.get(e["source_id"], e["source_id"])}、{STATUS_LABELS.get(e["status"], e["status"])}）')
    cal = facts['state'].get('calendar', {})
    if cal.get('next_24h') != 'ok':
        out.append(f'{as_of.astimezone(JST).strftime("%Y-%m-%d %H:%M")} JST 公式予定の一部が未確認'
                   f'（{"・".join(cal.get("reasons") or ["calendar"])}、未確認）')
    return out
