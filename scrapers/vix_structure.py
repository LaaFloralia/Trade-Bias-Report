"""VIX 構造 (Term Structure) スクレイパー（マルチソース）

取得対象:
  VIX9D ... CBOE 9 Day Volatility Index    (短期スポット)
  VIX   ... CBOE Volatility Index 30D       (基準スポット)
  VIX3M ... CBOE 3-Month Volatility Index   (中期スポット — VX1/VX2 先物の代替として使用)
  VIX6M ... CBOE 6-Month Volatility Index   (長期スポット)

ソース優先順:
  1. FRED (VIXCLS, VXVCLS) — VIX と VIX3M をデイリー安定取得 (前日比含む)
  2. CBOE 公式 Dashboard (Playwright) — VIX9D / VIX6M (FRED 未提供) を取得
  3. Twelve Data — 妥当性 5-100 範囲外は不採用 (シンボル衝突リスク)
  4. Yahoo Finance — fin-streamer から data-symbol マッチで取得 (フォールバック)

判定ロジック:
  - VIX > VIX3M → バックワーデーション (危機 / リスクオフ局面)
  - VIX < VIX3M → コンタンゴ (通常 / リスクオン)
  - VIX9D > VIX → 短期不安先行 (イベント直前)
  - VIX 値: <15 安、15-20 通常、20-30 警戒、>30 パニック

ICT 用途: ボラ環境のレジーム判定 → 信頼度スコアと PO3 期待値の調整に使う。
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

import requests
from playwright.async_api import async_playwright
from config import (
    BROWSER_TIMEOUT,
    USER_AGENT,
    TWELVEDATA_API_KEY,
    VIX_CBOE_URLS,
    VIX_FRED_SYMBOLS,
    VIX_TD_SYMBOLS,
    VIX_YAHOO_URLS,
)

# 系列定義は config.yaml（SSoT）の vix_series から供給される

# Twelve Data 上のシンボル名 (株式・指数扱い、頭の ^ は省略)
TD_SYMBOLS = VIX_TD_SYMBOLS

# FRED 系列 (VIX9D / VIX6M は FRED 未提供)
FRED_SYMBOLS = VIX_FRED_SYMBOLS

# CBOE 公式 Dashboard URL (VIX9D / VIX6M を補完)
CBOE_URLS = VIX_CBOE_URLS

# Yahoo Finance URL (最終フォールバック)
YAHOO_URLS = VIX_YAHOO_URLS


def _fetch_twelvedata_batch() -> Optional[dict]:
    if not TWELVEDATA_API_KEY:
        return None
    symbols = ",".join(TD_SYMBOLS.values())
    try:
        resp = requests.get(
            "https://api.twelvedata.com/quote",
            params={"symbol": symbols, "apikey": TWELVEDATA_API_KEY, "dp": "3"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("status") == "error":
            return None
        return data
    except Exception:
        return None


async def _fetch_fred_vix(label: str, series_id: str) -> Optional[float]:
    """FRED から VIX 系列を取得する。get_fred_api_key + fetch_fred_series を流用。"""
    try:
        from scrapers.fred import fetch_fred_series, get_fred_api_key
        api_key = get_fred_api_key()
        if not api_key:
            return None
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, fetch_fred_series, series_id, api_key)
        v = result.get("value") if isinstance(result, dict) else None
        if v is None:
            return None
        v = float(v)
        if 5.0 <= v <= 100.0:
            return v
    except Exception:
        return None
    return None


async def _fetch_cboe_dashboard(label: str, url: str) -> Optional[float]:
    """CBOE 公式 Dashboard ページから現在値を抽出する。

    ページ構造: ヘッダー直下に <symbol>\n<full name>\nLast Sale\n<value>... の順で並ぶ。
    body 全体から最初の妥当な VIX 値 (5-100) を採用する。
    """
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=USER_AGENT, ignore_https_errors=True)
            page = await context.new_page()
            await page.goto(url, timeout=BROWSER_TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_timeout(4000)
            text = await page.inner_text("body")
            await browser.close()
        # CBOE のページは "Last Sale" 文字列の直後に値があるが、レイアウトによっては
        # シンボル名と値だけが連続している場合もある。安全側で全数値の妥当域を採用。
        nums = re.findall(r"\b(\d{1,3}\.\d{1,3})\b", text)
        for n in nums:
            try:
                v = float(n)
                if 5.0 <= v <= 100.0:
                    return v
            except ValueError:
                continue
    except Exception:
        return None
    return None


async def _fetch_yahoo(symbol: str, url: str) -> Optional[float]:
    """Yahoo Finance fin-streamer から VIX 値を取得する。"""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=USER_AGENT, ignore_https_errors=True)
            page = await context.new_page()
            await page.goto(url, timeout=BROWSER_TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_timeout(4000)
            # data-symbol="^<SYMBOL>" の fin-streamer から regularMarketPrice を取得
            yh_symbol = "^" + symbol
            els = await page.query_selector_all(
                f'fin-streamer[data-symbol="{yh_symbol}"][data-field="regularMarketPrice"]'
            )
            for el in els:
                try:
                    v_attr = await el.get_attribute("value")
                    if v_attr:
                        v = float(v_attr)
                        if 5.0 <= v <= 100.0:
                            await browser.close()
                            return v
                except Exception:
                    continue
            await browser.close()
    except Exception:
        return None
    return None


def _classify_term_structure(vals: dict) -> dict:
    """ターム構造とレジーム判定を返す。"""
    vix = vals.get("VIX")
    vix3m = vals.get("VIX3M")
    vix9d = vals.get("VIX9D")

    structure = "unknown"
    if vix is not None and vix3m is not None:
        if vix > vix3m * 1.02:
            structure = "backwardation"
        elif vix < vix3m * 0.98:
            structure = "contango"
        else:
            structure = "flat"

    short_alert = None
    if vix is not None and vix9d is not None:
        short_alert = vix9d > vix * 1.05  # 5% 以上の短期スパイク

    level = None
    if vix is not None:
        if vix < 15:
            level = "low (calm)"
        elif vix < 20:
            level = "normal"
        elif vix < 30:
            level = "elevated"
        else:
            level = "panic"

    return {
        "term_structure": structure,
        "vix_level_regime": level,
        "short_term_event_alert": short_alert,
    }


async def scrape_vix_structure() -> dict:
    """VIX ターム構造を取得する。

    Returns:
        {
            "source": str,
            "values": {"VIX9D": 18.2, "VIX": 19.0, "VIX3M": 20.1, "VIX6M": 21.0},
            "term_structure": "contango" | "backwardation" | "flat" | "unknown",
            "vix_level_regime": "low (calm)" | "normal" | "elevated" | "panic",
            "short_term_event_alert": bool | None,
            "error": str | None,
        }
    """
    result = {
        "source": None,
        "values": {},
        "term_structure": None,
        "vix_level_regime": None,
        "short_term_event_alert": None,
        "error": None,
    }

    sources_used: list[str] = []

    # 1. FRED から VIX / VIX3M を取得 (最も安定)
    fred_pairs = [
        (label, sid) for label, sid in FRED_SYMBOLS.items() if label in TD_SYMBOLS
    ]
    fred_results = await asyncio.gather(
        *[_fetch_fred_vix(lbl, sid) for lbl, sid in fred_pairs],
        return_exceptions=True,
    )
    for (lbl, _), val in zip(fred_pairs, fred_results):
        if isinstance(val, Exception):
            continue
        if val is not None:
            result["values"][lbl] = val
    if any(lbl in result["values"] for lbl, _ in fred_pairs):
        sources_used.append("FRED")

    # 2. 残り (VIX9D / VIX6M、または FRED 失敗時の VIX/VIX3M) を CBOE Dashboard から取得
    # ※ 複数 Playwright インスタンスを同一プロセスで並列起動すると競合するため、ここは直列実行
    missing = [k for k in TD_SYMBOLS if k not in result["values"]]
    if missing:
        cboe_picked_up = False
        for lbl in missing:
            try:
                val = await _fetch_cboe_dashboard(lbl, CBOE_URLS[lbl])
            except Exception:
                val = None
            if val is not None:
                result["values"][lbl] = val
                cboe_picked_up = True
        if cboe_picked_up:
            sources_used.append("CBOE Dashboard")

    # 3. Twelve Data バッチ取得 (まだ欠損あれば)。妥当域 5-100 でフィルタ。
    missing = [k for k in TD_SYMBOLS if k not in result["values"]]
    if missing:
        loop = asyncio.get_event_loop()
        td_data = await loop.run_in_executor(None, _fetch_twelvedata_batch)
        if td_data:
            for lbl in list(missing):
                td_sym = TD_SYMBOLS[lbl]
                q = td_data.get(td_sym) if isinstance(td_data, dict) else None
                if q and q.get("close"):
                    try:
                        val = float(q["close"])
                    except (TypeError, ValueError):
                        continue
                    if 5.0 <= val <= 100.0:
                        result["values"][lbl] = val
            if "Twelve Data" not in sources_used and any(lbl in result["values"] for lbl in missing):
                sources_used.append("Twelve Data")

    # 4. Yahoo Finance フォールバック (最終)
    missing = [k for k in TD_SYMBOLS if k not in result["values"]]
    if missing:
        for lbl in missing:
            url = YAHOO_URLS.get(lbl)
            if not url:
                continue
            val = await _fetch_yahoo(lbl, url)
            if val is not None:
                result["values"][lbl] = val
        if "Yahoo" not in sources_used and any(lbl in result["values"] for lbl in missing):
            sources_used.append("Yahoo Finance")

    if not result["values"]:
        result["error"] = "VIX 取得不可 (FRED + CBOE + Twelve Data + Yahoo すべて失敗)"
        result["source"] = "取得不可"
        return result

    result["source"] = " + ".join(sources_used) if sources_used else "部分的取得"

    # ターム構造判定
    judgment = _classify_term_structure(result["values"])
    result.update(judgment)

    return result


if __name__ == "__main__":
    data = asyncio.run(scrape_vix_structure())
    print("--- VIX Structure ---")
    for k, v in data.items():
        print(f"  {k}: {v}")
