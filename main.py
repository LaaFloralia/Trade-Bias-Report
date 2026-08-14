"""ICT Daily Bias Report — スクレイピングオーケストレーター

このスクリプトはデータ取得のみを担当する。
LLM 分析・レポート生成・Brain 保存は `.claude/commands/daily-bias.md`
スラッシュコマンド (Claude Code セッション内で実行) が責任を持つ。

実行方法:
    python main.py            # 日次データ取得 (output/scraped_data_*.{json,txt} を保存)
    python main.py --weekly   # 週次データ取得 (ファイル名 prefix が scraped_data_weekly_ になる)

COT は Daily / Weekly を問わず常時取得する (2026-08 の 2 本体制統合以降)。
--weekly はファイル名 prefix (scraped_data_ / scraped_data_weekly_) の分岐のみを担う。
"""

import asyncio
import sys
import json
from datetime import datetime, timedelta
from pathlib import Path

from config import (
    INSTRUMENTS,
    OPEN_ORDER_SYMBOLS,
    FOMC_DATES,
    FOMC_SCHEDULE_WARN_DAYS,
    DEFAULT_SYMBOL,
    CONTEXT_SYMBOLS,
    XAU_TF_H1_CSV,
)
from scrapers.myfxbook import scrape_myfxbook
from scrapers.fxssi import scrape_fxssi
from scrapers.ig_sentiment import scrape_ig_sentiment
from scrapers.coinglass import scrape_coinglass
from scrapers.cot import fetch_cot_data
from scrapers.twelvedata import fetch_price_data_with_raw
from scrapers.dxy import scrape_dxy
from scrapers.economic_calendar import scrape_economic_calendar
from scrapers.fedwatch import scrape_fedwatch
from scrapers.btc_etf import scrape_btc_etf
from scrapers.fred import fetch_fred_data
from scrapers.validation import validate_all, apply_validation
from scrapers.metadata_schema import normalize_scraper_results
# Deep Bias 強化用スクレイパー (Daily / Weekly 速報用ラインには影響しない)
from scrapers.dxy_components import scrape_dxy_components
from scrapers.vix_structure import scrape_vix_structure
from scrapers.premarket import scrape_premarket
from scrapers.macro_liquidity import scrape_macro_liquidity
from scrapers.rate_spreads import scrape_rate_spreads
from scrapers.crypto_funding import scrape_crypto_funding
from scrapers.myfxbook_open_orders import scrape_myfxbook_open_orders
from scrapers.binance_btc_sentiment import fetch_binance_btc_sentiment
from scrapers.gold_etf import scrape_gold_etf
from scrapers.gold_cb import scrape_gold_cb, _label as _cb_label
from scrapers.report_anchor import load_report_anchor, format_anchor_lines
from scrapers.fedwatch_history import record_snapshot, compute_deltas, format_delta_lines
from scrapers.retail_analytics import build_retail_analytics, format_retail_analytics_lines
from scrapers.cot_disaggregated import fetch_cot_disaggregated, format_disaggregated_lines
from scrapers.correlation import (
    build_correlations,
    daily_closes_from_h1,
    format_correlation_lines,
)
from scrapers.session_stats import compute_session_stats, format_session_stats_lines


def _get_fomc_metadata(today: datetime = None) -> dict:
    """FOMC週判定とメタデータを返す。

    Returns:
        {
            "is_fomc_week": bool,
            "next_fomc_date": str (YYYY-MM-DD),
            "days_until_fomc": int,
            "schedule_warning": str | None,  # 日程テーブル枯渇 90 日前からの警告
        }
    """
    if today is None:
        today = datetime.now()
    today_date = today.date()

    fomc_dates = sorted(datetime.strptime(d, "%Y-%m-%d").date() for d in FOMC_DATES)

    # 日程テーブル枯渇の事前警告（最終登録日の FOMC_SCHEDULE_WARN_DAYS 日前から）
    schedule_warning = None
    last_fomc = fomc_dates[-1]
    days_to_exhaustion = (last_fomc - today_date).days
    if days_to_exhaustion <= FOMC_SCHEDULE_WARN_DAYS:
        schedule_warning = (
            f"FOMC 日程テーブルが残り {max(days_to_exhaustion, 0)} 日で枯渇します"
            f"（最終登録日 {last_fomc}）。次年度日程を config.py に追加してください。"
        )

    # 次回FOMC日を特定
    next_fomc = None
    for fd in fomc_dates:
        if fd >= today_date:
            next_fomc = fd
            break

    if next_fomc is None:
        return {
            "is_fomc_week": False,
            "next_fomc_date": f"未定（{last_fomc.year}年日程終了）",
            "days_until_fomc": -1,
            "schedule_warning": schedule_warning,
        }

    days_until = (next_fomc - today_date).days

    # FOMC週判定: FOMC開催日を含む週の月曜〜金曜
    fomc_weekday = next_fomc.weekday()  # 0=月
    fomc_monday = next_fomc - timedelta(days=fomc_weekday)
    fomc_friday = fomc_monday + timedelta(days=4)
    is_fomc_week = fomc_monday <= today_date <= fomc_friday

    return {
        "is_fomc_week": is_fomc_week,
        "next_fomc_date": next_fomc.strftime("%Y-%m-%d"),
        "days_until_fomc": days_until,
        "schedule_warning": schedule_warning,
    }


