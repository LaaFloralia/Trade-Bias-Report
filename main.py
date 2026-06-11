"""ICT Daily Bias Report — スクレイピングオーケストレーター

このスクリプトはデータ取得のみを担当する。
LLM 分析・レポート生成・Brain 保存は `.claude/commands/daily-bias.md`
スラッシュコマンド (Claude Code セッション内で実行) が責任を持つ。

実行方法:
    python main.py            # 日次データ取得 (output/scraped_data_*.{json,txt} を保存)
    python main.py --weekly   # 週次データ取得 (COT を含む)
"""

import asyncio
import sys
import json
from datetime import datetime, timedelta
from pathlib import Path

from config import INSTRUMENTS, FOMC_DATES_2026
from scrapers.myfxbook import scrape_myfxbook
from scrapers.fxssi import scrape_fxssi
from scrapers.ig_sentiment import scrape_ig_sentiment
from scrapers.coinglass import scrape_coinglass
from scrapers.cot import fetch_cot_data
from scrapers.twelvedata import fetch_price_data
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


def _get_fomc_metadata(today: datetime = None) -> dict:
    """FOMC週判定とメタデータを返す。

    Returns:
        {
            "is_fomc_week": bool,
            "next_fomc_date": str (YYYY-MM-DD),
            "days_until_fomc": int,
        }
    """
    if today is None:
        today = datetime.now()
    today_date = today.date()

    fomc_dates = sorted(datetime.strptime(d, "%Y-%m-%d").date() for d in FOMC_DATES_2026)

    # 次回FOMC日を特定
    next_fomc = None
    for fd in fomc_dates:
        if fd >= today_date:
            next_fomc = fd
            break

    if next_fomc is None:
        return {
            "is_fomc_week": False,
            "next_fomc_date": "未定（2026年日程終了）",
            "days_until_fomc": -1,
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
    }


