"""FXSSI Current Ratio スクレイパー

対象URL: https://fxssi.com/tools/current-ratio
取得データ: 各銘柄の Buy% / Sell% (加重平均)
"""

import asyncio
import re
from playwright.async_api import async_playwright
from config import BROWSER_TIMEOUT, FXSSI_SYMBOLS, USER_AGENT


async def scrape_fxssi() -> dict:
    """FXSSIからCurrent Ratio データを一括取得する。

    Returns:
        {
            "source": "FXSSI",
            "data": {
                "XAUUSD": {"buy_pct": float, "sell_pct": float},
                "USDJPY": {"buy_pct": float, "sell_pct": float},
                ...
            },
            "error": str | None,
        }
    """
    result = {"source": "FXSSI", "data": {}, "error": None}
    url = "https://fxssi.com/tools/current-ratio"

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=USER_AGENT, ignore_https_errors=True)
            page = await context.new_page()

            await page.goto(url, timeout=BROWSER_TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)  # JSレンダリング待機

            # ページ全体のテキストを取得
            body_text = await page.inner_text("body")

            # 各銘柄のデータを正規表現で抽出
            # 抽出対象は config.yaml（SSoT）から供給される
            target_symbols = FXSSI_SYMBOLS
            for symbol in target_symbols:
                # パターン: "XAUUSD ... XX% ... XX%" のような並び
                pattern = rf'{symbol}.*?(\d+(?:\.\d+)?)\s*%.*?(\d+(?:\.\d+)?)\s*%'
                match = re.search(pattern, body_text, re.DOTALL)
                if match:
                    val1, val2 = float(match.group(1)), float(match.group(2))
                    # 通常は Buy% が先に表示される
                    result["data"][symbol] = {
                        "buy_pct": val1,
                        "sell_pct": val2,
                    }

            # フォールバック: テーブル要素から取得
            if not result["data"]:
                rows = await page.query_selector_all("tr, [class*='ratio'], [class*='pair']")
                for row in rows:
                    text = await row.inner_text()
                    for symbol in target_symbols:
                        if symbol in text:
                            numbers = re.findall(r'(\d+(?:\.\d+)?)', text)
                            pcts = [float(n) for n in numbers if 0 < float(n) <= 100]
                            if len(pcts) >= 2:
                                result["data"][symbol] = {
                                    "buy_pct": pcts[0],
                                    "sell_pct": pcts[1],
                                }

            if not result["data"]:
                raw = body_text[:3000]
                result["raw_text"] = raw
                result["error"] = "セレクタでの取得失敗。raw_textにページテキストを格納済み。"

            await browser.close()

    except Exception as e:
        result["error"] = f"ページ取得失敗: {str(e)}"

    return result


if __name__ == "__main__":
    asyncio.run(scrape_fxssi()).items().__iter__
    data = asyncio.run(scrape_fxssi())
    print(data)