async def collect_all_data(weekly: bool = False, symbol: str = None) -> dict:
    """MyFXBook優先でデータを取得し、失敗銘柄はFXSSI→IGの順でフォールバックする。
    COT データは weekly に関係なく常時取得する（weekly 引数はファイル名 prefix 用に
    呼び出し側で使われるのみで、取得内容は Daily / Weekly で同一）。
    新規データソース: DXY, FRED (DGS10/DGS2/DTWEXBGS), 経済指標カレンダー, FedWatch, BTC ETFフロー

    銘柄スコープ (2026-08-12 XAUUSD 特化再設計):
        symbol (既定 config report.default_symbol) + context_symbols のみを
        銘柄固有スクレイパーの対象にする。マクロ層 (FedWatch/FRED/VIX/DXY/
        カレンダー等) は常時取得。BTC 系は BTCUSD がスコープ内の時のみ、
        gold ETF/中銀・リテール分析は XAUUSD がスコープ内の時のみ動く。
    """
    symbol = symbol or DEFAULT_SYMBOL
    scope = {symbol} | set(CONTEXT_SYMBOLS)
    print(f"[1/4] データ取得を開始... (対象: {symbol} + 文脈 {CONTEXT_SYMBOLS})")

    results = {
        "timestamp": datetime.now().isoformat(),
        "price_data": None,  # Twelve Data API
        "retail_sentiment": {},  # 銘柄ごとに1ソースのみ格納
        "coinglass": {},
        "cot": None,  # 常時取得（Daily でもポジショニング分析に使用）
        "dxy": None,
        "fred": None,  # FRED: DGS10 / DGS2 / DTWEXBGS / DFII10 (実質金利) / T10YIE (インフレ期待)
        "economic_calendar": None,
        "fedwatch": None,
        "btc_etf": None,
        # Deep Bias 強化用 (速報用 daily / weekly セクションでは出力されない)
        "dxy_components": None,
        "vix_structure": None,
        "premarket": None,
        "macro_liquidity": None,
        "rate_spreads": None,
        "crypto_funding": None,
        "myfxbook_open_orders": {},  # 銘柄ごとに格納
        "binance_btc_sentiment": None,  # Binance Futures BTCUSDT Long/Short (MyFXBook 非対応の代替)
        # XAUUSD ファンダ大局バイアス用 (master_prompt セクション1.5)
        "gold_etf": None,  # GLD 保有トン数 (SPDR 公式 API)
        "gold_cb": None,   # 中銀ゴールド購入 (IMF IRFCL 報告国ベース)
        # 2026-08-13 追加: 機関内訳・相関定量・セッション統計
        "cot_disaggregated": None,  # Managed Money / Swap Dealer / Producer 内訳
        "correlation": None,        # ローリング相関係数 (ネットワーク不要、既存データの再計算)
        "session_stats": None,      # アジアレンジ / PDH-PDL スイープ率 (H1 から決定論計算)
        # 前回レポート アンカー (Brain ローカル読み込み、オンデマンド運用の自己完結化)
        "report_anchor": None,
    }

    # --- Weekly 前回レビュー入力 (ローカル IO のみ) ---
    # interactive / headless 両フローが同一の照合材料を得るための共通層。
    if weekly:
        try:
            from scrapers.weekly_review import build_weekly_review_block
            results["weekly_review"] = build_weekly_review_block()
            print(f"  [OK]    weekly_review: {'あり' if results['weekly_review'] else '材料なし'}")
        except Exception as e:
            results["weekly_review"] = None
            print(f"  [WARN]  weekly_review: {e}")

    # --- 前回レポート アンカー (ローカル IO のみ、ネットワーク不要) ---
    # デフォルト銘柄 (XAUUSD) 実行時のみ。個別銘柄レポートは前回照合を持たない。
    if symbol == DEFAULT_SYMBOL:
        try:
            results["report_anchor"] = load_report_anchor()
            ra = results["report_anchor"]
            w = ra.get("weekly")
            d = ra.get("prev_daily")
            x = ra.get("xau_tf")
            print(
                "  [OK]    report_anchor: "
                f"Weekly={w['file'] + (' [STALE]' if w['stale'] else '') if w else 'なし'} / "
                f"前回Daily={d['file'] + (' [STALE]' if d['stale'] else '') if d else 'なし'} / "
                f"XAU-TF={x['file'] + (' [STALE]' if x['stale'] else '') if x else 'なし'}"
            )
        except Exception as e:
            results["report_anchor"] = {"error": str(e)}
            print(f"  [WARN]  report_anchor: {e}")

    # --- Twelve Data: 価格データ取得 (スコープ内銘柄のみ) ---
    print("  Twelve Data: 価格データ取得中...")
    try:
        price_text, raw_quotes, raw_series = fetch_price_data_with_raw(instruments=sorted(scope))
        results["price_data"] = price_text
        for sym, quote in raw_quotes.items():
            if quote:
                results[f"_raw_quote_{sym}"] = quote
                results[f"_raw_series_{sym}"] = raw_series.get(sym, [])
        print("  [OK]    Twelve Data: 価格データ取得完了")
    except Exception as e:
        results["price_data"] = f"Twelve Data 取得不可（{e}）"
        print(f"  [ERROR] Twelve Data: {e}")

    # --- Binance Futures: BTC Long/Short 取得（MyFXBook が BTC 非対応のため代替）---
    if "BTCUSD" in scope:
        print("  Binance Futures: BTC Long/Short 取得中...")
        try:
            results["binance_btc_sentiment"] = fetch_binance_btc_sentiment()
            if results["binance_btc_sentiment"].get("error"):
                print(f"  [WARN]  Binance BTC: {results['binance_btc_sentiment']['error']}")
            else:
                print("  [OK]    Binance BTC: Top Trader + Global L/S 取得完了")
        except Exception as e:
            results["binance_btc_sentiment"] = {"error": str(e)}
            print(f"  [ERROR] Binance BTC: {e}")

    # --- Phase 1: MyFXBook + CoinGlass を並列取得 (スコープ内銘柄のみ) ---
    myfxbook_targets = [
        (sym, cfg["myfxbook_slug"])
        for sym, cfg in INSTRUMENTS.items()
        if cfg.get("myfxbook_slug") and sym in scope
    ]
    phase1_tasks = [("myfxbook", sym, scrape_myfxbook(slug)) for sym, slug in myfxbook_targets]
    if "BTCUSD" in scope:
        phase1_tasks.append(("coinglass", "BTCUSD", scrape_coinglass()))

    print(f"  Phase 1: {len(phase1_tasks)} タスクを並列実行中（MyFXBook + CoinGlass）...")
    phase1_results = await asyncio.gather(*[t[2] for t in phase1_tasks], return_exceptions=True)

    failed_symbols = []
    for i, (source, symbol, _) in enumerate(phase1_tasks):
        res = phase1_results[i]
        if source == "coinglass":
            if isinstance(res, Exception):
                results["coinglass"]["BTCUSD"] = {"error": str(res)}
                print(f"  [ERROR] coinglass/BTCUSD: {res}")
            else:
                results["coinglass"]["BTCUSD"] = res
                print(f"  [WARN]  coinglass/BTCUSD: {res['error']}" if res.get("error") else f"  [OK]    coinglass/BTCUSD")
        else:
            # MyFXBook: long_pct が取得できていれば成功
            ok = not isinstance(res, Exception) and isinstance(res, dict) and res.get("long_pct") is not None
            if ok:
                results["retail_sentiment"][symbol] = {**res, "_fallback": None}
                print(f"  [OK]    myfxbook/{symbol}")
            else:
                err = str(res) if isinstance(res, Exception) else (res.get("error") if isinstance(res, dict) else "不明")
                print(f"  [WARN]  myfxbook/{symbol}: {err} → フォールバック予定")
                failed_symbols.append(symbol)

    # --- Phase 2: 失敗銘柄のフォールバック（FXSSI → IG）---
    if failed_symbols:
        print(f"  Phase 2: フォールバック取得中（{failed_symbols}）...")
        fxssi_result = await scrape_fxssi()
        fxssi_data = fxssi_result.get("data", {}) if not fxssi_result.get("error") else {}

        ig_needed = []
        for symbol in failed_symbols:
            if symbol in fxssi_data:
                d = fxssi_data[symbol]
                results["retail_sentiment"][symbol] = {
                    "source": "FXSSI",
                    "symbol": symbol,
                    "long_pct": d.get("buy_pct"),
                    "short_pct": d.get("sell_pct"),
                    "avg_long_entry": None,
                    "avg_short_entry": None,
                    "_fallback": "FXSSI",
                    "error": None,
                }
                print(f"  [OK]    fxssi/{symbol} (フォールバック)")
            else:
                ig_needed.append(symbol)
                print(f"  [WARN]  fxssi/{symbol}: データなし → IG試行")

        if ig_needed:
            ig_results = await asyncio.gather(*[scrape_ig_sentiment(sym) for sym in ig_needed], return_exceptions=True)
            for i, symbol in enumerate(ig_needed):
                res = ig_results[i]
                ok = not isinstance(res, Exception) and isinstance(res, dict) and res.get("long_pct") is not None
                if ok:
                    results["retail_sentiment"][symbol] = {**res, "_fallback": "IG"}
                    print(f"  [OK]    ig/{symbol} (フォールバック)")
                else:
                    err = str(res) if isinstance(res, Exception) else (res.get("error") if isinstance(res, dict) else "取得不可")
                    results["retail_sentiment"][symbol] = {
                        "source": "IG",
                        "symbol": symbol,
                        "long_pct": None,
                        "short_pct": None,
                        "_fallback": "IG",
                        "error": err,
                    }
                    print(f"  [ERROR] ig/{symbol}: {err}")

    # --- COT データ取得（常時。Daily でも機関ポジショニングに使う。スコープ内銘柄のみ）---
    print("  COT: CFTC APIからデータ取得中...")
    cot_targets = [
        (cfg["cot"]["label"], cfg["cot"]["market"])
        for sym, cfg in sorted(
            ((s, c) for s, c in INSTRUMENTS.items() if c.get("cot") and s in scope),
            key=lambda item: item[1]["cot"]["order"],
        )
    ]
    try:
        cot = fetch_cot_data(targets=cot_targets)
        results["cot"] = cot
        if cot.get("error"):
            print(f"  [WARN]  COT: 一部エラー: {cot['error']}")
        else:
            print(f"  [OK]    COT: Report Date {cot['report_date']}")
    except Exception as e:
        results["cot"] = {"text": None, "error": str(e)}
        print(f"  [ERROR] COT: {e}")

    # --- 新規データソース（全実行で取得）---
    # FOMC週判定（FedWatchスクレイピングの要否を決定）
    fomc_meta = _get_fomc_metadata()
    is_fomc_week = fomc_meta["is_fomc_week"]
    if fomc_meta.get("schedule_warning"):
        print(f"  [WARN]  {fomc_meta['schedule_warning']}")
    print(f"  FOMC判定: is_fomc_week={is_fomc_week}, next={fomc_meta['next_fomc_date']}, "
          f"days_until={fomc_meta['days_until_fomc']}")

    # Twelve Data /quote 競合回避のため 2 段階で取得する。
    # Phase A (Twelve Data 非依存): 全部並列で OK
    # Phase B (Twelve Data /quote 依存): 直列で 7 秒間隔
    #   理由: TD 無料枠 8 calls/min。fetch_price_data_with_raw() で既に /quote+/time_series=2 calls 消費、
    #         dxy_components / premarket でさらに 2 calls 必要。並列だと burst で 429 を踏む。
    # FedWatch は is_fomc_week 分岐を撤廃し常時取得 (Deep Bias 強化要件)
    phase_a_tasks = [
        ("dxy", scrape_dxy()),
        ("fred", fetch_fred_data()),
        ("economic_calendar", scrape_economic_calendar()),
        ("fedwatch", scrape_fedwatch()),
        # vix_structure は CBOE Dashboard を 4 ページ直列取得するため重い。Phase B へ移動。
        ("macro_liquidity", scrape_macro_liquidity()),
        ("rate_spreads", scrape_rate_spreads()),
    ]
    # 銘柄スコープ依存: BTC 系は BTCUSD、金フロー系は XAUUSD がスコープ内の時のみ
    if "BTCUSD" in scope:
        phase_a_tasks.append(("btc_etf", scrape_btc_etf()))
        phase_a_tasks.append(("crypto_funding", scrape_crypto_funding()))
    if "XAUUSD" in scope:
        phase_a_tasks.append(("gold_etf", scrape_gold_etf()))
        phase_a_tasks.append(("gold_cb", scrape_gold_cb()))
    print(f"  Phase A: {len(phase_a_tasks)} 系を並列取得中（DXY/FRED/Calendar/BTC ETF/FedWatch + Deep 強化大半）...")
    phase_a_results = await asyncio.gather(*[t[1] for t in phase_a_tasks], return_exceptions=True)

    for i, (key, _) in enumerate(phase_a_tasks):
        res = phase_a_results[i]
        if isinstance(res, Exception):
            results[key] = {"error": str(res)}
            print(f"  [ERROR] {key}: {res}")
        else:
            results[key] = res
            err = res.get("error") if isinstance(res, dict) else None
            if err:
                print(f"  [WARN]  {key}: {err}")
            else:
                print(f"  [OK]    {key}")

    # --- FedWatch スナップショット履歴 + 前日比/前週比の決定論的計算 ---
    fw = results.get("fedwatch")
    if isinstance(fw, dict) and fw.get("target_rates"):
        try:
            saved = record_snapshot(fw)
            fw["deltas"] = compute_deltas(fw)
            print(f"  [OK]    fedwatch_history: snapshot={'saved' if saved else 'skipped'}, "
                  f"前日比={'あり' if fw['deltas'].get('prev_day') else 'なし'}, "
                  f"前週比={'あり' if fw['deltas'].get('prev_week') else 'なし'}")
        except Exception as e:
            print(f"  [WARN]  fedwatch_history: {e}")

    # Phase B: Twelve Data /quote 系 + CBOE Dashboard 直列実行
    # - Twelve Data: 8 calls/min 制限回避のため間隔を空ける
    # - CBOE Dashboard: 並列だと Playwright 競合で失敗するため直列実行する vix_structure
    print("  Phase B: 直列実行（dxy_components → 7s → premarket → vix_structure）...")
    try:
        results["dxy_components"] = await scrape_dxy_components()
        err_d = results["dxy_components"].get("error") if isinstance(results["dxy_components"], dict) else None
        print(f"  [{'WARN' if err_d else 'OK'}]  dxy_components: {err_d or 'OK'}")
    except Exception as e:
        results["dxy_components"] = {"error": str(e)}
        print(f"  [ERROR] dxy_components: {e}")

    # 7 秒待機して Twelve Data 1 分窓内の連続呼び出しを回避
    await asyncio.sleep(7)

    try:
        results["premarket"] = await scrape_premarket()
        err_p = results["premarket"].get("error") if isinstance(results["premarket"], dict) else None
        print(f"  [{'WARN' if err_p else 'OK'}]  premarket: {err_p or 'OK'}")
    except Exception as e:
        results["premarket"] = {"error": str(e)}
        print(f"  [ERROR] premarket: {e}")

    # vix_structure を直列取得 (CBOE Dashboard 4 ページ + FRED)
    try:
        results["vix_structure"] = await scrape_vix_structure()
        err_v = results["vix_structure"].get("error") if isinstance(results["vix_structure"], dict) else None
        vals = results["vix_structure"].get("values", {}) if isinstance(results["vix_structure"], dict) else {}
        print(f"  [{'WARN' if err_v else 'OK'}]  vix_structure: {len(vals)} series ({err_v or sorted(vals.keys())})")
    except Exception as e:
        results["vix_structure"] = {"error": str(e)}
        print(f"  [ERROR] vix_structure: {e}")

    # --- MyFXBook Open Orders 並列取得 (Deep Bias 強化、対象は config.yaml の open_orders ∩ スコープ) ---
    open_order_targets = [s for s in OPEN_ORDER_SYMBOLS if s in scope]
    print(f"  MyFXBook Open Orders: {open_order_targets} を並列取得中...")

    def _quote_close(sym: str):
        """TwelveData quote から現在価格ヒントを取り出す（クロスチェック用）。"""
        q = results.get(f"_raw_quote_{sym}")
        try:
            return float(q["close"]) if isinstance(q, dict) and q.get("close") else None
        except (TypeError, ValueError):
            return None

    oo_tasks = [
        scrape_myfxbook_open_orders(s, current_price_hint=_quote_close(s))
        for s in open_order_targets
    ]
    oo_results = await asyncio.gather(*oo_tasks, return_exceptions=True)
    for sym, res in zip(open_order_targets, oo_results):
        if isinstance(res, Exception):
            results["myfxbook_open_orders"][sym] = {"error": str(res)}
            print(f"  [ERROR] open_orders/{sym}: {res}")
        else:
            results["myfxbook_open_orders"][sym] = res
            if res.get("error"):
                print(f"  [WARN]  open_orders/{sym}: {res['error']}")
            else:
                # 新スキーマ: bid_count + ask_count + bsl_candidates + ssl_candidates
                bsl_n = len(res.get("bsl_candidates", []))
                ssl_n = len(res.get("ssl_candidates", []))
                cp = res.get("current_price")
                print(
                    f"  [OK]    open_orders/{sym}: "
                    f"bids={res.get('bid_count', 0)} asks={res.get('ask_count', 0)} "
                    f"BSL clusters={bsl_n} SSL clusters={ssl_n} (current_price={cp})"
                )

    # --- 機関ポジショニング内訳 (Disaggregated COT、XAUUSD スコープ時のみ) ---
    if "XAUUSD" in scope:
        gold_market = INSTRUMENTS["XAUUSD"]["cot"]["market"]
        print("  COT Disaggregated: Managed Money / Swap Dealer 内訳を取得中...")
        try:
            results["cot_disaggregated"] = fetch_cot_disaggregated(gold_market)
            err = results["cot_disaggregated"].get("error")
            print(f"  [{'WARN' if err else 'OK'}]  cot_disaggregated: {err or 'OK'}")
        except Exception as e:
            results["cot_disaggregated"] = {"error": str(e)}
            print(f"  [ERROR] cot_disaggregated: {e}")

    # --- 相関定量 + セッション統計 (ネットワーク不要、既存データからの決定論計算) ---
    if symbol == DEFAULT_SYMBOL:
        try:
            xau_closes = daily_closes_from_h1(XAU_TF_H1_CSV)
            results["correlation"] = build_correlations(xau_closes, results.get("fred") or {})
            pairs = results["correlation"].get("pairs", [])
            print(f"  [OK]    correlation: {len(pairs)} ペア算出"
                  f"（{', '.join(p['verdict'] for p in pairs)}）")
        except Exception as e:
            results["correlation"] = {"error": str(e)}
            print(f"  [WARN]  correlation: {e}")

        try:
            results["session_stats"] = compute_session_stats(XAU_TF_H1_CSV)
            st = results["session_stats"]
            status = st.get("error") or f"標本 {st.get('sample_days')}日"
            print(f"  [{'WARN' if st.get('error') else 'OK'}]  session_stats: {status}")
        except Exception as e:
            results["session_stats"] = {"error": str(e)}
            print(f"  [WARN]  session_stats: {e}")

    # --- リテール分析 (デフォルト銘柄 = XAUUSD 実行時のみ):
    #     P/L 構造 + リクイディティプール + スイープ検証 ---
    if symbol == DEFAULT_SYMBOL:
        target = DEFAULT_SYMBOL
        try:
            oo_target = results["myfxbook_open_orders"].get(target) or {}
            cp = oo_target.get("current_price") or _quote_close(target)
            # 前回 Daily の発行時刻。スイープ検証に渡すと「前回予測より後に
            # 起きたか」が各イベントに付き、前回照合の的中判定が甘くならない
            _anchor = results.get("report_anchor")
            _prev_daily = _anchor.get("prev_daily") if isinstance(_anchor, dict) else None
            _prev_at = _prev_daily.get("generated_at") if isinstance(_prev_daily, dict) else None
            results["retail_analytics"] = build_retail_analytics(
                retail=results["retail_sentiment"].get(target) or {},
                open_orders=oo_target,
                current_price=cp,
                h1_path=XAU_TF_H1_CSV,
                prev_report_at=_prev_at,
            )
            ra = results["retail_analytics"]
            print(
                f"  [OK]    retail_analytics/{target}: "
                f"ATR20d={ra.get('atr20d')} pools(BSL/SSL)="
                f"{len(ra['top_pools']['bsl'])}/{len(ra['top_pools']['ssl'])} "
                f"sweeps={len(ra.get('sweep_events', []))} "
                f"baseline={ra.get('baseline_date') or '当日'}"
            )
        except Exception as e:
            results["retail_analytics"] = {"error": str(e)}
            print(f"  [WARN]  retail_analytics/{target}: {e}")

    # 共通メタデータスキーマ補完（source/symbol/timestamp/as_of_date/
    # stale/fallback_used/error/note）。既存キーは上書きしない。
    normalize_scraper_results(results)

    return results


