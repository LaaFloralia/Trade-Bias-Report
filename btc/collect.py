"""Collection plan for one BTCUSD run (design 3, 4; P8).

Three phases inside one budget (design 3.1: 540 s collection, wall clock from
the collection start):
1. ``collect_context`` (collect worker, the only process holding the FRED key):
   slow-moving and history sources in parallel, plus news headlines only;
   then the entry, outside the sandbox, runs the Jev headline triage
   (btc.headline_triage);
2. ``finish_collection`` (news-detail worker, sandboxed, no secrets): news
   selection, article bodies, reactions and clusters;
3. quotes and the 0/30/60 s book snapshots last, so that the reference price
   and the books are as fresh as possible when the collection closes (as_of).

Coinbase is never contacted (terms, P8); it is recorded as terms_restricted.
Only normalised records and raw_sha256 values leave this module.
"""
from __future__ import annotations

from datetime import datetime, timezone
import time

from btc import SYMBOL
from btc.common import JST
from btc.fetch import SourceTask, collect_parallel
from btc.sources import calendar, derivatives, flows, macro, market, options

UTC = timezone.utc
COLLECTOR_VERSION = 'btc-collector-1.0'
BUDGET_SECONDS = 540
BOOK_PHASE_SECONDS = 110  # reserved for quotes + three book snapshots
NEWS_DETAIL_SECONDS = 120  # article bodies (90 s) + reactions
MAX_WORKERS = 6           # design 3.1: six concurrent requests overall
DAILY_LOOKBACK_HOURS = (36, 72)  # max(36 h, since last success), capped at 72 h (design 9)
WEEKLY_LOOKBACK_HOURS = 168
TERMS_RESTRICTED = (
    {'source_id': 'coinbase', 'status': 'terms_restricted', 'reason': '規約により未使用',
     'detail': 'Coinbase Market Data Terms (2026-08-07) restrict AI/automated use; no price, book or premium is fetched.'},
    {'source_id': 'coinglass', 'status': 'not_applicable', 'reason': '未使用（有料・推定モデル）',
     'detail': 'Liquidation heatmaps are paid model estimates; observed books and option OI are shown instead.'},
)

# Sources this collector may contact (hash recorded in machine.json provenance).
SOURCE_CATALOG = (
    ('binance_derivatives', 'https://fapi.binance.com/'), ('bybit_derivatives', 'https://api.bybit.com/'),
    ('okx_derivatives', 'https://www.okx.com/api/v5/'), ('binance_ls', 'https://fapi.binance.com/futures/data/'),
    ('binance_top_position', 'https://fapi.binance.com/futures/data/'),
    ('binance_taker', 'https://fapi.binance.com/futures/data/'), ('deribit_options', 'https://www.deribit.com/api/v2/'),
    ('farside', 'https://farside.co.uk/btc/'), ('fgi', 'https://api.alternative.me/fng/'),
    ('cftc_tff', 'https://publicreporting.cftc.gov/resource/gpe5-46if.json'),
    ('defillama', 'https://stablecoins.llama.fi/'), ('mempool_fees', 'https://mempool.space/api/v1/fees/recommended'),
    ('coingecko_global', 'https://api.coingecko.com/api/v3/global'),
    ('treasury_yields', 'https://home.treasury.gov/resource-center/data-chart-center/interest-rates/'),
    ('fred', 'https://api.stlouisfed.org/fred/series/observations'),
    ('fed_calendar', 'https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm'),
    ('bea_calendar', 'https://www.bea.gov/news/schedule'), ('bls_schedule_cache', 'https://www.bls.gov/schedule/'),
    ('news', 'rss: coindesk, theblock, cointelegraph, decrypt, bitcoinmagazine, sec, cftc, federalreserve'),
    ('binance_klines', 'https://api.binance.com/api/v3/klines'),
    ('kraken_ticker', 'https://api.kraken.com/0/public/'), ('bitstamp_ticker', 'https://www.bitstamp.net/api/v2/'),
    ('binance_spot', 'https://api.binance.com/api/v3/'), ('kraken_usdt', 'https://api.kraken.com/0/public/'),
    ('books', 'binance, binance futures, bybit, okx, kraken, bitstamp order books (25 bp buckets only)'),
)


def source_catalog_sha256() -> str:
    import hashlib
    import json
    data = {'version': COLLECTOR_VERSION, 'sources': SOURCE_CATALOG,
            'not_used': [{k: v for k, v in t.items() if k != 'detail'} for t in TERMS_RESTRICTED]}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def news_lookback_hours(mode: str, now: datetime, last_success: datetime | None) -> float:
    if mode == 'weekly':
        return WEEKLY_LOOKBACK_HOURS
    low, high = DAILY_LOOKBACK_HOURS
    if last_success is None:
        return low
    since = (now - last_success).total_seconds() / 3600
    return round(min(high, max(low, since)), 2)


