"""Build report observations and figures from one fresh, immutable collection.

No data collection, LLM, credentials, Brain, or network access in this module.
Numeric facts are rendered once into an appendix and referenced by every figure.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

JST = ZoneInfo('Asia/Tokyo')
REQUIRED_FIGURES = ('price', 'retail', 'etf', 'cot', 'calendar')


class BundleError(ValueError):
    pass


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def timestamp(value):
    try:
        t = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return t.replace(tzinfo=JST) if t.tzinfo is None else t.astimezone(JST)
    except (ValueError, TypeError, AttributeError):
        raise BundleError('Missing or invalid data timestamp') from None


def require_fresh(data, now=None):
    stamp = timestamp(data.get('timestamp'))
    current = (now or datetime.now(JST)).astimezone(JST)
    if stamp > current or current - stamp > timedelta(hours=6):
        raise BundleError('Data is stale or future-dated; maximum age is six hours')
    return stamp


def number(value, digits=2):
    if isinstance(value, bool):
        raise BundleError('Boolean is not a numeric observation')
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            raise InvalidOperation
        return float(result.quantize(Decimal(1).scaleb(-digits)))
    except (InvalidOperation, ValueError):
        raise BundleError('Numeric observation is missing or nonfinite') from None


def usable(value):
    return isinstance(value, dict) and not value.get('error') and value.get('stale') is not True


def quote_snapshot(data):
    """Whitelist the price fields before main.save_scraped removes transient quotes."""
    quote = data.get('_raw_quote_XAUUSD')
    if not isinstance(quote, dict) or number(quote.get('close')) <= 0:
        raise BundleError('Required XAUUSD quote is missing')
    return {key: quote.get(key) for key in ('symbol', 'close', 'datetime', 'timestamp', 'previous_close', 'high', 'low')}


def observations(data, now=None):
    stamp = require_fresh(data, now)
    figures, rows, missing, bindings, limitations = [], [], [], [], []

    def add(ident, kind, title, unit, caption, source, observed, specs):
        items, item_rows, item_bindings = [], [], []
        for label, value, pointer, digits, suffix, detail in specs:
            value = number(value, digits)
            display = f'{value:,.{digits}f}{suffix}'
            text = f'{label}: {display}。観測: {observed}。{detail}'.strip()
            item_rows.append(text)
            item = {'label': label, 'value': value, 'display': display, 'source_quote': text,
                    'tone': 'negative' if value < 0 else 'neutral'}
            if detail:
                item['detail'] = detail
            items.append(item)
            item_bindings.append({'figure': ident, 'label': label, 'path': pointer, 'value': value,
                             'digits': digits, 'unit': unit, 'observedAt': observed})
        rows.extend(item_rows)
        bindings.extend(item_bindings)
        figures.append({'id': ident, 'type': kind, 'title': title, 'unit': unit,
                        'subtitle': f'{unit} / 観測: {observed}', 'caption': caption,
                        'source_label': source[0], 'source_url': source[1], 'items': items})

    q = data.get('report_quote') or data.get('_raw_quote_XAUUSD')
    if not isinstance(q, dict) or number(q.get('close')) <= 0:
        raise BundleError('Required XAUUSD quote is missing')
    r = (data.get('retail_sentiment') or {}).get('XAUUSD')
    quote_time = str(q.get('datetime') or q.get('timestamp') or '市場観測時刻は未確認')
    if usable(r):
        rt = ('観測日 ' + str(r['as_of_date'])) if r.get('as_of_date') else ('観測時刻は未確認 / 取得 ' + str(r.get('timestamp') or '未確認'))
        provider = r.get('source') or '提供元は未確認'
        provider_url = 'https://fxssi.com/tools/current-ratio' if provider.upper() == 'FXSSI' else 'https://www.myfxbook.com/community/outlook'
        try:
            add('price', 'price_map', '価格とリテール平均建値', 'USD/oz',
                '価格と建玉集計は別の系列です。観測時刻と時点の一致は未確認です。平均建値はSL位置や注文集中を示しません。小数第2位へ丸めています。',
                ('Twelve Data / ' + provider, 'https://twelvedata.com/'),
                f'価格 {quote_time} / 建玉 {rt}', [
                    ('XAUUSD 記録価格', q.get('close'), 'report_quote.close', 2, '', ''),
                    ('Long 平均建値', r.get('avg_long_entry'), 'retail_sentiment.XAUUSD.avg_long_entry', 2, '', ''),
                    ('Short 平均建値', r.get('avg_short_entry'), 'retail_sentiment.XAUUSD.avg_short_entry', 2, '', '')])
            figures[-1]['source_links'] = [{'label': 'Twelve Data', 'url': 'https://twelvedata.com/'},
                                            {'label': provider, 'url': provider_url}]
        except BundleError:
            limitations.append('Long・Shortの平均建値は取得できず、価格との比較はできません。この比較に基づく方向判断は行いません')
            add('price', 'price_map', '価格とリテール平均建値（比較不能）', 'USD/oz',
                'Long平均建値: 取得できず／比較不能。Short平均建値: 取得できず／比較不能。XAUUSD記録価格だけを表示しています。欠測を高安や推測値に置き換えていません。',
                ('Twelve Data', 'https://twelvedata.com/'), quote_time,
                [('XAUUSD 記録価格', q.get('close'), 'report_quote.close', 2, '', '')])
            figures[-1]['availability'] = 'partial'
            rows.append(limitations[-1])
        try:
            long, short = number(r.get('long_pct')), number(r.get('short_pct'))
            if min(long, short) < 0 or abs(long + short - 100) > .01:
                raise BundleError('Retail percentages do not add to 100')
            add('retail', 'split', '参加口座の建玉比率', '%',
                '取得された比率を小数第2位で表示しています。母集団・分母は提供元の定義に依存し、市場全体の比率を示すものではありません。',
                (provider, provider_url), rt, [
                    ('Long', long, 'retail_sentiment.XAUUSD.long_pct', 2, '%', ''),
                    ('Short', short, 'retail_sentiment.XAUUSD.short_pct', 2, '%', '')])
        except BundleError:
            missing.append('retail: 建玉の一部欠測または比率不整合')
    else:
        missing.append('retail: 建玉データは欠測または古い')
    etf = data.get('gold_etf')
    if usable(etf) and etf.get('as_of_date'):
        try:
            add('etf', 'diverging_bars', 'GLD保有量の変化', 'トン',
                '期間は重複するため合算しません。保有量変化は価格収益率や資金流入額ではありません。',
                ('SPDR Gold Shares', 'https://www.spdrgoldshares.com/usa/historical-data/'), etf['as_of_date'], [
                    ('直近5営業日', etf.get('change_5d_t'), 'gold_etf.change_5d_t', 2, ' t', '期間は直近5営業日'),
                    ('直近20営業日', etf.get('change_20d_t'), 'gold_etf.change_20d_t', 2, ' t', '期間は直近20営業日')])
            streak = etf.get('streak_days')
            direction = etf.get('streak_direction')
            if isinstance(streak, int) and streak > 0 and direction in {'inflow', 'outflow'}:
                word = '保有増' if direction == 'inflow' else '保有減'
                note = f'GLDは直近{streak}営業日連続の{word}。観測: {etf["as_of_date"]}。中期の変化と直近の動きを分けて確認します。'
                rows.append(note)
                figures[-1]['caption'] += note
                limitations.append(note)
        except BundleError:
            missing.append('etf: 保有量変化は欠測')
    cot = data.get('cot_disaggregated')
    if usable(cot) and isinstance(cot.get('data'), dict) and cot['data'].get('date'):
        c = cot['data']
        try:
            specs = []
            for key, label in [('managed_money', 'Managed Money'), ('swap_dealers', 'Swap Dealers'),
                               ('producer_merchant', 'Producer / Merchant'), ('other_reportables', 'Other Reportables')]:
                v = c.get(key) or {}
                if number(v.get('long'), 0) - number(v.get('short'), 0) != number(v.get('net'), 0):
                    raise BundleError('COT net does not match long minus short')
                specs.append((label, v['net'], f'cot_disaggregated.data.{key}.net', 0, ' 枚', ''))
            add('cot', 'diverging_bars', 'Gold先物・主体別ネット建玉', '枚',
                'ネットはLong−Short。週次の先物建玉であり、スポット注文や過熱の判定ではありません。',
                ('CFTC Disaggregated', 'https://www.cftc.gov/dea/futures/other_lf.htm'), c['date'], specs)
        except BundleError:
            missing.append('cot: 主体別建玉の欠測または算術不整合')
    calendar = data.get('economic_calendar')
    upcoming = []
    if usable(calendar):
        for event in calendar.get('events') or []:
            try:
                day = datetime.strptime(event['date'], '%A, %B %d, %Y').date()
                t = datetime.fromisoformat(f'{day.isoformat()}T{event["time_jst"]}').replace(tzinfo=JST)
            except (KeyError, TypeError, ValueError):
                continue
            if stamp <= t <= stamp + timedelta(days=7) and event.get('country') in ('United States', 'Euro Zone'):
                upcoming.append((t, event))
        items = []
        for t, event in sorted(upcoming, key=lambda pair: pair[0])[:6]:
            label = (str(event.get('country', '')) + ' / ' + str(event.get('indicator', ''))).replace('|', ' ')[:180]
            text = f'{t.date().isoformat()} {t.strftime("%H:%M")} JST {label}。提供カレンダーの掲載予定。発表元との日時照合の状況は本文を参照。'
            rows.append(text)
            items.append({'date': t.date().isoformat(), 'time': t.strftime('%H:%M') + ' JST',
                          'label': label, 'source_quote': text, 'detail': '発表直前に日程を再確認'})
        if items:
            figures.append({'id': 'calendar', 'type': 'timeline', 'title': '今後の掲載予定',
                            'subtitle': 'JST / 今後7日間の取得範囲', 'caption': '最大6件の掲載予定です。網羅性は未確認。予想欄は列の意味が未照合のため図に採用しません。',
                            'source_label': 'Investing.com Calendar', 'source_url': 'https://www.investing.com/economic-calendar/', 'items': items})
    if data.get('synthetic') is True:
        for figure in figures:
            figure['caption'] = '合成値による表示検証です。リンクは参照用で、数値の取得元ではありません。' + figure['caption']
            figure['title'] += '（合成値）'
    present = {f['id'] for f in figures}
    missing.extend(f'{key}: 図の作成に必要なデータが不足' for key in REQUIRED_FIGURES if key not in present)
    appendix = ('\n\n## 図表に使用した観測値\n\n'
                f'データ収集: {stamp.isoformat()}。以下の数値は同じ収集JSONから図表と本文へ出力しています。丸めは各図に表示しています。\n\n' +
                '\n\n'.join(rows) + '\n')
    if missing:
        appendix += '\n欠測・除外: ' + '。'.join(dict.fromkeys(missing)) + '\n'
    return {'asOf': stamp.isoformat(), 'figures': figures, 'appendix': appendix,
            'missing': list(dict.fromkeys(missing)), 'limitations': limitations, 'bindings': bindings}


def check_bindings(data, manifest, figures):
    """Recompute chart values from recorded source paths; matching digits elsewhere never pass."""
    indexed = {(f['id'], i['label']): i for f in figures for i in f['items'] if 'value' in i}
    expected = {(b['figure'], b['label']) for b in manifest['bindings']}
    if set(indexed) != expected:
        raise BundleError('Figure bindings are missing or unexpected')
    for b in manifest['bindings']:
        value = data
        for key in b['path'].split('.'):
            if key == 'report_quote' and key not in value:
                key = '_raw_quote_XAUUSD'
            if not isinstance(value, dict) or key not in value:
                raise BundleError('Bound source field is missing')
            value = value[key]
        actual = number(value, b['digits'])
        if actual != b['value'] or actual != indexed[(b['figure'], b['label'])]['value']:
            raise BundleError('Figure number does not match its source field')


def make_summary(source, mode, as_of):
    """Quote complete source paragraphs; never invent an editorial conclusion."""
    sections = re.split(r'^##\s+', source, flags=re.M)
    intro = next((s for s in sections if re.match(r'.*(?:サマリー|要約)', s.split('\n', 1)[0])), '')
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', intro.partition('\n')[2])
                  if p.strip() and not p.lstrip().startswith(('#', '|', '<'))]
    if not paragraphs:
        raise BundleError('Report summary section has no source paragraph')
    first = paragraphs[0]
    if len(first) > 1100:
        raise BundleError('Summary paragraph is too long; edit without truncating conditions')
    # Keep complete source statements; a long metrics list is not a useful lead.
    conclusion_text = first
    lines = [line.strip() for line in first.splitlines() if line.strip()]
    if len(lines) > 3:
        lead = next((line for line in lines if re.match(r'XAUUSD[:：]', line)), None)
        if lead:
            conclusion_text = lead
    candidates = [p.strip() for p in re.split(r'\n\s*\n', source.split('## 図表に使用した観測値')[0])
                  if p.strip() and not p.lstrip().startswith(('#', '|', '<')) and p.strip() != first and len(p) <= 700]
    condition = next((p for p in candidates if re.search(r'取得後|確認できな|再評価|成立しな|再開後', p)), None)
    if condition is None:
        condition = next((p for p in candidates if re.search('確認|条件|見送', p)), None)
    def plain(text):
        return text.replace('**', '').replace('`', '').replace('  \n', '\n')
    metrics = []
    score_line = next((line for line in lines if '信頼度' in line and 'スコア' in line), None)
    if score_line:
        match = re.search(r'信頼度[:：]\s*(\w+)', score_line)
        if match:
            metrics.append({'label': '信頼度', 'value': match.group(1), 'context': plain(score_line), 'source_quote': score_line})
    return {'eyebrow': f'GOLD {mode.upper()}', 'short_title': 'チャート外分析',
            'report_date': timestamp(as_of).date().isoformat(), 'status': '観測・条件・未確認事項',
            'conclusion': {'title': '今回の見立て', 'text': plain(conclusion_text), 'source_quote': first},
            'metrics': metrics,
            'conditions': [{'title': '次に確認する条件', 'text': plain(condition), 'source_quote': condition}] if condition else [],
            'source_note': '収集時刻と市場の観測時点は異なります。各図の出典・本文の制約を確認してください。',
            'note': '判断条件と数値は原稿の引用です。売買の確率を示すものではありません。'}


def prepare(md_path, data_path, mode, now=None):
    md_path, data_path = Path(md_path), Path(data_path)
    data = json.loads(data_path.read_text())
    result = observations(data, now)
    source = md_path.read_text(encoding='utf-8')
    marker = '\n\n## 図表に使用した観測値\n'
    source = source.split(marker)[0].rstrip() + result['appendix']
    md_path.write_text(source, encoding='utf-8')
    summary = make_summary(source, mode, result['asOf'])
    summary['limitations'] = [{'title': '今回の制約と直近の変化', 'text': note, 'source_quote': note}
                              for note in result['limitations']]
    summary_path, figures_path, manifest_path = [md_path.with_suffix(s) for s in ('.summary.json', '.figures.json', '.bundle.json')]
    check_bindings(data, result, result['figures'])
    for path, value in ((summary_path, summary), (figures_path, result['figures'])):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    manifest = {'schemaVersion': 1, 'kind': mode, 'asOf': result['asOf'],
                'reportDate': timestamp(result['asOf']).date().isoformat(), 'synthetic': data.get('synthetic') is True,
                'dataPath': str(data_path.absolute()), 'dataSha256': digest(data_path),
                'sourcePath': str(md_path.absolute()), 'sourceSha256': digest(md_path),
                'summaryPath': str(summary_path), 'summarySha256': digest(summary_path),
                'figuresPath': str(figures_path), 'figuresSha256': digest(figures_path),
                'missing': result['missing'], 'limitations': result['limitations'], 'bindings': result['bindings'],
                'requiredFigures': list(REQUIRED_FIGURES), 'figureIds': [f['id'] for f in result['figures']],
                'numericCheck': 'passed', 'semanticReview': 'pending', 'publicationReady': False}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    return {'summary_path': str(summary_path), 'visuals_path': str(figures_path), 'bundle_path': str(manifest_path)}


def calendar_events_24h(data, as_of):
    """Keep every supplied US/Euro Zone event inside the exact next-24h window."""
    start = timestamp(as_of)
    events = []
    for event in (data.get('economic_calendar') or {}).get('events') or []:
        if event.get('country') not in {'United States', 'Euro Zone'}:
            continue
        try:
            day = datetime.strptime(event['date'], '%A, %B %d, %Y').date()
            when = datetime.fromisoformat(f'{day.isoformat()}T{event["time_jst"]}').replace(tzinfo=JST)
        except (KeyError, ValueError, TypeError):
            continue
        if start <= when <= start + timedelta(hours=24):
            label = f'{when.date().isoformat()} {when.strftime("%H:%M")} JST {event["country"]} / {event["indicator"]}（提供カレンダー、確認状況は本文参照）'
            events.append((when, label))
    return list(dict.fromkeys(label for _, label in sorted(events)))
