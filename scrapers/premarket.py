"""US 株指数のプリマーケット気配スクレイパー

対象: SPX (S&P 500), NQ (Nasdaq 100 futures), DJI (Dow)
取得: Twelve Data /quote バッチで現在値・前日比・OHLC を一括取得。

ICT 用途:
  - NY セッション開始前のリスクオン/オフを把握
  - BTCUSD と NQ の相関乖離を早期検出
  - VIX 構造と組み合わせてセンチメントを補強

Twelve Data の注意:
  - SPX は "SPX" シンボル
  - Nasdaq 100 は "NDX" or "NQ=F" — Twelve Data では "NDX" が確実
  - Dow は "DJI"
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

import requests
from config import BROWSER_TIMEOUT, TWELVEDATA_API_KEY, USER_AGENT

# Twelve Data 上の symbol
SYMBOLS = {
    "SPX": "SPX",
    "NDX": "NDX",
    "DJI": "DJI",
}

# Yahoo Finance のシンボルとフォールバック URL
YAHOO_URLS = {
    "SPX": "https://finance.yahoo.com/quote/%5EGSPC/",
    "NDX": "https://finance.yahoo.com/quote/%5ENDX/",
    "DJI": "https://finance.yahoo.com/quote/%5EDJI/",
}


def _fetch_quotes() -> Optional[dict]:
    if not TWELVEDATA_API_KEY:
        return None
    symbols = ",".join(SYMBOLS.values())
    import time
    for attempt in range(2):
        try:
            resp = requests.get(
                "https://api.twelvedata.com/quote",
                params={"symbol": symbols, "apikey": TWELVEDATA_API_KEY, "dp": "2"},
                timeout=15,
            )
            if resp.status_code == 429:
                time.sleep(8)
                continue
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and data.get("status") == "error":
                if data.get("code") == 429:
                    time.sleep(8)
                    continue
                return None
            return data
        except Exception:
            if attempt == 0:
                time.sleep(5)
                continue
            return None
    return None


async def _yahoo_fetch_quote(label: str, url: str) -> Optional[dict]:
    """Yahoo Finance ページから現在値と前日比 % を抽出する (Playwright)。"""
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=USER_AGENT, ignore_https_errors=True)
            page = await context.new_page()
            await page.goto(url, timeout=BROWSER_TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_timeout(4000)
            body = await page.inner_text("body")
            await browser.close()

        # 現在値: 連続する 4-5 桁の数値 (カンマ区切り) + 小数。
        # Yahoo の quote-header 領域に「XXXX.XX (+YY.YY +Z.ZZ%)」形式で現れる。
        price_m = re.search(r"([\d,]{3,8}\.\d{1,2})\s*[+\-]?[\d,]+\.\d{1,2}\s*\(", body)
        pct_m = re.search(r"\(([\+\-]?\d+\.\d+)%\)", body)
        if not price_m:
            return None
        try:
            current = float(price_m.group(1).replace(",", ""))
        except ValueError:
            return None
        change_pct = None
        if pct_m:
            try:
                change_pct = float(pct_m.group(1))
            except ValueError:
                pass
        return {
            "current": current,
            "change_pct": change_pct,
            "_source": "Yahoo Finance (fallback)",
        }
    except Exception:
        return None


async def scrape_premarket() -> dict:
    """US 株指数のプリマーケット (またはスポット) 値を取得する。

    主: Twelve Data /quote バッチ。失敗時は Yahoo Finance フォールバック (Playwright)。

    Returns:
        {
            "source": "Twelve Data (premarket)" | "Twelve Data + Yahoo フォールバック",
            "indices": {
                "SPX":  {"current": 7351.31, "change_pct": -0.83, "open": ..., "high": ..., "low": ...},
                "NDX":  {...},
                "DJI":  {...},
            },
            "risk_regime": "risk-on" | "risk-off" | "mixed" | "unknown",
            "error": str | None,
        }
    """
    result = {
        "source": "Twelve Data (premarket)",
        "indices": {},
        "risk_regime": None,
        "error": None,
    }

    used_fallback = False

    if TWELVEDATA_API_KEY:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, _fetch_quotes)
        if data is None:
            data = {}
    else:
        data = {}

    for label, td_symbol in SYMBOLS.items():
        q = data.get(td_symbol) if isinstance(data, dict) else None
        if not q or "close" not in q:
            result["indices"][label] = {"error": "no quote"}
            continue
        try:
            entry = {
                "current": float(q.get("close")),
                "prev_close": float(q.get("previous_close")) if q.get("previous_close") else None,
                "change": float(q.get("change")) if q.get("change") else None,
                "change_pct": float(q.get("percent_change")) if q.get("percent_change") else None,
                "open": float(q.get("open")) if q.get("open") else None,
                "high": float(q.get("high")) if q.get("high") else None,
                "low": float(q.get("low")) if q.get("low") else None,
            }
            result["indices"][label] = entry
        except (TypeError, ValueError) as exc:
            result["indices"][label] = {"error": f"parse error: {exc}"}

    # 欠損があれば Yahoo フォールバック
    missing = [k for k, v in result["indices"].items() if not isinstance(v, dict) or v.get("current") is None]
    for label in missing:
        url = YAHOO_URLS.get(label)
        if not url:
            continue
        yh = await _yahoo_fetch_quote(label, url)
        if yh:
            used_fallback = True
            result["indices"][label] = yh

    if used_fallback:
        result["source"] = "Twelve Data + Yahoo フォールバック"

    if not any(isinstance(v, dict) and v.get("current") is not None for v in result["indices"].values()):
        result["error"] = "Twelve Data + Yahoo 両方失敗"

    # リスクレジーム判定 — 3 銘柄の前日比方向
    pcts = [v.get("change_pct") for v in result["indices"].values() if isinstance(v, dict) and v.get("change_pct") is not None]
    if pcts:
        ups = sum(1 for p in pcts if p > 0.1)
        downs = sum(1 for p in pcts if p < -0.1)
        if ups == len(pcts):
            result["risk_regime"] = "risk-on"
        elif downs == len(pcts):
            result["risk_regime"] = "risk-off"
        else:
            result["risk_regime"] = "mixed"
    else:
        result["risk_regime"] = "unknown"

    return result


if __name__ == "__main__":
    data = asyncio.run(scrape_premarket())
    print("--- Premarket ---")
    for k, v in data.items():
        if k == "indices":
            print("  indices:")
            for sym, vals in v.items():
                print(f"    {sym}: {vals}")
        else:
            print(f"  {k}: {v}")
