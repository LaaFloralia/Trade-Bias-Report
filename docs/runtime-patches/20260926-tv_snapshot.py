"""TradingView 公式MCPで親が取得した当日値を検証・集計し、時点付きで保存する。

親（定期実行の Codex）は MCP で次を取得し、作業フォルダの tv_raw.json に保存する:
  {"retrieved_at": "ISO8601",
   "quotes": {"TVC:DXY": {"latest", "change", "previous_close", "time"}, "TVC:US02Y": {...},
              "TVC:US10Y": {...}, "COMEX:GC1!": {..., "volume", "open_interest"}, "OANDA:XAUUSD": {...}},
   "series": {"COMEX:GC1!": [{"t", "c", "v"}...], "COMEX:GC1!_OI": [{"t", "close"}...],
              "CBOE:GVZ": [{"t", "close"}...]}}
このスクリプトは値の型・時刻を検証し、金利の bp 変化、金先物の建玉変化と価格との組み合わせ、
GVZ の最新日足を計算して Markdown と JSON を出力する。判断はしない。

  python tv_snapshot.py <work_dir>/tv_raw.json
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HISTORY = ROOT / 'runtime' / 'output' / 'history' / 'tv_snapshots.jsonl'
REQUIRED = ('TVC:DXY', 'TVC:US02Y', 'TVC:US10Y', 'COMEX:GC1!')


def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _quote(q, bars=()):
    """bars: 同じ銘柄の日足 [(t, close)]。提供元の更新時刻が無いとき、値の時点を最新日足の日付で示す。"""
    q = q if isinstance(q, dict) else {}
    latest, prev = _num(q.get('latest')), _num(q.get('previous_close'))
    time_ = next((q[k] for k in ('time', 'update_time', 'last_update_time') if isinstance(q.get(k), str) and q[k]), None)
    mode = q.get('update_mode') if isinstance(q.get('update_mode'), str) else None
    return {'latest': latest, 'previous_close': prev,
            'change': round(latest - prev, 6) if latest is not None and prev is not None else _num(q.get('change')),
            'time': time_, 'bar_date': _day(bars[-1][0]) if bars else None,
            'delayed_minutes': int(mode.rsplit('_', 1)[1]) // 60 if mode and mode.startswith('delayed_streaming_')
            and mode.rsplit('_', 1)[1].isdigit() else None,
            'volume': _num(q.get('volume')), 'open_interest': _num(q.get('open_interest'))}


def _series(rows, key):
    out = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and _num(row.get('t')) is not None and _num(row.get(key)) is not None:
            out.append((int(row['t']), float(row[key])))
    return sorted(out)


SESSION_SHIFT = timedelta(hours=10)


def _day(t):
    """TradingView の日足時刻はセッション開始（DXY 23:00・GC 22:00・XAUUSD 21:00 UTC は前日、GVZ 13:30 UTC は当日）。
    10時間進めて取引日の日付にそろえる。"""
    return (datetime.fromtimestamp(t, timezone.utc) + SESSION_SHIFT).date().isoformat()


def oi_regime(price_change, oi_change):
    """価格と建玉の同日変化の組み合わせ（先物分析の一般的な読み方。金での検証はしていない）。"""
    if price_change is None or oi_change is None or price_change == 0 or oi_change == 0:
        return '判定不能'
    if price_change > 0:
        return '上昇＋建玉増（新規買いが主導の可能性）' if oi_change > 0 else '上昇＋建玉減（売り方の買い戻しが主導の可能性）'
    return '下落＋建玉増（新規売りが主導の可能性）' if oi_change > 0 else '下落＋建玉減（買い方の手仕舞いが主導の可能性）'


def summarize(raw: dict) -> dict:
    series = raw.get('series') or {}
    quotes = {sym: _quote((raw.get('quotes') or {}).get(sym), _series(series.get(sym), 'c')) for sym in
              ('TVC:DXY', 'TVC:US02Y', 'TVC:US10Y', 'COMEX:GC1!', 'OANDA:XAUUSD')}
    missing = [s for s in REQUIRED if quotes[s]['latest'] is None]
    gc = _series(series.get('COMEX:GC1!'), 'c')
    oi = _series(series.get('COMEX:GC1!_OI'), 'close')
    gvz = _series(series.get('CBOE:GVZ'), 'close')
    oi_block = None
    if len(oi) >= 2:
        (t0, v0), (t1, v1) = oi[-2], oi[-1]
        price = {_day(t): c for t, c in gc}
        days = sorted(price)
        d1 = _day(t1)
        p_change = None
        if d1 in price and days.index(d1) > 0:
            p_change = price[d1] - price[days[days.index(d1) - 1]]
        oi20 = oi[-21][1] if len(oi) >= 21 else None
        oi_block = {'date': d1, 'open_interest': v1, 'change': v1 - v0, 'price_change_same_day': p_change,
                    'regime': oi_regime(p_change, v1 - v0),
                    'change_20d_pct': round((v1 - oi20) / oi20 * 100, 2) if oi20 else None}
    bp = lambda s: round(quotes[s]['change'] * 100, 1) if quotes[s]['change'] is not None else None
    return {'retrieved_at': raw.get('retrieved_at'), 'missing_required': missing, 'quotes': quotes,
            'us02y_change_bp': bp('TVC:US02Y'), 'us10y_change_bp': bp('TVC:US10Y'),
            'gc_open_interest': oi_block,
            'gvz_latest': {'date': _day(gvz[-1][0]), 'close': gvz[-1][1]} if gvz else None,
            'expected_move_tv': _expected_move(quotes['OANDA:XAUUSD']['latest'] or quotes['COMEX:GC1!']['latest'],
                                               gvz[-1][1] if gvz else None)}


def _expected_move(price, gvz):
    """入力データの参考変動額と同じ式（価格×GVZ/100/√252）を、TV の新しい GVZ で計算する。"""
    if price is None or gvz is None or price <= 0 or gvz <= 0:
        return None
    move = price * gvz / 100 / math.sqrt(252)
    return {'price': price, 'gvz': gvz, 'move_1sd': round(move, 2),
            'lower': round(price - move, 2), 'upper': round(price + move, 2)}


def markdown(s: dict) -> str:
    q = s['quotes']
    fmt = lambda v, d=3: f'{v:,.{d}f}' if isinstance(v, (int, float)) else '取得不可'
    lines = ['### TradingView 当日値（親がMCPで取得・コード集計）',
             f"- 取得: {s['retrieved_at']}。値は提供元の時刻つき。FRED より新しい場合は当日グループの判定にこちらを使う"]
    for sym, label in (('TVC:DXY', 'DXY'), ('TVC:US02Y', '米2年債'), ('TVC:US10Y', '米10年債'),
                       ('COMEX:GC1!', '金先物（期近）'), ('OANDA:XAUUSD', 'XAUUSD')):
        v = q[sym]
        when = v['time'] or (f"更新時刻なし・日足 {v['bar_date']} の値" if v['bar_date'] else '時刻不明')
        delay = f"、{v['delayed_minutes']}分遅延" if v['delayed_minutes'] else ''
        lines.append(f"- {label}: {fmt(v['latest'])}（前日終値 {fmt(v['previous_close'])}、差 {fmt(v['change'])}、{when}{delay}）")
    lines.append(f"- 米2年債 {s['us02y_change_bp']}bp／米10年債 {s['us10y_change_bp']}bp（前日終値比）")
    oi = s['gc_open_interest']
    if oi:
        lines.append(f"- 金先物の建玉（{oi['date']}）: {oi['open_interest']:,.0f}枚、前日比 {oi['change']:+,.0f}枚、"
                     f"同日の価格変化 {fmt(oi['price_change_same_day'], 1)} → {oi['regime']}。20営業日の建玉変化 {oi['change_20d_pct']}%")
        lines.append('  ※ 価格×建玉の読み方は先物分析の一般則で、金での予測力は未検証。方向の票にせず需給の補足に使う')
    else:
        lines.append('- 金先物の建玉: 取得不可')
    g = s['gvz_latest']
    lines.append(f"- GVZ 最新日足: {g['close']}（{g['date']}）" if g else '- GVZ: 取得不可')
    m = s.get('expected_move_tv')
    if m:
        lines.append(f"- 参考変動額（TVのGVZ・252日換算）: ±{m['move_1sd']:,.2f}（{m['lower']:,.2f}〜{m['upper']:,.2f}、基準価格 {m['price']:,.2f}）。"
                     "入力データの値より GVZ の日付が新しければ本文ではこちらを優先し、両方の日付を書く")
    if s['missing_required']:
        lines.append(f"- 取得できなかった必須銘柄: {'・'.join(s['missing_required'])}（該当項目は入力データの値を使う）")
    return '\n'.join(lines) + '\n'


def main(path: str) -> int:
    raw_path = Path(path)
    raw = json.loads(raw_path.read_text(encoding='utf-8'))
    summary = summarize(raw)
    raw_path.with_name('tv_facts.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding='utf-8')
    raw_path.with_name('tv_facts.md').write_text(markdown(summary), encoding='utf-8')
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY.open('a', encoding='utf-8') as f:
        f.write(json.dumps(summary, ensure_ascii=False) + '\n')
    print(markdown(summary))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1]))