def first_phase_tasks(mode: str, now: datetime, lookback_hours: float) -> list[SourceTask]:
    from btc import news

    today = now.astimezone(UTC).date()
    return [
        SourceTask('binance_derivatives', derivatives.binance_derivatives),
        SourceTask('bybit_derivatives', derivatives.bybit_derivatives),
        SourceTask('okx_derivatives', derivatives.okx_derivatives),
        SourceTask('binance_ls', derivatives.binance_ls),
        SourceTask('binance_top_position', derivatives.binance_top_position),
        SourceTask('binance_taker', derivatives.binance_taker),
        SourceTask('deribit_options', lambda f: options.deribit_options(f, now.astimezone(UTC))),
        SourceTask('farside', flows.farside, 'https://farside.co.uk/btc/'),
        SourceTask('fgi', flows.fgi, 'https://api.alternative.me/fng/'),
        SourceTask('cftc_tff', flows.cftc_tff, 'https://publicreporting.cftc.gov/resource/gpe5-46if.json'),
        SourceTask('defillama', macro.defillama, 'https://stablecoins.llama.fi/stablecoins'),
        SourceTask('mempool_fees', macro.mempool, 'https://mempool.space/api/v1/fees/recommended'),
        SourceTask('coingecko_global', macro.coingecko_global, 'https://api.coingecko.com/api/v3/global'),
        SourceTask('treasury_yields', lambda f: macro.treasury(f, today)),
        SourceTask('fred', macro.fred, 'https://api.stlouisfed.org/fred/series/observations'),
        SourceTask('fed_calendar', lambda f: calendar.fed_calendar(f, today),
                   'https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm'),
        SourceTask('bea_calendar', lambda f: calendar.bea_calendar(f, today), 'https://www.bea.gov/news/schedule'),
        SourceTask('news', lambda f: news.collect_headline_source(f, now=now.astimezone(UTC),
                                                                  lookback_hours=lookback_hours)),
    ]


def book_phase_tasks(sleep=time.sleep) -> list[SourceTask]:
    tasks = [SourceTask('kraken_ticker', market.kraken_ticker), SourceTask('bitstamp_ticker', market.bitstamp_ticker),
             SourceTask('binance_spot', market.binance_spot), SourceTask('kraken_usdt', market.kraken_usdt)]
    tasks += [SourceTask(source_id, market.book(source_id, sleep)) for source_id in market.VENUES]
    return tasks


def collect_context(mode: str, *, started_at: datetime, last_success: datetime | None = None,
                    budget_seconds: float = BUDGET_SECONDS, first=None, fetcher_factory=None) -> dict:
    """Phase 1 (collect worker); never raises for a source failure (each becomes a fixed-label record)."""
    lookback = news_lookback_hours(mode, started_at, last_success)
    first = first if first is not None else first_phase_tasks(mode, started_at, lookback)
    one = collect_parallel(first, budget_seconds=max(30.0, budget_seconds - BOOK_PHASE_SECONDS - NEWS_DETAIL_SECONDS),
                           max_workers=MAX_WORKERS, fetcher_factory=fetcher_factory)
    return {
        'symbol': SYMBOL, 'collector_version': COLLECTOR_VERSION, 'mode': mode,
        'collection_started_at': started_at.astimezone(JST).isoformat(),
        'news_lookback_hours': lookback,
        'last_success_at': last_success.astimezone(JST).isoformat() if last_success else None,
        'budget_seconds': budget_seconds, 'budget_exhausted': one['budget_exhausted'],
        'phases': [{'name': 'context', 'elapsed_ms': one['elapsed_ms'], 'budget_seconds': one['budget_seconds']}],
        'sources': one['results'],
    }


def news_record(context: dict) -> dict | None:
    return next((r for r in context.get('sources', []) if r.get('source_id') == 'news'), None)


def _elapsed(started_at: datetime) -> float:
    return max(0.0, (datetime.now(JST) - started_at).total_seconds())


def finish_collection(context: dict, scores: dict | None, selection: dict, *, second=None,
                      fetcher_factory=None) -> dict:
    """Phases 2 and 3 (news-detail worker, no secrets) inside the remaining collection budget."""
    from btc import news
    from btc.common import parse_time

    started_at = parse_time(context['collection_started_at'])
    budget_seconds = float(context.get('budget_seconds') or BUDGET_SECONDS)
    sources = [dict(r) for r in context['sources']]
    phases = list(context['phases'])
    exhausted = bool(context.get('budget_exhausted'))
    index = next((i for i, r in enumerate(sources) if r.get('source_id') == 'news'), None)
    if index is not None:
        record = sources[index]
        if record.get('status') in ('ok', 'partial') and isinstance((record.get('values') or {}).get('candidates'), list):
            remaining = budget_seconds - _elapsed(started_at) - BOOK_PHASE_SECONDS
            detail = collect_parallel(
                [SourceTask('news', lambda f: news.finish_news(f, record, scores, selection,
                                                               now=started_at.astimezone(UTC)),
                            record.get('source_url'))],
                budget_seconds=min(NEWS_DETAIL_SECONDS, max(30.0, remaining)), max_workers=1,
                fetcher_factory=fetcher_factory)
            sources[index] = detail['results'][0]
            exhausted = exhausted or detail['budget_exhausted']
            phases.append({'name': 'news_detail', 'elapsed_ms': detail['elapsed_ms'],
                           'budget_seconds': detail['budget_seconds']})
        else:
            (record.get('values') or {}).pop('candidates', None)
    second = second if second is not None else book_phase_tasks()
    remaining = max(30.0, budget_seconds - _elapsed(started_at))
    two = collect_parallel(second, budget_seconds=min(remaining, BOOK_PHASE_SECONDS + 30), max_workers=10,
                           fetcher_factory=fetcher_factory)
    phases.append({'name': 'quotes_and_books', 'elapsed_ms': two['elapsed_ms'], 'budget_seconds': two['budget_seconds']})
    completed = datetime.now(JST)
    return {
        'symbol': SYMBOL, 'collector_version': COLLECTOR_VERSION, 'mode': context['mode'],
        'collection_started_at': context['collection_started_at'],
        'collection_completed_at': completed.isoformat(),
        'news_lookback_hours': context['news_lookback_hours'],
        'last_success_at': context['last_success_at'],
        'budget_seconds': budget_seconds,
        'elapsed_ms': round((completed - started_at).total_seconds() * 1000, 1),
        'budget_exhausted': exhausted or two['budget_exhausted'],
        'phases': phases,
        'sources': sources + two['results'],
        'terms_restricted': [dict(item) for item in TERMS_RESTRICTED],
    }