def format_scraped_data(data: dict) -> str:
    """取得データをClaude APIに渡すテキスト形式に整形する。
    リテールセンチメントは銘柄ごとに1ソースのみ表示する。
    バリデーション処理を実行し、異常データを除外する。
    """
    # --- バリデーション実行 ---
    validation_results = validate_all(data)

    # FRED 結果は DXY フォールバック表示でも参照するため早めに参照確保
    fred = data.get("fred") or {}

    lines = []
    lines.append(f"データ取得日時: {data['timestamp']}")
    lines.append("")

    # --- 前回レポート アンカー (Weekly 大局 + 前回 Daily の継続性チェック用) ---
    anchor = data.get("report_anchor")
    if anchor and isinstance(anchor, dict):
        lines.extend(format_anchor_lines(anchor))
        lines.append("")

    # --- Weekly 前回レビュー入力 (前回想定との答え合わせ用、weekly 実行時のみ) ---
    weekly_review = data.get("weekly_review")
    if weekly_review:
        lines.append(weekly_review)
        lines.append("")

    # --- 価格データ（Twelve Data API）---
    price_data = data.get("price_data")
    if price_data:
        lines.append(price_data)
        lines.append("")

    # --- DXY 価格データ ---
    dxy = data.get("dxy")
    if dxy and isinstance(dxy, dict) and dxy.get("current_price") is not None:
        dxy_issues = validation_results.get("DXY", [])
        lines.append("[DXY (スクレイピング)]")
        lines.append(
            f"現在値: {dxy['current_price']:,.3f} | 前日終値: {dxy.get('prev_close', 'N/A')} | "
            f"前日比: {dxy.get('change', 'N/A')} ({dxy.get('change_pct', 'N/A')}%)"
        )
        if dxy.get("note"):
            lines.append(f"※ {dxy['note']}")

        # PDH/PDL等の出力（バリデーション結果を反映）
        for h_key, l_key, label in [("pdh", "pdl", "PDH/PDL"), ("pwh", "pwl", "PWH/PWL"), ("pmh", "pml", "PMH/PML")]:
            h, l = dxy.get(h_key), dxy.get(l_key)
            has_issue = any(label in issue for issue in dxy_issues)
            if has_issue:
                issue_msg = next((i for i in dxy_issues if label in i), "")
                lines.append(f"{label}: データ異常: {issue_msg}")
            elif h is not None and l is not None:
                note = "（EUR/USD逆数から推定）" if dxy.get("estimated") else ""
                lines.append(f"{label.split('/')[0]}: {h:,.3f} / {label.split('/')[1]}: {l:,.3f}{note}")
            else:
                lines.append(f"{label}: 取得不可")

        # IPDA レベル
        for days, h_key, l_key in [
            (20, "ipda_20_high", "ipda_20_low"),
            (40, "ipda_40_high", "ipda_40_low"),
            (60, "ipda_60_high", "ipda_60_low"),
        ]:
            h, l = dxy.get(h_key), dxy.get(l_key)
            if h is not None and l is not None:
                lines.append(f"IPDA {days}日: High {h:,.3f} / Low {l:,.3f}")
            else:
                lines.append(f"IPDA {days}日: 未取得")

        lines.append(f"ソース: {dxy.get('source', '不明')}")
        lines.append("")
    elif dxy and isinstance(dxy, dict) and dxy.get("error"):
        lines.append(f"[DXY] 取得不可（{dxy['error']}）")
        # DXY 失敗時のフォールバック: Broad USD Index (DTWEXBGS) を proxy として明示
        dtwex_fb = fred.get("DTWEXBGS") if isinstance(fred, dict) else None
        if isinstance(dtwex_fb, dict) and dtwex_fb.get("value") is not None:
            lines.append(
                f"※ DXY unavailable; using Broad USD Index proxy "
                f"(FRED DTWEXBGS = {dtwex_fb['value']:.3f}, "
                f"as_of {dtwex_fb.get('as_of_date', 'N/A')}, fallback_used=true)"
            )
        lines.append("")

    # --- FRED Treasury yields (US10Y / US2Y) — DGS10 / DGS2 で完全置換（旧 Investing.com / CNBC スクレイピング廃止）---
    def _emit_fred_yield(label: str, series_id: str):
        series = fred.get(series_id) if isinstance(fred, dict) else None
        if not isinstance(series, dict) or series.get("value") is None:
            err = series.get("error", "取得不可") if isinstance(series, dict) else "取得不可"
            lines.append(f"[{label}] 取得不可（FRED {series_id}: {err}）")
            lines.append("")
            return
        change = series.get("change")
        change_str = f"{change:+.3f}" if change is not None else "N/A"
        stale_tag = " [STALE]" if series.get("stale") else ""
        lines.append(f"[{label}]{stale_tag}")
        # 20 営業日比: ファンダ大局バイアス (master_prompt セクション1.5) の中期トレンド判定用
        change_20 = series.get("change_20obs")
        if change_20 is not None:
            trend = "上昇" if change_20 > 0.05 else ("低下" if change_20 < -0.05 else "横ばい")
            trend_str = f" | 20営業日比: {change_20:+.3f} (トレンド: {trend})"
        else:
            trend_str = ""
        lines.append(
            f"現在利回り: {series['value']:.3f}% | 前日比: {change_str}{trend_str} | "
            f"as_of: {series.get('as_of_date', 'N/A')}"
        )
        lines.append(f"ソース: FRED {series_id}")
        if series.get("note"):
            lines.append(f"※ {series['note']}")
        lines.append("")

    _emit_fred_yield("US10Y", "DGS10")
    _emit_fred_yield("US2Y", "DGS2")
    # XAUUSD マクロドライバー: 実質金利とインフレ期待（DGS10 ≒ DFII10 + T10YIE）
    _emit_fred_yield("US10Y Real (TIPS)", "DFII10")
    _emit_fred_yield("10Y Breakeven Inflation", "T10YIE")

    # --- Broad USD Index (FRED DTWEXBGS) — USD macro proxy。DXY とは別物として明示 ---
    dtwex = fred.get("DTWEXBGS") if isinstance(fred, dict) else None
    if isinstance(dtwex, dict) and dtwex.get("value") is not None:
        change = dtwex.get("change")
        change_str = f"{change:+.3f}" if change is not None else "N/A"
        stale_tag = " [STALE]" if dtwex.get("stale") else ""
        change_20 = dtwex.get("change_20obs")
        trend_str = f" | 20営業日比: {change_20:+.3f}" if change_20 is not None else ""
        lines.append(f"[Broad USD Index]{stale_tag}")
        lines.append(
            f"現在値: {dtwex['value']:.3f} | 前日比: {change_str}{trend_str} | "
            f"as_of: {dtwex.get('as_of_date', 'N/A')}"
        )
        lines.append("ソース: FRED DTWEXBGS（Broad USD Index, USD macro proxy / NOT DXY）")
        if dtwex.get("note"):
            lines.append(f"※ {dtwex['note']}")
        lines.append("")
    elif isinstance(dtwex, dict) and dtwex.get("error"):
        lines.append(f"[Broad USD Index] 取得不可（FRED DTWEXBGS: {dtwex['error']}）")
        lines.append("")

    # --- リテールセンチメント（銘柄ごとに1ソース）---
    lines.append("### リテールポジション (Retail Sentiment)")
    for symbol, d in data.get("retail_sentiment", {}).items():
        if not isinstance(d, dict):
            lines.append(f"- {symbol}: 取得不可")
            continue

        source = d.get("source", "不明")
        fallback = d.get("_fallback")
        long_pct = d.get("long_pct")
        short_pct = d.get("short_pct")

        if long_pct is not None:
            line = f"- {symbol} ({source}): Long {long_pct}% / Short {short_pct}%"
            if d.get("avg_long_entry"):
                line += f", 平均ロング {d['avg_long_entry']:,.4g}"
            if d.get("avg_short_entry"):
                line += f", 平均ショート {d['avg_short_entry']:,.4g}"
            lines.append(line)
            if d.get("long_volume_lots") is not None or d.get("long_positions") is not None:
                lines.append(
                    f"  建玉実数: Long {d.get('long_volume_lots', 'N/A')} lots / "
                    f"{d.get('long_positions', 'N/A')} positions, "
                    f"Short {d.get('short_volume_lots', 'N/A')} lots / "
                    f"{d.get('short_positions', 'N/A')} positions"
                )
            if fallback:
                lines.append(f"  ※ MyFXBook取得不可のため{source}にフォールバック")
        else:
            err = d.get("error", "取得不可")
            lines.append(f"- {symbol}: 取得不可（{err}）")
    lines.append("")

    # --- CoinGlass (BTCUSD がスコープ内の実行でのみデータが存在する) ---
    if data.get("coinglass"):
        lines.append("### CoinGlass (BTCUSD)")
        cg = data.get("coinglass", {}).get("BTCUSD", {})
        if isinstance(cg, dict):
            if cg.get("long_short_ratio") is not None:
                lines.append(f"- Long/Short Ratio: {cg['long_short_ratio']}")
            if cg.get("long_pct") is not None:
                lines.append(f"- Long/Short: {cg['long_pct']}% / {cg['short_pct']}%")
            if cg.get("funding_rate") is not None:
                lines.append(f"- Funding Rate: {cg['funding_rate']}%")
            if cg.get("error"):
                lines.append(f"- エラー: {cg['error']}")
        else:
            lines.append("- BTCUSD: 取得不可")

    # --- Binance Top Trader vs Global（BTCUSD のリテール／プロ センチメント差）---
    # MyFXBook が BTC 非対応のため、取引所機関データで代替。
    # Top Trader (Position/Account) = プロ寄り、Global = リテール寄り。差分から divergence を読む。
    binance_btc = data.get("binance_btc_sentiment")
    if binance_btc and isinstance(binance_btc, dict):
        lines.append("")
        lines.append("### Binance Top Trader vs Global (BTCUSD, 1h)")
        if binance_btc.get("top_trader_position_long_pct") is not None:
            lines.append(
                f"- Top Trader Position（実ポジション量、プロ寄り）: "
                f"Long {binance_btc['top_trader_position_long_pct']}% / "
                f"Short {binance_btc['top_trader_position_short_pct']}% "
                f"(L/S ratio {binance_btc['top_trader_position_ls_ratio']})"
            )
        if binance_btc.get("top_trader_account_long_pct") is not None:
            lines.append(
                f"- Top Trader Account（アカウント数、プロ寄り）: "
                f"Long {binance_btc['top_trader_account_long_pct']}% / "
                f"Short {binance_btc['top_trader_account_short_pct']}% "
                f"(L/S ratio {binance_btc['top_trader_account_ls_ratio']})"
            )
        if binance_btc.get("global_long_pct") is not None:
            lines.append(
                f"- Global Account（全アカウント、リテール寄り）: "
                f"Long {binance_btc['global_long_pct']}% / "
                f"Short {binance_btc['global_short_pct']}% "
                f"(L/S ratio {binance_btc['global_ls_ratio']})"
            )
        # divergence の自動算出
        if (
            binance_btc.get("top_trader_position_long_pct") is not None
            and binance_btc.get("global_long_pct") is not None
        ):
            div = round(
                binance_btc["global_long_pct"] - binance_btc["top_trader_position_long_pct"],
                2,
            )
            sign = "+" if div > 0 else ""
            interp = (
                "リテール強気・プロ弱気（ETF流出と整合しやすい）" if div > 5
                else "リテール弱気・プロ強気（反転シグナル候補）" if div < -5
                else "Global と Top Trader の偏りは小さい（divergence なし）"
            )
            lines.append(f"- Divergence (Global − Top Trader): {sign}{div}pp → {interp}")
        if binance_btc.get("error"):
            lines.append(f"- エラー: {binance_btc['error']}")
        lines.append("- ソース: Binance Futures Public API（無料・認証不要）")

    # --- BTC ETFフロー ---
    btc_etf = data.get("btc_etf")
    if btc_etf and isinstance(btc_etf, dict):
        lines.append("")
        lines.append("### BTC ETF フロー")
        if btc_etf.get("daily_flows"):
            lines.append(f"ソース: {btc_etf.get('source', '不明')}")
            for day in btc_etf["daily_flows"]:
                flows = day.get("flows", {})
                # None は「未発表」と明示（ソース側で当該日の数値がまだ公開されていない）
                flow_parts = []
                for etf, v in flows.items():
                    if v is not None:
                        flow_parts.append(f"{etf}: {v:+.1f}M")
                    else:
                        flow_parts.append(f"{etf}: 未発表")
                total = day.get("total")
                total_str = f"合計: {total:+.1f}M" if total is not None else "合計: 未発表"
                lines.append(f"- {day.get('date', 'N/A')}: {', '.join(flow_parts) + ', ' if flow_parts else ''}{total_str}")
        elif btc_etf.get("error"):
            lines.append(f"取得不可（{btc_etf['error']}）")

    # --- 金ETFフロー (GLD 保有量) — XAUUSD ファンダ大局用 ---
    gold_etf = data.get("gold_etf")
    if gold_etf and isinstance(gold_etf, dict):
        lines.append("")
        lines.append("### 金ETFフロー (GLD 保有量)")
        if gold_etf.get("error"):
            lines.append(f"取得不可（{gold_etf['error']}）")
        else:
            lines.append(
                f"保有量: {gold_etf.get('tonnes')} t (as_of {gold_etf.get('as_of_date')}) | "
                f"ソース: {gold_etf.get('source', '不明')}"
            )
            for f in gold_etf.get("daily_flows", []):
                lines.append(f"- {f['date']}: {f['tonnes']} t ({f['change_t']:+.2f} t)")
            c5, c20 = gold_etf.get("change_5d_t"), gold_etf.get("change_20d_t")
            c5_str = f"{c5:+.2f} t" if c5 is not None else "N/A"
            c20_str = f"{c20:+.2f} t" if c20 is not None else "N/A"
            lines.append(f"- 5営業日累計: {c5_str} / 20営業日累計: {c20_str}")
            if gold_etf.get("streak_direction"):
                d_ja = "流入" if gold_etf["streak_direction"] == "inflow" else "流出"
                lines.append(f"- 連続方向: {gold_etf['streak_days']}営業日連続{d_ja}")
            lines.append("- 解釈: 保有増 = 機関の買い圧力 / 保有減 = 売り圧力")

    # --- 中銀ゴールド購入 (IMF IRFCL) — XAUUSD ファンダ大局用 ---
    gold_cb = data.get("gold_cb")
    if gold_cb and isinstance(gold_cb, dict):
        lines.append("")
        lines.append("### 中銀ゴールド購入 (IMF IRFCL 報告国ベース)")
        if gold_cb.get("error"):
            lines.append(f"取得不可（{gold_cb['error']}）")
        else:
            for m in gold_cb.get("months", []):
                movers = ", ".join(f"{_cb_label(c)} {v:+.1f}" for c, v in m.get("top_movers", []))
                partial_tag = " [速報・報告国少]" if m.get("partial") else ""
                lines.append(
                    f"- {m['period']}: 純変化 {m['net_tonnes']:+.1f} t "
                    f"(報告 {m['reporters']} カ国){partial_tag}"
                    + (f" — 主な動き: {movers}" if movers else "")
                )
            regime_ja = {
                "net_buying": "中銀は買い越し基調",
                "net_selling": "中銀は売り越し基調",
                "neutral": "中立",
                "unknown": "確定月不足で判定不能",
            }.get(gold_cb.get("regime"), "不明")
            periods = gold_cb.get("cumulative_periods") or []
            # 累計の対象月を明示する（上のリストは速報月を含む直近3ヶ月なので、
            # 累計に使った確定月がリストに出てこないことがある）
            span = f"（{periods[-1]}〜{periods[0]}）" if len(periods) >= 2 else ""
            lines.append(
                f"- 確定月3ヶ月累計{span}: {gold_cb.get('cumulative_3m_t'):+.1f} t → "
                f"レジーム: {gold_cb.get('regime')} ({regime_ja})"
            )
            shown = {m["period"] for m in gold_cb.get("months", [])}
            missing = [p for p in periods if p not in shown]
            if missing:
                lines.append(
                    f"  ※ 累計に含まれるが上の内訳に出ていない確定月: {', '.join(missing)}"
                    "（内訳表示は直近3ヶ月のみ）"
                )
            if gold_cb.get("note"):
                lines.append(f"※ {gold_cb['note']}")

    # --- 経済指標カレンダー ---
    calendar = data.get("economic_calendar")
    if calendar and isinstance(calendar, dict):
        lines.append("")
        lines.append("### 経済指標カレンダー（ハイインパクト）")
        if calendar.get("events"):
            for ev in calendar["events"]:
                lines.append(
                    f"- {ev.get('date', '')} {ev.get('time_jst', '')} | "
                    f"{ev.get('country', '')} | {ev.get('indicator', '')} | "
                    f"前回: {ev.get('previous', 'N/A')} | 予想: {ev.get('forecast', 'N/A')}"
                )
        elif calendar.get("error"):
            lines.append(f"取得不可（{calendar['error']}）")
        else:
            lines.append("該当なし")

    # --- FedWatch（常時取得、Deep Bias 強化）---
    # 旧 is_fomc_week 分岐は撤廃。平時も次回 FOMC への利下げ確率を追跡する。
    fomc_meta = _get_fomc_metadata()
    lines.append("")
    lines.append("### FedWatch（常時取得 / Deep Bias 強化）")
    lines.append(f"is_fomc_week: {str(fomc_meta['is_fomc_week']).lower()}")
    lines.append(f"next_fomc_date: {fomc_meta['next_fomc_date']}")
    lines.append(f"days_until_fomc: {fomc_meta['days_until_fomc']}")

    fedwatch = data.get("fedwatch")
    if fedwatch and isinstance(fedwatch, dict) and any(
        fedwatch.get(k) is not None for k in ["hold_pct", "cut_25bp_pct", "cut_50bp_pct"]
    ):
        if fedwatch.get("hold_pct") is not None:
            lines.append(f"- 据え置き確率: {fedwatch['hold_pct']}%")
        if fedwatch.get("cut_25bp_pct") is not None:
            lines.append(f"- 25bp利下げ確率: {fedwatch['cut_25bp_pct']}%")
        if fedwatch.get("cut_50bp_pct") is not None:
            lines.append(f"- 50bp利下げ確率: {fedwatch['cut_50bp_pct']}%")
        if fedwatch.get("hike_25bp_pct") is not None:
            lines.append(f"- 25bp利上げ確率: {fedwatch['hike_25bp_pct']}%")
        # レートレンジ別確率 + 前日比/前週比（fedwatch_history が計算済みの値を出力）
        lines.extend(format_delta_lines(fedwatch))
        if fedwatch.get("next_fomc_date"):
            lines.append(f"- 次回FOMC（ソース表記）: {fedwatch['next_fomc_date']}")
        if fedwatch.get("source"):
            lines.append(f"- ソース: {fedwatch['source']}")
    elif fedwatch and isinstance(fedwatch, dict) and fedwatch.get("error"):
        lines.append(f"- 取得不可（{fedwatch['error']}）")
    else:
        lines.append("- 取得不可（CME / Investing.com 両ソースから値抽出失敗）")

    # ============================================================
    # Deep 強化セクション (旧 master_prompt_deep.md 由来。2026-08 の統合後は
    # master_prompt.md / master_prompt_weekly.md が S2-X / S5-X / S6-X 相当を参照)
    # ============================================================

    # --- DXY 構成通貨スプレッド分解 (S2-X) ---
    dxy_comp = data.get("dxy_components")
    if dxy_comp and isinstance(dxy_comp, dict) and dxy_comp.get("components"):
        lines.append("")
        lines.append("### Deep: DXY 構成通貨分解 (S2-X)")
        lines.append(f"推定 DXY 前日比 (構成寄与合計): {dxy_comp.get('estimated_dxy_change_pct')}%")
        lead = dxy_comp.get("leading_driver")
        lead_v = dxy_comp.get("leading_contribution")
        if lead:
            lines.append(f"主要ドライバー: {lead} (寄与 {lead_v:+.4f}%)")
        for c in dxy_comp["components"]:
            if c.get("change_pct") is not None:
                lines.append(
                    f"- {c['symbol']} (w={c['weight']:.3f}): "
                    f"前日比 {c['change_pct']:+.3f}% → DXY 寄与 {c.get('dxy_contribution'):+.4f}%"
                )
            else:
                lines.append(f"- {c['symbol']} (w={c['weight']:.3f}): 取得不可")
    elif dxy_comp and isinstance(dxy_comp, dict) and dxy_comp.get("error"):
        lines.append("")
        lines.append(f"### Deep: DXY 構成通貨分解 (S2-X) — 取得不可（{dxy_comp['error']}）")

    # --- VIX ターム構造 (S5-X ボラ環境) ---
    vix = data.get("vix_structure")
    if vix and isinstance(vix, dict) and vix.get("values"):
        lines.append("")
        lines.append("### Deep: VIX ターム構造 (S5-X ボラ環境)")
        for k, v in vix["values"].items():
            lines.append(f"- {k}: {v}")
        lines.append(f"- ターム構造: {vix.get('term_structure')}")
        lines.append(f"- VIX レベル区分: {vix.get('vix_level_regime')}")
        if vix.get("short_term_event_alert"):
            lines.append("- 短期イベント警戒 (VIX9D > VIX×1.05)")
    elif vix and isinstance(vix, dict) and vix.get("error"):
        lines.append("")
        lines.append(f"### Deep: VIX ターム構造 (S5-X) — 取得不可（{vix['error']}）")

    # --- US 株指数プリマーケット ---
    pre = data.get("premarket")
    if pre and isinstance(pre, dict) and pre.get("indices"):
        lines.append("")
        lines.append("### Deep: US 株指数プリマーケット (S5)")
        lines.append(f"- リスクレジーム: {pre.get('risk_regime')}")
        for sym, vals in pre["indices"].items():
            if vals.get("error"):
                lines.append(f"- {sym}: 取得不可 ({vals['error']})")
                continue
            cp = vals.get("change_pct")
            cur = vals.get("current")
            lines.append(
                f"- {sym}: 現在 {cur} | 前日比 {cp:+.2f}% (O {vals.get('open')} / H {vals.get('high')} / L {vals.get('low')})"
            )
    elif pre and isinstance(pre, dict) and pre.get("error"):
        lines.append("")
        lines.append(f"### Deep: US 株指数プリマーケット — 取得不可（{pre['error']}）")

    # --- マクロ流動性 ---
    liq = data.get("macro_liquidity")
    if liq and isinstance(liq, dict) and liq.get("series"):
        lines.append("")
        lines.append("### Deep: マクロ流動性 (S6-X Net Liquidity)")
        lines.append(
            f"- Net Liquidity: {liq.get('net_liquidity_b')} B USD "
            f"(前回比 {liq.get('net_liquidity_change_b'):+.2f} B USD, regime: {liq.get('regime')})"
            if liq.get('net_liquidity_change_b') is not None
            else f"- Net Liquidity: {liq.get('net_liquidity_b')} B USD (regime: {liq.get('regime')})"
        )
        for sid, s in liq["series"].items():
            if s.get("value") is not None:
                stale_tag = " [STALE]" if s.get("stale") else ""
                lines.append(
                    f"- FRED {sid}{stale_tag}: {s.get('value')} (as_of {s.get('as_of_date')}, "
                    f"前回 {s.get('prev_value')})"
                )
            else:
                lines.append(f"- FRED {sid}: 取得不可 ({s.get('error', 'unknown')})")
    elif liq and isinstance(liq, dict) and liq.get("error"):
        lines.append("")
        lines.append(f"### Deep: マクロ流動性 (S6-X) — 取得不可（{liq['error']}）")

    # --- 国債利回りスプレッド ---
    rs = data.get("rate_spreads")
    if rs and isinstance(rs, dict) and rs.get("spreads"):
        lines.append("")
        lines.append("### Deep: 国債利回りスプレッド (S6-X 中期方向)")
        for s in rs["spreads"]:
            stale_tag = " [STALE]" if s.get("stale") else ""
            spread = s.get("spread")
            change = s.get("change")
            change_str = f"{change:+.3f}" if change is not None else "N/A"
            spread_str = f"{spread:+.3f}" if spread is not None else "N/A"
            lines.append(
                f"- {s['pair']}{stale_tag}: スプレッド {spread_str} (前回比 {change_str}) — {s.get('interpretation')}"
            )
            if s.get("base_as_of") and s.get("sub_as_of"):
                lines.append(f"  base_as_of={s['base_as_of']}, sub_as_of={s['sub_as_of']}")
    elif rs and isinstance(rs, dict) and rs.get("error"):
        lines.append("")
        lines.append(f"### Deep: 国債利回りスプレッド (S6-X) — 取得不可（{rs['error']}）")

    # --- 暗号資産 Funding Rate (3 取引所平均) ---
    cf = data.get("crypto_funding")
    if cf and isinstance(cf, dict) and cf.get("exchanges"):
        lines.append("")
        lines.append("### Deep: 暗号資産 Funding Rate (S2-X BTCUSD)")
        lines.append(
            f"- 3 取引所平均 Funding Rate: {cf.get('average_funding_rate')}% "
            f"(乖離 {cf.get('max_dispersion')}%, regime: {cf.get('regime')})"
        )
        for e in cf["exchanges"]:
            mp = f", mark={e.get('mark_price')}" if e.get("mark_price") is not None else ""
            lines.append(f"- {e['exchange']}: {e['funding_rate']:.5f}%{mp}")
    elif cf and isinstance(cf, dict) and cf.get("error"):
        lines.append("")
        lines.append(f"### Deep: 暗号資産 Funding Rate — 取得不可（{cf['error']}）")

    # --- MyFXBook Open Orders ヒートマップ (S2-X) ---
    oo = data.get("myfxbook_open_orders")
    if oo and isinstance(oo, dict) and oo:
        lines.append("")
        lines.append("### Deep: MyFXBook Order Book (S2-X 実数 BSL/SSL クラスタ)")
        for sym, d in oo.items():
            if not isinstance(d, dict):
                lines.append(f"- {sym}: 取得不可")
                continue
            if d.get("error"):
                lines.append(f"- {sym}: 取得不可 ({d['error']})")
                continue
            cp = d.get("current_price")
            lines.append(
                f"- {sym}: 現在価格 {cp} | bids={d.get('bid_count', 0)} asks={d.get('ask_count', 0)}"
            )
            bsl_list = d.get("bsl_candidates") or []
            ssl_list = d.get("ssl_candidates") or []
            if d.get("note"):
                lines.append(f"  ※ {d['note']}")
            if bsl_list:
                lines.append("  - BSL 候補クラスタ (現在価格より上方、Ask 集中):")
                for c in bsl_list:
                    share = f", side内シェア {c['share_pct']}%" if c.get("share_pct") is not None else ""
                    lines.append(
                        f"    * {c.get('low')} – {c.get('high')} "
                        f"(volume_sum={c.get('volume_sum')}, entries={c.get('entries')}{share})"
                    )
            if ssl_list:
                lines.append("  - SSL 候補クラスタ (現在価格より下方、Bid 集中):")
                for c in ssl_list:
                    share = f", side内シェア {c['share_pct']}%" if c.get("share_pct") is not None else ""
                    lines.append(
                        f"    * {c.get('low')} – {c.get('high')} "
                        f"(volume_sum={c.get('volume_sum')}, entries={c.get('entries')}{share})"
                    )

    # --- リテール分析 (Retail P/L・Liquidity Pools・Sweep 検証) ---
    ra = data.get("retail_analytics")
    if ra and isinstance(ra, dict) and not (ra.get("error") and not ra.get("pl_structure")):
        lines.append("")
        lines.extend(format_retail_analytics_lines(ra))
    elif ra and isinstance(ra, dict) and ra.get("error"):
        lines.append("")
        lines.append(f"### リテール分析 — 取得不可（{ra['error']}）")

    # --- COT（常時取得）---
    cot = data.get("cot")
    if cot is not None:
        lines.append("")
        if cot.get("text"):
            lines.append(cot["text"])
        else:
            err = cot.get("error", "取得不可")
            lines.append(f"COTデータ取得不可（{err}）")

    # --- COT Disaggregated（機関ポジショニング内訳）---
    cot_dis = data.get("cot_disaggregated")
    if cot_dis:
        lines.append("")
        lines.extend(format_disaggregated_lines(cot_dis))

    # --- 相関レジーム定量 ---
    corr = data.get("correlation")
    if corr:
        lines.append("")
        lines.extend(format_correlation_lines(corr))

    # --- セッション統計 ---
    sess = data.get("session_stats")
    if sess:
        lines.append("")
        lines.extend(format_session_stats_lines(sess))

    # --- バリデーション結果サマリー ---
    if validation_results:
        lines.append("")
        lines.append("### データバリデーション警告")
        for symbol, issues in validation_results.items():
            for issue in issues:
                lines.append(f"- {symbol}: データ異常: {issue}")

    result_text = "\n".join(lines)

    # バリデーション結果をテキストに適用
    result_text = apply_validation(result_text, validation_results)

    return result_text