async def collect_all_data(weekly: bool = False) -> dict:
    """MyFXBook優先でデータを取得し、失敗銘柄はFXSSI→IGの順でフォールバックする。
    weekly=True のときは COT データも取得する。
    新規データソース: DXY, FRED (DGS10/DGS2/DTWEXBGS), 経済指標カレンダー, FedWatch, BTC ETFフロー
    """
    print("[1/4] データ取得を開始...")

    results = {
        "timestamp": datetime.now().isoformat(),
        "price_data": None,  # Twelve Data API
        "retail_sentiment": {},  # 銘柄ごとに1ソースのみ格納
        "coinglass": {},
        "cot": None,  # ウィークリー時のみ使用
        "dxy": None,
        "fred": None,  # FRED: DGS10 (US10Y) / DGS2 (US2Y) / DTWEXBGS (Broad USD Index, ≠ DXY)
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
    }

    # --- Twelve Data: 価格データ取得 ---
    print("  Twelve Data: 価格データ取得中...")
    try:
        price_text = fetch_price_data()
        results["price_data"] = price_text
        print("  [OK]    Twelve Data: 価格データ取得完了")
    except Exception as e:
        results["price_data"] = f"Twelve Data 取得不可（{e}）"
        print(f"  [ERROR] Twelve Data: {e}")

    # --- Binance Futures: BTC Long/Short 取得（MyFXBook が BTC 非対応のため代替）---
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

    # --- Phase 1: MyFXBook + CoinGlass を並列取得 ---
    myfxbook_targets = [(sym, cfg["myfxbook_slug"]) for sym, cfg in INSTRUMENTS.items() if cfg.get("myfxbook_slug")]
    phase1_tasks = [("myfxbook", sym, scrape_myfxbook(slug)) for sym, slug in myfxbook_targets]
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

    # --- COT データ取得（ウィークリーのみ）---
    if weekly:
        print("  COT: CFTC APIからデータ取得中...")
        try:
            cot = fetch_cot_data()
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
    print(f"  FOMC判定: is_fomc_week={is_fomc_week}, next={fomc_meta['next_fomc_date']}, "
          f"days_until={fomc_meta['days_until_fomc']}")

    # Twelve Data /quote 競合回避のため 2 段階で取得する。
    # Phase A (Twelve Data 非依存): 全部並列で OK
    # Phase B (Twelve Data /quote 依存): 直列で 7 秒間隔
    #   理由: TD 無料枠 8 calls/min。fetch_price_data() で既に /quote+/time_series=2 calls 消費、
    #         dxy_components / premarket でさらに 2 calls 必要。並列だと burst で 429 を踏む。
    # FedWatch は is_fomc_week 分岐を撤廃し常時取得 (Deep Bias 強化要件)
    phase_a_tasks = [
        ("dxy", scrape_dxy()),
        ("fred", fetch_fred_data()),
        ("economic_calendar", scrape_economic_calendar()),
        ("btc_etf", scrape_btc_etf()),
        ("fedwatch", scrape_fedwatch()),
        # vix_structure は CBOE Dashboard を 4 ページ直列取得するため重い。Phase B へ移動。
        ("macro_liquidity", scrape_macro_liquidity()),
        ("rate_spreads", scrape_rate_spreads()),
        ("crypto_funding", scrape_crypto_funding()),
    ]
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

    # --- MyFXBook Open Orders: XAUUSD / USDJPY 並列取得 (Deep Bias 強化) ---
    open_order_targets = ["XAUUSD", "USDJPY"]
    print(f"  MyFXBook Open Orders: {open_order_targets} を並列取得中...")
    oo_tasks = [scrape_myfxbook_open_orders(s) for s in open_order_targets]
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
        lines.append(
            f"現在利回り: {series['value']:.3f}% | 前日比: {change_str} | "
            f"as_of: {series.get('as_of_date', 'N/A')}"
        )
        lines.append(f"ソース: FRED {series_id}")
        if series.get("note"):
            lines.append(f"※ {series['note']}")
        lines.append("")

    _emit_fred_yield("US10Y", "DGS10")
    _emit_fred_yield("US2Y", "DGS2")

    # --- Broad USD Index (FRED DTWEXBGS) — USD macro proxy。DXY とは別物として明示 ---
    dtwex = fred.get("DTWEXBGS") if isinstance(fred, dict) else None
    if isinstance(dtwex, dict) and dtwex.get("value") is not None:
        change = dtwex.get("change")
        change_str = f"{change:+.3f}" if change is not None else "N/A"
        stale_tag = " [STALE]" if dtwex.get("stale") else ""
        lines.append(f"[Broad USD Index]{stale_tag}")
        lines.append(
            f"現在値: {dtwex['value']:.3f} | 前日比: {change_str} | "
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
            lines.append(line)
            if fallback:
                lines.append(f"  ※ MyFXBook取得不可のため{source}にフォールバック")
        else:
            err = d.get("error", "取得不可")
            lines.append(f"- {symbol}: 取得不可（{err}）")
    lines.append("")

    # --- CoinGlass ---
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
                flow_parts = [f"{etf}: {v:+.1f}M" for etf, v in flows.items() if v is not None]
                total = day.get("total")
                total_str = f"合計: {total:+.1f}M" if total is not None else "合計: N/A"
                lines.append(f"- {day.get('date', 'N/A')}: {', '.join(flow_parts) + ', ' if flow_parts else ''}{total_str}")
        elif btc_etf.get("error"):
            lines.append(f"取得不可（{btc_etf['error']}）")

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
        if fedwatch.get("source"):
            lines.append(f"- ソース: {fedwatch['source']}")
    elif fedwatch and isinstance(fedwatch, dict) and fedwatch.get("error"):
        lines.append(f"- 取得不可（{fedwatch['error']}）")
    else:
        lines.append("- 取得不可（CME / Investing.com 両ソースから値抽出失敗）")

    # ============================================================
    # Deep Bias 強化セクション (master_prompt_deep.md S2-X / S5-X / S6-X 用)
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
            if bsl_list:
                lines.append("  - BSL 候補クラスタ (現在価格より上方、Ask 集中):")
                for c in bsl_list:
                    lines.append(
                        f"    * {c.get('low')} – {c.get('high')} "
                        f"(volume_sum={c.get('volume_sum')}, entries={c.get('entries')})"
                    )
            if ssl_list:
                lines.append("  - SSL 候補クラスタ (現在価格より下方、Bid 集中):")
                for c in ssl_list:
                    lines.append(
                        f"    * {c.get('low')} – {c.get('high')} "
                        f"(volume_sum={c.get('volume_sum')}, entries={c.get('entries')})"
                    )

    # --- COT（ウィークリー時のみ）---
    cot = data.get("cot")
    if cot is not None:
        lines.append("")
        if cot.get("text"):
            lines.append(cot["text"])
        else:
            err = cot.get("error", "取得不可")
            lines.append(f"COTデータ取得不可（{err}）")

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


def save_scraped(scraped_data: dict, formatted_text: str) -> tuple[Path, Path]:
    """取得データを output/ に保存する。

    JSON (生データ) と TXT (formatted) の2種を出力する。
    LLM 分析・レポート生成は Claude Code スラッシュコマンド側が担当する。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    output_dir = Path(__file__).parent / "output"
    output_dir.mkdir(exist_ok=True)

    json_path = output_dir / f"scraped_data_{today}.json"
    clean_data = json.loads(json.dumps(scraped_data, default=str))
    for source in clean_data.values():
        if isinstance(source, dict):
            for symbol_data in source.values():
                if isinstance(symbol_data, dict):
                    symbol_data.pop("raw_text", None)
    json_path.write_text(json.dumps(clean_data, ensure_ascii=False, indent=2), encoding="utf-8")

    txt_path = output_dir / f"scraped_data_{today}.txt"
    txt_path.write_text(formatted_text, encoding="utf-8")

    return json_path, txt_path


async def main():
    weekly = "--weekly" in sys.argv

    print("=" * 60)
    mode = "Weekly" if weekly else "Daily"
    print(f"ICT {mode} Bias Scraper — {datetime.now().strftime('%Y/%m/%d %H:%M')}")
    print("=" * 60)

    scraped_data = await collect_all_data(weekly=weekly)
    formatted_data = format_scraped_data(scraped_data)

    print(f"\n[取得データサマリー]")
    print(formatted_data)

    json_path, txt_path = save_scraped(scraped_data, formatted_data)

    print(f"\n{'=' * 60}")
    print(f"完了!")
    print(f"  JSON: {json_path}")
    print(f"  TXT:  {txt_path}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    asyncio.run(main())
