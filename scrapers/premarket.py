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

# Yahoo Chart API 用シンボル（公式・無料・無認証、HTML より安定）
YAHOO_CHART_SYMBOLS = {
    "SPX": "^GSPC",
    "NDX": "^NDX",
    "DJI": "^DJI",
}


def _yahoo_chart_api(label: str, retries: int = 3) -> Optional[dict]:
    """Yahoo Finance Chart API から最新日の OHLC を取得（公式・無料・無認証）。

    HTTP 429 (Too Many Requests) を受けやすいため retry + 指数バックオフを付ける。
    range=5d で取り、末尾の有効日 ([-1]) の OHLC を返す。
    """
    import time

    yahoo_sym = YAHOO_CHART_SYMBOLS.get(label)
    if not yahoo_sym:
        return None

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_sym}"
    params = {"interval": "1d", "range": "5d"}
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://finance.yahoo.com/",
    }

    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=10)
            if resp.status_code == 429:
                time.sleep(2.0 * (attempt + 1))
                continue
            resp.raise_for_status()
            payload = resp.json()
            result = payload["chart"]["result"][0]
            meta = result["meta"]
            quote = result["indicators"]["quote"][0]

            opens = quote.get("open") or []
            highs = quote.get("high") or []
            lows = quote.get("low") or []
            idx = next(
                (i for i in range(len(opens) - 1, -1, -1)
                 if opens[i] is not None and highs[i] is not None and lows[i] is not None),
                None,
            )
            open_v = opens[idx] if idx is not None else None
            high_v = highs[idx] if idx is not None else None
            low_v = lows[idx] if idx is not None else None

            current = meta.get("regularMarketPrice")
            prev_close = meta.get("previousClose") or meta.get("chartPreviousClose")
            change = (current - prev_close) if (current is not None and prev_close is not None) else None
            change_pct = (
                round(change / prev_close * 100, 2)
                if (change is not None and prev_close) else None
            )

            return {
                "current": current,
                "prev_close": prev_close,
                "change": change,
                "change_pct": change_pct,
                "open": open_v,
                "high": high_v,
                "low": low_v,
                "_source": "Yahoo Chart API",
            }
        except Exception:
            if attempt < retries - 1:
                time.sleep(1.5)
                continue
            return None
    return None


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
    """Yahoo Finance ページから現在値・前日比・Open/Day's Range/Previous Close を抽出 (Playwright)。

    Chart API (429) が使えないときの最終フォールバック。
    Yahoo の Summary テーブルから OHL も抽出する。
    """
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

        # 現在値: 「XXXX.XX (+YY.YY +Z.ZZ%)」形式
        price_m = re.search(r"([\d,]{3,8}\.\d{1,2})\s*[+\-]?[\d,]+\.\d{1,2}\s*\(", body)
        pct_m = re.search(r"\(([\+\-]?\d+\.\d+)%\)", body)
        # Summary テーブル: 「Open\n7,408.51」「Day's Range\n7,390.00 - 7,420.00」「Previous Close\n7,500.00」
        open_m = re.search(r"Open\s*\n\s*([\d,]+\.\d{1,2})", body)
        range_m = re.search(
            r"Day'?s Range\s*\n\s*([\d,]+\.\d{1,2})\s*[-–]\s*([\d,]+\.\d{1,2})",
            body,
        )
        prev_m = re.search(r"Previous Close\s*\n\s*([\d,]+\.\d{1,2})", body)

        if not price_m:
            return None
        try:
            current = float(price_m.group(1).replace(",", ""))
        except ValueError:
            return None

        def _f(m, group=1):
            if not m:
                return None
            try:
                return float(m.group(group).replace(",", ""))
            except (ValueError, IndexError):
                return None

        change_pct = _f(pct_m)
        open_v = _f(open_m)
        low_v = _f(range_m, 1)
        high_v = _f(range_m, 2)
        prev_v = _f(prev_m)
        change = (current - prev_v) if (current is not None and prev_v is not None) else None

        return {
            "current": current,
            "prev_close": prev_v,
            "change": change,
            "change_pct": change_pct,
            "open": open_v,
            "high": high_v,
            "low": low_v,
            "_source": "Yahoo Finance HTML",
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

    # 欠損があれば Yahoo フォールバック（Chart API を優先、HTML は最終手段）
    loop = asyncio.get_event_loop()
    missing = [k for k, v in result["indices"].items() if not isinstance(v, dict) or v.get("current") is None]
    for label in missing:
        # 1. Yahoo Chart API (公式 JSON、OHLC 取れる)
        yh = await loop.run_in_executor(None, _yahoo_chart_api, label)
        if yh and yh.get("current") is not None:
            used_fallback = True
            result["indices"][label] = yh
            continue
        # 2. Yahoo HTML スクレイピング (current + change_pct のみ、OHL は None)
        url = YAHOO_URLS.get(label)
        if not url:
            continue
        yh = await _yahoo_fetch_quote(label, url)
        if yh:
            used_fallback = True
            result["indices"][label] = yh

    # OHL の欠損があれば Chart API で補完（Twelve Data 成功時でも OHL だけ欠落するケース対応）
    for label in list(result["indices"].keys()):
        entry = result["indices"][label]
        if not isinstance(entry, dict):
            continue
        if entry.get("current") is not None and any(entry.get(k) is None for k in ("open", "high", "low")):
            yh = await loop.run_in_executor(None, _yahoo_chart_api, label)
            if yh and yh.get("open") is not None:
                for k in ("open", "high", "low"):
                    if entry.get(k) is None:
                        entry[k] = yh.get(k)
                entry["_ohl_source"] = "Yahoo Chart API (補完)"

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