def save_scraped(scraped_data: dict, formatted_text: str, weekly: bool = False,
                 symbol: str = None) -> tuple[Path, Path]:
    """取得データを output/ に保存する。

    JSON (生データ) と TXT (formatted) の2種を出力する。
    LLM 分析・レポート生成は Claude Code スラッシュコマンド側が担当する。

    ファイル名契約:
        デフォルト銘柄 (XAUUSD): scraped_data_(weekly_)YYYY-MM-DD.*（従来どおり、
            intel.py / logos-engine が参照する外部契約のため変更しない）
        個別銘柄: scraped_data_{SYM}_YYYY-MM-DD.*（intel.py の日付 glob を
            汚染しないよう、日付直結パターンから外れる別名にする）
    """
    today = datetime.now().strftime("%Y-%m-%d")
    output_dir = Path(__file__).parent / "output"
    output_dir.mkdir(exist_ok=True)

    # Daily は既存の slash command / master_prompt 参照との互換性のため旧名を維持し、
    # weekly だけを分離する。この非対称は意図的。
    if symbol and symbol != DEFAULT_SYMBOL:
        prefix = f"scraped_data_{symbol}_"
    else:
        prefix = "scraped_data_weekly_" if weekly else "scraped_data_"

    json_path = output_dir / f"{prefix}{today}.json"
    clean_data = json.loads(json.dumps(scraped_data, default=str))
    for key in list(clean_data.keys()):
        if key.startswith("_raw_quote_") or key.startswith("_raw_series_"):
            clean_data.pop(key, None)
    for source in clean_data.values():
        if isinstance(source, dict):
            for symbol_data in source.values():
                if isinstance(symbol_data, dict):
                    symbol_data.pop("raw_text", None)
                    # FRED の観測列は相関計算のみに使う中間データ。
                    # 系列 40 本 × 6 系列で JSON が肥大するため保存しない。
                    symbol_data.pop("observations", None)
    json_path.write_text(json.dumps(clean_data, ensure_ascii=False, indent=2), encoding="utf-8")

    txt_path = output_dir / f"{prefix}{today}.txt"
    txt_path.write_text(formatted_text, encoding="utf-8")

    return json_path, txt_path


