"""DXY (US Dollar Index) 価格データスクレイパー

優先順:
1. Investing.com DXYページからPlaywrightでスクレイピング
2. MarketWatch をフォールバック
3. Twelve Data EUR/USD から簡易DXY推定

HTFレベル（PWH/PWL/PMH/PML/IPDA）は Investing.com ヒストリカルデータから算出する。

取得データ: 現在価格、前日終値、前日比、PDH/PDL、PWH/PWL、PMH/PML、IPDA 20/40
"""

from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.async_api import async_playwright
from config import (
    BROWSER_TIMEOUT,
    DXY_ESTIMATE_SYMBOL,
    EURUSD_DXY_FACTOR,
    TWELVEDATA_API_KEY,
    USER_AGENT,
)

# EUR/USD 逆数推定の係数・シンボルは config.yaml（SSoT）の dxy_estimate から供給される


def _estimate_dxy_from_eurusd(eurusd: float) -> float:
    return (1 / eurusd) * EURUSD_DXY_FACTOR


def _parse_float(text: str) -> Optional[float]:
    """数値文字列をfloatに変換する。カンマ・%等を除去。"""
    if not text:
        return None
    cleaned = text.replace(",", "").replace("%", "").replace("+", "").strip()
    if not cleaned or cleaned == "-":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _calculate_levels(daily_ohlc: List[Dict]) -> Dict[str, Optional[float]]:
    """日足OHLCリストからHTFレベルを算出する。

    daily_ohlc は新しい日付順（[0]=最新）で、各要素は
    {"date": datetime, "open": float, "high": float, "low": float, "close": float}
    """
    levels = {
        "pdh": None, "pdl": None,
        "pwh": None, "pwl": None,
        "pmh": None, "pml": None,
        "ipda_20_high": None, "ipda_20_low": None,
        "ipda_40_high": None, "ipda_40_low": None,
        "ipda_60_high": None, "ipda_60_low": None,
    }  # type: Dict[str, Optional[float]]

    if len(daily_ohlc) < 2:
        return levels

    today = daily_ohlc[0]["date"]
    past_days = daily_ohlc[1:]  # 当日を除外

    # PDH/PDL: 前営業日
    if past_days:
        levels["pdh"] = past_days[0]["high"]
        levels["pdl"] = past_days[0]["low"]

    # PWH/PWL: 前週（月〜金）のmax(high)/min(low)
    # 今週の月曜日を求め、その前の月〜金曜日を「前週」とする
    today_weekday = today.weekday()  # 0=月
    this_monday = today - timedelta(days=today_weekday)
    prev_friday = this_monday - timedelta(days=1)
    prev_monday = prev_friday - timedelta(days=4)

    prev_week_data = [
        d for d in past_days
        if prev_monday.date() <= d["date"].date() <= prev_friday.date()
    ]
    if prev_week_data:
        levels["pwh"] = max(d["high"] for d in prev_week_data)
        levels["pwl"] = min(d["low"] for d in prev_week_data)

    # PMH/PML: 前月1日〜末日のmax(high)/min(low)
    first_of_this_month = today.replace(day=1)
    last_of_prev_month = first_of_this_month - timedelta(days=1)
    first_of_prev_month = last_of_prev_month.replace(day=1)

    prev_month_data = [
        d for d in past_days
        if first_of_prev_month.date() <= d["date"].date() <= last_of_prev_month.date()
    ]
    if prev_month_data:
        levels["pmh"] = max(d["high"] for d in prev_month_data)
        levels["pml"] = min(d["low"] for d in prev_month_data)

    # IPDA 20日: 直近20営業日のmax(high)/min(low)
    ipda_20 = past_days[:20]
    if ipda_20:
        levels["ipda_20_high"] = max(d["high"] for d in ipda_20)
        levels["ipda_20_low"] = min(d["low"] for d in ipda_20)

    # IPDA 40日: 直近40営業日のmax(high)/min(low)
    ipda_40 = past_days[:40]
    if ipda_40:
        levels["ipda_40_high"] = max(d["high"] for d in ipda_40)
        levels["ipda_40_low"] = min(d["low"] for d in ipda_40)

    # IPDA 60日: 直近60営業日のmax(high)/min(low)
    ipda_60 = past_days[:60]
    if ipda_60:
        levels["ipda_60_high"] = max(d["high"] for d in ipda_60)
        levels["ipda_60_low"] = min(d["low"] for d in ipda_60)

    return levels


