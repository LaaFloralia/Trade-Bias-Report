"""BTC ETF フロー スクレイパー

優先順:
1. Farside Investors (https://farside.co.uk/bitcoin-etf-flow-all-data/)
2. SoSoValue (https://sosovalue.com/assets/etf/us-btc-spot)
3. CoinGlass (https://www.coinglass.com/bitcoin-etf)

取得データ: ETF別（IBIT, FBTC, GBTC, その他）と日次合計、直近5営業日分
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
from config import BROWSER_TIMEOUT, BTC_ETF_TICKERS, USER_AGENT

# 追跡ティッカーは config.yaml（SSoT）の btc_etf_tickers から供給される
TARGET_ETFS = BTC_ETF_TICKERS


def _scrape_farside() -> Optional[dict]:
    """Farside Investors からBTC ETFフローを取得する（旧 requests 版、403 で殆ど失敗）。

    現状は Cloudflare/anti-bot で 403 を返すため、`_scrape_farside_playwright` を優先使用する。
    本関数は後方互換のため残置（async に書き換えると主要呼出が壊れるため、シグネチャを維持）。
    """
    url = "https://farside.co.uk/btc/"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://farside.co.uk/",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"  [WARN] Farside (requests): {e}")
        return None
    return _parse_farside_html(resp.text)


def _parse_farside_html(html: str) -> Optional[dict]:
    """Farside HTML をパースして daily_flows リストを返す。

    farside.co.uk/btc/ のテーブル構造（class="etf"）:
      row 0: header (Total only at end)
      row 1: ETF symbols (IBIT, FBTC, BITB, ARKB, BTCO, EZBC, BRRR, HODL, BTCW, MSBT, GBTC, BTC)
      row 2: Fee row
      row 3: empty separator
      row 4..N-4: daily flow rows (Date | flows | Total)
      row N-3: Total cumulative
      row N-2: Average / Maximum / Minimum
    """
    # 1) class="etf" テーブルを切り出す
    tbl_m = re.search(r'<table[^>]*class="etf"[^>]*>(.*?)</table>', html, re.DOTALL | re.IGNORECASE)
    if not tbl_m:
        return None
    table_html = tbl_m.group(1)

    # 2) 各 <tr> 行を分割
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_html, re.DOTALL | re.IGNORECASE)
    if len(rows) < 5:
        return None

    import html as _html

    def cells(row_html: str) -> list[str]:
        cs = re.findall(r'<t[hd][^>]*>(.*?)</t[hd]>', row_html, re.DOTALL | re.IGNORECASE)
        # HTML エンティティ (&nbsp; 等) をデコードしてから空白除去。
        # Farside の header は `&nbsp;&nbsp;IBIT` のような形でくるため必須。
        return [
            _html.unescape(re.sub(r'<[^>]+>', '', c)).replace('\xa0', '').strip()
            for c in cs
        ]

    # 3) ヘッダー (row 1) から ETF 名を取得
    header_cells = cells(rows[1])
    etf_indices: dict[str, int] = {}
    for i, h in enumerate(header_cells):
        for etf in TARGET_ETFS:
            if h.upper() == etf:
                etf_indices[etf] = i
    # Total 列はヘッダーにテキストで出ていないので、最後のセル index を total と仮定
    total_idx = len(header_cells) - 1

    # 4) row 4 から N-4 までを daily flows としてパース
    daily_flows: list[dict] = []
    for row_html in rows[4:-4]:
        cs = cells(row_html)
        if not cs:
            continue
        date_str = cs[0].strip()
        if not re.match(r'\d{1,2}\s+\w{3}\s+\d{4}', date_str):
            continue
        day_data = {"date": date_str, "flows": {}, "total": None}
        for etf, idx in etf_indices.items():
            if idx < len(cs):
                day_data["flows"][etf] = _parse_flow_value(cs[idx])
        if total_idx < len(cs):
            day_data["total"] = _parse_flow_value(cs[total_idx])
        if day_data["total"] is None:
            known = [v for v in day_data["flows"].values() if v is not None]
            if known:
                day_data["total"] = sum(known)
        if day_data["flows"] or day_data["total"] is not None:
            daily_flows.append(day_data)

    # 直近 5 件 (新しい順)
    daily_flows = list(reversed(daily_flows))[:5]
    if daily_flows:
        return {
            "source": "Farside Investors",
            "daily_flows": daily_flows,
            "error": None,
        }
    return None


async def _scrape_farside_playwright() -> Optional[dict]:
    """Farside を Playwright + stealth ヘッダーで取得する（403 回避の主経路）。

    - `--disable-blink-features=AutomationControlled` でボット検知を緩和
    - フル HTTP ヘッダー (Accept-Language / Sec-Ch-Ua) と User-Agent を実ブラウザ風に
    - `navigator.webdriver` を undefined に上書き
    """
    url = "https://farside.co.uk/btc/"
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = await browser.new_context(
                user_agent=USER_AGENT,
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
                    "Sec-Ch-Ua-Mobile": "?0",
                    "Sec-Ch-Ua-Platform": '"macOS"',
                },
                viewport={"width": 1280, "height": 800},
                ignore_https_errors=True,
            )
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = await context.new_page()
            await page.goto(url, timeout=BROWSER_TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_timeout(7000)
            html = await page.content()
            await browser.close()
        return _parse_farside_html(html)
    except Exception as e:
        print(f"  [WARN] Farside (Playwright): {e}")
        return None


async def _scrape_sosovalue() -> Optional[dict]:
    """SoSoValue からBTC ETFフローを取得する。"""
    url = "https://sosovalue.com/assets/etf/us-btc-spot"
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=USER_AGENT, ignore_https_errors=True)
            page = await context.new_page()

            await page.goto(url, timeout=BROWSER_TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_timeout(8000)

            body_text = await page.inner_text("body")

            daily_flows = []

            # テーブルからETFフローデータを取得
            rows = await page.query_selector_all('table tr, [class*="table"] [class*="row"]')
            for row in rows:
                try:
                    text = await row.inner_text()
                    # 日付パターンを探す
                    date_match = re.search(r'(\d{4}[-/]\d{2}[-/]\d{2}|\w+\s+\d{1,2},?\s*\d{4})', text)
                    if not date_match:
                        continue

                    day_data = {"date": date_match.group(1), "flows": {}, "total": None}

                    for etf in TARGET_ETFS:
                        pattern = rf'{etf}.*?([-+]?\$?[\d,.]+\s*[MB]?)'
                        match = re.search(pattern, text, re.IGNORECASE)
                        if match:
                            day_data["flows"][etf] = _parse_flow_value(match.group(1))

                    # 合計
                    total_match = re.search(r'(?:Total|Net).*?([-+]?\$?[\d,.]+\s*[MB]?)', text, re.IGNORECASE)
                    if total_match:
                        day_data["total"] = _parse_flow_value(total_match.group(1))

                    if day_data["flows"] or day_data["total"] is not None:
                        daily_flows.append(day_data)

                except Exception:
                    continue

            await browser.close()

            if daily_flows:
                return {
                    "source": "SoSoValue",
                    "daily_flows": daily_flows[:5],
                    "error": None,
                }

    except Exception as e:
        print(f"  [WARN] SoSoValue: {e}")

    return None


async def _scrape_coinglass_etf() -> Optional[dict]:
    """CoinGlass からBTC ETFフローを取得する。"""
    url = "https://www.coinglass.com/bitcoin-etf"
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=USER_AGENT, ignore_https_errors=True)
            page = await context.new_page()

            await page.goto(url, timeout=BROWSER_TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_timeout(8000)

            body_text = await page.inner_text("body")

            daily_flows = []

            # テーブルからデータを取得
            rows = await page.query_selector_all('table tr')
            for row in rows:
                try:
                    text = await row.inner_text()
                    date_match = re.search(r'(\d{4}[-/]\d{2}[-/]\d{2}|\w+\s+\d{1,2})', text)
                    if not date_match:
                        continue

                    day_data = {"date": date_match.group(1), "flows": {}, "total": None}

                    # 数値を抽出
                    values = re.findall(r'([-+]?[\d,.]+)', text)
                    if len(values) >= 2:
                        day_data["total"] = _parse_flow_value(values[-1])

                    if day_data["total"] is not None:
                        daily_flows.append(day_data)

                except Exception:
                    continue

            await browser.close()

            if daily_flows:
                return {
                    "source": "CoinGlass",
                    "daily_flows": daily_flows[:5],
                    "error": None,
                }

    except Exception as e:
        print(f"  [WARN] CoinGlass ETF: {e}")

    return None


def _parse_flow_value(text: str) -> Optional[float]:
    """フロー値のテキストを数値(百万USD)に変換する。"""
    if not text or text in ("-", "N/A", ""):
        return None
    text = text.strip().replace("$", "").replace(",", "").replace(" ", "")

    negative = text.startswith("-") or text.startswith("(")
    text = text.lstrip("-+(").rstrip(")")

    multiplier = 1.0
    if text.upper().endswith("B"):
        multiplier = 1000.0
        text = text[:-1]
    elif text.upper().endswith("M"):
        text = text[:-1]

    try:
        val = float(text) * multiplier
        return -val if negative else val
    except ValueError:
        return None


async def scrape_btc_etf() -> dict:
    """BTC ETFフローデータを取得する。優先順にソースを試行。

    Returns:
        {
            "source": str,
            "daily_flows": [
                {
                    "date": str,
                    "flows": {"IBIT": float, "FBTC": float, "GBTC": float},
                    "total": float | None,
                }
            ],
            "error": str | None,
        }
    """
    # 1. Farside (Playwright stealth) — 主経路
    print("  BTC ETF: Farside (Playwright stealth) を試行中...")
    data = await _scrape_farside_playwright()
    if data and data.get("daily_flows"):
        print(f"  [OK]    BTC ETF: Farside Playwright ({len(data['daily_flows'])}日分)")
        return data

    # 2. Farside (requests fallback) — 一応試行 (殆ど 403)
    print("  BTC ETF: Farside (requests fallback) を試行中...")
    data = _scrape_farside()
    if data and data.get("daily_flows"):
        print(f"  [OK]    BTC ETF: Farside requests ({len(data['daily_flows'])}日分)")
        return data

    # 3. SoSoValue
    print("  BTC ETF: SoSoValue を試行中...")
    data = await _scrape_sosovalue()
    if data and data.get("daily_flows"):
        print(f"  [OK]    BTC ETF: SoSoValue ({len(data['daily_flows'])}日分)")
        return data

    # 4. CoinGlass
    print("  BTC ETF: CoinGlass を試行中...")
    data = await _scrape_coinglass_etf()
    if data and data.get("daily_flows"):
        print(f"  [OK]    BTC ETF: CoinGlass ({len(data['daily_flows'])}日分)")
        return data

    return {
        "source": "取得不可",
        "daily_flows": [],
        "error": "全ソースからの取得に失敗",
    }


if __name__ == "__main__":
    data = asyncio.run(scrape_btc_etf())
    print(f"\n--- BTC ETF ({data['source']}) ---")
    if data["error"]:
        print(f"  Error: {data['error']}")
    for day in data.get("daily_flows", []):
        flows_str = ", ".join(f"{k}: {v}" for k, v in day.get("flows", {}).items())
        print(f"  {day['date']}: {flows_str} | Total: {day.get('total')}")