def parse_args(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        prog="main.py", description="ICT Bias Report スクレイピングオーケストレーター"
    )
    parser.add_argument(
        "--weekly", action="store_true",
        help="週次データ取得（ファイル名 prefix が scraped_data_weekly_ になる）",
    )
    parser.add_argument(
        "--symbol", default=DEFAULT_SYMBOL, choices=sorted(INSTRUMENTS.keys()),
        help=f"対象銘柄（既定: {DEFAULT_SYMBOL}。個別指定はデイリー専用）",
    )
    args = parser.parse_args(argv)
    if args.weekly and args.symbol != DEFAULT_SYMBOL:
        parser.error(f"--weekly は {DEFAULT_SYMBOL}/マクロ専用（--symbol と併用不可）")
    return args


async def main():
    args = parse_args()
    weekly = args.weekly
    symbol = args.symbol

    print("=" * 60)
    mode = "Weekly" if weekly else "Daily"
    print(f"ICT {mode} Bias Scraper ({symbol}) — {datetime.now().strftime('%Y/%m/%d %H:%M')}")
    print("=" * 60)

    scraped_data = await collect_all_data(weekly=weekly, symbol=symbol)
    formatted_data = format_scraped_data(scraped_data)

    print(f"\n[取得データサマリー]")
    print(formatted_data)

    json_path, txt_path = save_scraped(scraped_data, formatted_data, weekly=weekly, symbol=symbol)

    print(f"\n{'=' * 60}")
    print(f"完了!")
    print(f"  JSON: {json_path}")
    print(f"  TXT:  {txt_path}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    asyncio.run(main())