async def _scrape_historical_investing() -> List[Dict]:
    """Investing.com DXY ヒストリカルAPI経由で日足OHLCを取得する。

    ページにアクセスしてセッションを確立後、内部APIで90日分のデータを取得。
    API は domain-id: www ヘッダーが必要。
    """
    ohlc = []  # type: List[Dict]
    page_url = "https://www.investing.com/indices/usdollar-historical-data"
    instrument_id = "942611"

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=USER_AGENT, ignore_https_errors=True)
            page = await context.new_page()

            await page.goto(page_url, timeout=BROWSER_TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            end = datetime.now()
            start = end - timedelta(days=90)
            api_url = (
                f"https://api.investing.com/api/financialdata/historical/{instrument_id}"
                f"?start-date={start.strftime('%Y-%m-%d')}"
                f"&end-date={end.strftime('%Y-%m-%d')}"
                f"&time-frame=Daily&add-missing-rows=false"
            )

            result = await page.evaluate("""async (apiUrl) => {
                const resp = await fetch(apiUrl, {
                    headers: { 'domain-id': 'www' }
                });
                if (!resp.ok) return { error: resp.status };
                return await resp.json();
            }""", api_url)

            if isinstance(result, dict) and result.get("error"):
                print(f"  [WARN] Investing.com DXY API: HTTP {result['error']}")
                await browser.close()
                return await _scrape_historical_investing_html(page_url)

            rows = result.get("data", []) if isinstance(result, dict) else []
            for row in rows:
                try:
                    dt = datetime.strptime(row["rowDateTimestamp"][:10], "%Y-%m-%d")
                    high = _parse_float(row.get("last_max", ""))
                    low = _parse_float(row.get("last_min", ""))
                    if high is not None and low is not None:
                        ohlc.append({
                            "date": dt,
                            "open": _parse_float(row.get("last_open", "")) or 0,
                            "high": high,
                            "low": low,
                            "close": _parse_float(row.get("last_close", "")) or 0,
                        })
                except (KeyError, ValueError):
                    continue

            await browser.close()

    except Exception as e:
        print(f"  [WARN] Investing.com DXY Historical: {e}")

    return ohlc


async def _scrape_historical_investing_html(page_url: str) -> List[Dict]:
    """Investing.com ヒストリカルページからHTMLテーブルをパースする（APIフォールバック）。"""
    ohlc = []  # type: List[Dict]

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=USER_AGENT, ignore_https_errors=True)
            page = await context.new_page()

            await page.goto(page_url, timeout=BROWSER_TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)

            rows = await page.query_selector_all("table tr")
            for row in rows:
                text = await row.inner_text()
                text = text.strip()
                if not text:
                    continue

                date_match = re.match(r'(\w{3}\s+\d{1,2},\s+\d{4})\t', text)
                if not date_match:
                    continue

                parts = text.split("\t")
                if len(parts) < 5:
                    continue

                try:
                    dt = datetime.strptime(date_match.group(1), "%b %d, %Y")
                except ValueError:
                    continue

                high_val = _parse_float(parts[3])
                low_val = _parse_float(parts[4])
                if high_val is not None and low_val is not None:
                    ohlc.append({
                        "date": dt,
                        "open": _parse_float(parts[2]) or 0,
                        "high": high_val,
                        "low": low_val,
                        "close": _parse_float(parts[1]) or 0,
                    })

            await browser.close()

    except Exception as e:
        print(f"  [WARN] Investing.com DXY Historical HTML: {e}")

    return ohlc


async def _scrape_historical_stooq() -> List[Dict]:
    """Stooq から DXY 日足OHLCを取得する（フォールバック）。"""
    url = "https://stooq.com/q/d/?s=dx.f"
    ohlc = []  # type: List[Dict]

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=USER_AGENT, ignore_https_errors=True)
            page = await context.new_page()

            await page.goto(url, timeout=BROWSER_TIMEOUT * 2, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)

            rows = await page.query_selector_all("table#d1 tr")
            for row in rows:
                text = await row.inner_text()
                text = text.strip()
                # "2026-04-04\t100.03\t99.99\t100.22\t99.95\t..."
                date_match = re.match(r'(\d{4}-\d{2}-\d{2})\t', text)
                if not date_match:
                    continue

                parts = text.split("\t")
                if len(parts) < 5:
                    continue

                try:
                    dt = datetime.strptime(date_match.group(1), "%Y-%m-%d")
                except ValueError:
                    continue

                close_val = _parse_float(parts[1])
                open_val = _parse_float(parts[2])
                high_val = _parse_float(parts[3])
                low_val = _parse_float(parts[4])

                if high_val is not None and low_val is not None:
                    ohlc.append({
                        "date": dt,
                        "open": open_val or 0,
                        "high": high_val,
                        "low": low_val,
                        "close": close_val or 0,
                    })

            await browser.close()

    except Exception as e:
        print(f"  [WARN] Stooq DXY Historical: {e}")

    return ohlc


