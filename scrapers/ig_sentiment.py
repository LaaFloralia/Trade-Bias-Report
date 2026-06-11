"""IG Client Sentiment スクレイパー

対象URL: https://www.ig.com/en/... (各銘柄ページ)
取得データ: Long% / Short%
"""

import asyncio
import re
from playwright.async_api import async_playwright
from config import BROWSER_TIMEOUT, IG_URLS, USER_AGENT

# IG の銘柄別 URL マッピングは config.yaml（SSoT）の instruments.*.ig_url から供給される
# 注: DXY と BTCUSD は IG International (ig.com/en) ではページが存在しない


async def scrape_ig_sentiment(symbol: str) -> dict:
    result = {
        "source": "IG Client Sentiment",
        "symbol": symbol,
        "long_pct": None,
        "short_pct": None,
        "error": None,
    }

    url = IG_URLS.get(symbol)
    if not url:
        result["error"] = f"IG URL未設定: {symbol}"
        return result

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=USER_AGENT, ignore_https_errors=True)
            page = await context.new_page()

            await page.goto(url, timeout=BROWSER_TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_timeout(8000)

            # Cookie バナーを閉じる（存在する場合）
            try:
                cookie_btn = await page.query_selector('[class*="cookie"] button, [id*="cookie"] button, .cmp-btn-accept')
                if cookie_btn:
                    await cookie_btn.click()
                    await page.wait_for_timeout(1000)
            except:
                pass

            # センチメントデータの取得
            body_text = await page.inner_text("body")

            # IGのテキスト形式: "XX% of client accounts are short on this market"
            # また "Long" "Short" "27%" "73%" がセクションとして並ぶ
            short_match = re.search(
                r'(\d+)\s*%\s*of client accounts are short',
                body_text, re.IGNORECASE
            )
            long_match = re.search(
                r'(\d+)\s*%\s*of client accounts are long',
                body_text, re.IGNORECASE
            )

            if short_match:
                result["short_pct"] = float(short_match.group(1))
            if long_match:
                result["long_pct"] = float(long_match.group(1))

            # フォールバック: "Long\nShort\nXX%\nYY%" パターン
            if result["long_pct"] is None and result["short_pct"] is None:
                ls_match = re.search(
                    r'Long\s+Short\s+(\d+)\s*%\s+(\d+)\s*%',
                    body_text, re.IGNORECASE
                )
                if ls_match:
                    result["long_pct"] = float(ls_match.group(1))
                    result["short_pct"] = float(ls_match.group(2))

            # 補完: long + short = 100 の関係を利用
            if result["long_pct"] and not result["short_pct"]:
                result["short_pct"] = round(100 - result["long_pct"], 1)
            elif result["short_pct"] and not result["long_pct"]:
                result["long_pct"] = round(100 - result["short_pct"], 1)

            if result["long_pct"] is None:
                result["raw_text"] = body_text[:3000]
                result["error"] = "セレクタでの取得失敗。raw_textにページテキストを格納済み。"

            await browser.close()

    except Exception as e:
        result["error"] = f"ページ取得失敗: {str(e)}"

    return result


if __name__ == "__main__":
    async def test():
        for sym in ["XAUUSD", "USDJPY", "BTCUSD"]:
            data = await scrape_ig_sentiment(sym)
            print(f"\n--- {sym} ---")
            for k, v in data.items():
                if k != "raw_text":
                    print(f"  {k}: {v}")
    asyncio.run(test())
