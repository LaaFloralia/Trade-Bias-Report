import json

import tv_snapshot as t


def raw(oi=((1, 100.0), (2, 110.0)), gc=((1, 10.0), (2, 12.0))):
    return {'retrieved_at': '2026-09-26T22:40:00+09:00',
            'quotes': {'TVC:DXY': {'latest': 101.034, 'previous_close': 101.249, 'change': -0.3, 'time': 'x'},
                       'TVC:US02Y': {'latest': 4.86, 'previous_close': 4.931},
                       'TVC:US10Y': {'latest': 5.167, 'previous_close': 5.208},
                       'COMEX:GC1!': {'latest': 4321.2, 'previous_close': 4298.0, 'open_interest': 319769},
                       'OANDA:XAUUSD': {'latest': float('nan')}},
            'series': {'COMEX:GC1!': [{'t': d * 86400, 'c': c} for d, c in gc],
                       'COMEX:GC1!_OI': [{'t': d * 86400, 'close': v} for d, v in oi],
                       'CBOE:GVZ': [{'t': 2 * 86400, 'close': 22.44}]}}


def test_changes_are_recomputed_and_nan_rejected():
    s = t.summarize(raw())
    assert s['quotes']['TVC:DXY']['change'] == -0.215          # 表示差ではなく最新−前日終値
    assert s['us02y_change_bp'] == -7.1 and s['quotes']['OANDA:XAUUSD']['latest'] is None
    assert s['missing_required'] == []


def test_open_interest_regime_and_markdown():
    s = t.summarize(raw())
    assert s['gc_open_interest']['change'] == 10.0 and s['gc_open_interest']['regime'].startswith('上昇＋建玉増')
    assert t.oi_regime(-1, -5).startswith('下落＋建玉減') and t.oi_regime(None, 5) == '判定不能'
    assert 'GVZ 最新日足: 22.44' in t.markdown(s)


def test_missing_required_quote_is_reported(tmp_path, monkeypatch):
    r = raw()
    del r['quotes']['TVC:US02Y']
    p = tmp_path / 'tv_raw.json'
    p.write_text(json.dumps(r))
    monkeypatch.setattr(t, 'HISTORY', tmp_path / 'hist.jsonl')
    assert t.main(str(p)) == 0
    assert 'TVC:US02Y' in (tmp_path / 'tv_facts.md').read_text()
    assert (tmp_path / 'hist.jsonl').read_text().count('\n') == 1


def test_missing_update_time_falls_back_to_bar_date_and_delay():
    r = raw()
    r['quotes']['COMEX:GC1!'].update(update_mode='delayed_streaming_600')
    s = t.summarize(r)
    gc = s['quotes']['COMEX:GC1!']
    assert gc['time'] is None and gc['bar_date'] == '1970-01-03' and gc['delayed_minutes'] == 10
    assert '更新時刻なし・日足 1970-01-03 の値、10分遅延' in t.markdown(s)
    assert s['quotes']['TVC:DXY']['time'] == 'x'


def test_expected_move_uses_tv_gvz():
    r = raw()
    r['quotes']['OANDA:XAUUSD']['latest'] = 4284.97
    m = t.summarize(r)['expected_move_tv']
    assert m['move_1sd'] == round(4284.97 * 22.44 / 100 / 252 ** 0.5, 2)
    assert '参考変動額（TVのGVZ・252日換算）' in t.markdown(t.summarize(r))
    assert t._expected_move(None, 20) is None and t._expected_move(4000, 0) is None