async def _fetch_historical_ohlc() -> List[Dict]:
    """日足OHLCを取得する。Investing.com優先、Stooqフォールバック。"""
    print("  DXY Historical: Investing.com を試行中...")
    ohlc = await _scrape_historical_investing()
    if len(ohlc) >= 20:
        print(f"  [OK]    DXY Historical: Investing.com から {len(ohlc)} 日分取得")
        return ohlc

    print("  DXY Historical: Stooq フォールバック...")
    ohlc = await _scrape_historical_stooq()
    if ohlc:
        print(f"  [OK]    DXY Historical: Stooq から {len(ohlc)} 日分取得")
        return ohlc

    print("  [WARN]  DXY Historical: 取得不可")
    return []


async def _scrape_investing() -> Optional[dict]:
    """Investing.com からDXYデータを取得する。"""
    url = "https://www.investing.com/indices/usdollar"
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=USER_AGENT, ignore_https_errors=True)
            page = await context.new_page()

            await page.goto(url, timeout=BROWSER_TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)

            body_text = await page.inner_text("body")

            result = {
                "source": "Investing.com",
                "current_price": None,
                "prev_close": None,
                "change": None,
                "change_pct": None,
                "pdh": None,
                "pdl": None,
                "pwh": None,
                "pwl": None,
                "pmh": None,
                "pml": None,
                "ipda_20_high": None,
                "ipda_20_low": None,
                "ipda_40_high": None,
                "ipda_40_low": None,
                "ipda_60_high": None,
                "ipda_60_low": None,
                "estimated": False,
            }  # type: dict

            price_el = await page.query_selector('[data-test="instrument-price-last"]')
            if price_el:
                price_text = await price_el.inner_text()
                result["current_price"] = float(price_text.replace(",", ""))

            prev_match = re.search(r'(?:Prev\.\s*Close|前日終値)[:\s]*([\d,.]+)', body_text, re.IGNORECASE)
            if prev_match:
                result["prev_close"] = float(prev_match.group(1).replace(",", ""))

            change_el = await page.query_selector('[data-test="instrument-price-change"]')
            if change_el:
                change_text = await change_el.inner_text()
                result["change"] = float(change_text.replace(",", "").replace("+", ""))

            change_pct_el = await page.query_selector('[data-test="instrument-price-change-percent"]')
            if change_pct_el:
                pct_text = await change_pct_el.inner_text()
                pct_clean = re.sub(r'[()%\s]', '', pct_text)
                if pct_clean:
                    result["change_pct"] = float(pct_clean)

            day_range = re.search(r"(?:Day's Range|日中安値/高値)[:\s]*([\d,.]+)\s*[-–]\s*([\d,.]+)", body_text, re.IGNORECASE)
            if day_range:
                result["pdl"] = float(day_range.group(1).replace(",", ""))
                result["pdh"] = float(day_range.group(2).replace(",", ""))

            await browser.close()

            if result["current_price"] is not None:
                if result["prev_close"] is None and result["change"] is not None:
                    result["prev_close"] = result["current_price"] - result["change"]
                if result["change"] is None and result["prev_close"] is not None:
                    result["change"] = result["current_price"] - result["prev_close"]
                if result["change_pct"] is None and result["prev_close"] and result["prev_close"] != 0:
                    result["change_pct"] = (result["change"] / result["prev_close"]) * 100

                return result

    except Exception as e:
        print(f"  [WARN] Investing.com DXY: {e}")

    return None


async def _scrape_marketwatch() -> Optional[dict]:
    """MarketWatch からDXYデータを取得する。"""
    url = "https://www.marketwatch.com/investing/index/dxy"
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=USER_AGENT, ignore_https_errors=True)
            page = await context.new_page()

            await page.goto(url, timeout=BROWSER_TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)

            body_text = await page.inner_text("body")

            result = {
                "source": "MarketWatch",
                "current_price": None,
                "prev_close": None,
                "change": None,
                "change_pct": None,
                "pdh": None,
                "pdl": None,
                "pwh": None,
                "pwl": None,
                "pmh": None,
                "pml": None,
                "ipda_20_high": None,
                "ipda_20_low": None,
                "ipda_40_high": None,
                "ipda_40_low": None,
                "ipda_60_high": None,
                "ipda_60_low": None,
                "estimated": False,
            }  # type: dict

            price_el = await page.query_selector('.intraday__price .value')
            if price_el:
                price_text = await price_el.inner_text()
                result["current_price"] = float(price_text.replace(",", ""))

            prev_match = re.search(r'(?:Previous Close|Close)[:\s]*([\d,.]+)', body_text, re.IGNORECASE)
            if prev_match:
                result["prev_close"] = float(prev_match.group(1).replace(",", ""))

            change_el = await page.query_selector('.intraday__change .change--point--q')
            if change_el:
                change_text = await change_el.inner_text()
                result["change"] = float(change_text.replace(",", "").replace("+", ""))

            pct_el = await page.query_selector('.intraday__change .change--percent--q')
            if pct_el:
                pct_text = await pct_el.inner_text()
                pct_clean = re.sub(r'[%\s]', '', pct_text)
                if pct_clean:
                    result["change_pct"] = float(pct_clean)

            await browser.close()

            if result["current_price"] is not None:
                if result["prev_close"] is None and result["change"] is not None:
                    result["prev_close"] = result["current_price"] - result["change"]
                if result["change"] is None and result["prev_close"] is not None:
                    result["change"] = result["current_price"] - result["prev_close"]
                if result["change_pct"] is None and result["prev_close"] and result["prev_close"] != 0:
                    result["change_pct"] = (result["change"] / result["prev_close"]) * 100
                return result

    except Exception as e:
        print(f"  [WARN] MarketWatch DXY: {e}")

    return None


def _estimate_from_eurusd() -> Optional[dict]:
    """Twelve Data EUR/USD から簡易DXYを推定する。"""
    import requests

    if not TWELVEDATA_API_KEY:
        return None

    base = "https://api.twelvedata.com"
    try:
        resp = requests.get(
            f"{base}/quote",
            params={"symbol": DXY_ESTIMATE_SYMBOL, "apikey": TWELVEDATA_API_KEY, "dp": "5"},
            timeout=15,
        )
        resp.raise_for_status()
        q = resp.json()
        if q.get("status") == "error":
            return None

        eurusd_close = float(q["close"])
        eurusd_prev = float(q["previous_close"])

        dxy_current = _estimate_dxy_from_eurusd(eurusd_close)
        dxy_prev = _estimate_dxy_from_eurusd(eurusd_prev)

        result = {
            "source": "Twelve Data EUR/USD推定",
            "current_price": round(dxy_current, 3),
            "prev_close": round(dxy_prev, 3),
            "change": round(dxy_current - dxy_prev, 3),
            "change_pct": round(((dxy_current - dxy_prev) / dxy_prev) * 100, 3) if dxy_prev else None,
            "pdh": None,
            "pdl": None,
            "pwh": None,
            "pwl": None,
            "pmh": None,
            "pml": None,
            "ipda_20_high": None,
            "ipda_20_low": None,
            "ipda_40_high": None,
            "ipda_40_low": None,
            "ipda_60_high": None,
            "ipda_60_low": None,
            "estimated": True,
            "note": "EUR/USD逆数から推定",
        }  # type: dict

        ts_resp = requests.get(
            f"{base}/time_series",
            params={
                "symbol": DXY_ESTIMATE_SYMBOL,
                "interval": "1day",
                "outputsize": "25",
                "apikey": TWELVEDATA_API_KEY,
                "dp": "5",
            },
            timeout=15,
        )
        ts_resp.raise_for_status()
        ts = ts_resp.json()
        if ts.get("status") == "error":
            return result

        values = ts.get("values", [])
        if len(values) >= 2:
            prev_day = values[1]
            result["pdh"] = round(_estimate_dxy_from_eurusd(float(prev_day["low"])), 3)
            result["pdl"] = round(_estimate_dxy_from_eurusd(float(prev_day["high"])), 3)

        if len(values) >= 6:
            week_data = values[1:6]
            result["pwh"] = round(max(_estimate_dxy_from_eurusd(float(v["low"])) for v in week_data), 3)
            result["pwl"] = round(min(_estimate_dxy_from_eurusd(float(v["high"])) for v in week_data), 3)

        if len(values) >= 23:
            month_data = values[1:23]
            result["pmh"] = round(max(_estimate_dxy_from_eurusd(float(v["low"])) for v in month_data), 3)
            result["pml"] = round(min(_estimate_dxy_from_eurusd(float(v["high"])) for v in month_data), 3)

        return result

    except Exception as e:
        print(f"  [WARN] EUR/USD DXY推定: {e}")
        return None


async def scrape_dxy() -> dict:
    """DXYデータを取得する。優先順にソースを試行する。

    Returns:
        {
            "source": str,
            "current_price": float | None,
            "prev_close": float | None,
            "change": float | None,
            "change_pct": float | None,
            "pdh": float | None, "pdl": float | None,
            "pwh": float | None, "pwl": float | None,
            "pmh": float | None, "pml": float | None,
            "ipda_20_high": float | None, "ipda_20_low": float | None,
            "ipda_40_high": float | None, "ipda_40_low": float | None,
            "estimated": bool,
            "note": str | None,
            "error": str | None,
        }
    """
    # ヒストリカルデータを先に取得（レベル算出に使用）
    ohlc = await _fetch_historical_ohlc()
    htf_levels = _calculate_levels(ohlc) if ohlc else {}

    # 1. Investing.com
    print("  DXY: Investing.com を試行中...")
    data = await _scrape_investing()
    if data and data.get("current_price") is not None:
        print(f"  [OK]    DXY: Investing.com から取得 ({data['current_price']})")
        data["error"] = None
        data["note"] = None

        # ヒストリカルから算出したHTFレベルで補完
        if htf_levels:
            notes = []
            for key in ["pdh", "pdl", "pwh", "pwl", "pmh", "pml",
                        "ipda_20_high", "ipda_20_low", "ipda_40_high", "ipda_40_low",
                        "ipda_60_high", "ipda_60_low"]:
                if data.get(key) is None and htf_levels.get(key) is not None:
                    data[key] = htf_levels[key]
                    if key.startswith("ipda"):
                        continue
                    notes.append(key.upper())
            if notes:
                data["note"] = f"{'/'.join(notes)}はヒストリカルデータから算出"
            # IPDA は常にヒストリカルから設定
            for ipda_key in ["ipda_20_high", "ipda_20_low", "ipda_40_high", "ipda_40_low",
                              "ipda_60_high", "ipda_60_low"]:
                if htf_levels.get(ipda_key) is not None:
                    data[ipda_key] = htf_levels[ipda_key]

        return data

    # 2. MarketWatch
    print("  DXY: MarketWatch を試行中...")
    data = await _scrape_marketwatch()
    if data and data.get("current_price") is not None:
        print(f"  [OK]    DXY: MarketWatch から取得 ({data['current_price']})")
        data["error"] = None
        data["note"] = None
        if htf_levels:
            notes = []
            for key in ["pdh", "pdl", "pwh", "pwl", "pmh", "pml",
                        "ipda_20_high", "ipda_20_low", "ipda_40_high", "ipda_40_low",
                        "ipda_60_high", "ipda_60_low"]:
                if data.get(key) is None and htf_levels.get(key) is not None:
                    data[key] = htf_levels[key]
                    if key.startswith("ipda"):
                        continue
                    notes.append(key.upper())
            if notes:
                data["note"] = f"{'/'.join(notes)}はヒストリカルデータから算出"
            for ipda_key in ["ipda_20_high", "ipda_20_low", "ipda_40_high", "ipda_40_low",
                              "ipda_60_high", "ipda_60_low"]:
                if htf_levels.get(ipda_key) is not None:
                    data[ipda_key] = htf_levels[ipda_key]
        return data

    # 3. EUR/USD から推定
    print("  DXY: EUR/USD逆数から推定中...")
    data = _estimate_from_eurusd()
    if data and data.get("current_price") is not None:
        print(f"  [OK]    DXY: EUR/USD推定 ({data['current_price']})")
        data["error"] = None
        return data

    return {
        "source": "取得不可",
        "current_price": None,
        "prev_close": None,
        "change": None,
        "change_pct": None,
        "pdh": None, "pdl": None,
        "pwh": None, "pwl": None,
        "pmh": None, "pml": None,
        "ipda_20_high": None, "ipda_20_low": None,
        "ipda_40_high": None, "ipda_40_low": None,
        "ipda_60_high": None, "ipda_60_low": None,
        "estimated": False,
        "note": None,
        "error": "全ソースからの取得に失敗",
    }


if __name__ == "__main__":
    data = asyncio.run(scrape_dxy())
    print("\n--- DXY ---")
    for k, v in data.items():
        if isinstance(v, float):
            print(f"  {k}: {v:,.3f}")
        else:
            print(f"  {k}: {v}")
