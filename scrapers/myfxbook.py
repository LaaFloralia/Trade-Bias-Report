"""MyFXBook Sentiment スクレイパー

対象URL: https://www.myfxbook.com/community/outlook/{symbol}
取得データ: Long%, Short%, 平均ロング/ショートエントリー価格, 建玉実数 (lots / positions)
"""

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.async_api import async_playwright
from config import BROWSER_TIMEOUT, USER_AGENT


def _parse_outlook_text(page_text: str, result: dict) -> None:
    """outlook ページの body テキストからセンチメント値を抽出し result を更新する。

    純関数化してあるのは、MyFXBook の文言変更（2026-08 に「59% of ...」と % 付きに
    変わり旧regexが全滅した）をキャプチャ済み実テキストで回帰テストするため。
    """
    # MyFXBookのテキスト形式 (2026-08 時点、数値の直後に % が付く):
    # "59% of the forex traders are currently going short with XAU/USD,
    #  with an average price of 4136.0708, meanwhile 41% of the forex traders
    #  are going long with XAU/USD, with an average price of 4506.4624."
    # 旧形式 ("59 of the forex traders ...") も % を optional にして両対応。
    short_match = re.search(
        r'(\d+(?:\.\d+)?)\s*%?\s+of the forex traders are currently going short.*?'
        r'average price of\s+([\d,.]+)',
        page_text, re.DOTALL | re.IGNORECASE
    )
    long_match = re.search(
        r'(\d+(?:\.\d+)?)\s*%?\s+of the forex traders are (?:going|currently going) long.*?'
        r'average price of\s+([\d,.]+)',
        page_text, re.DOTALL | re.IGNORECASE
    )

    if short_match:
        result["short_pct"] = float(short_match.group(1))
        price_str = short_match.group(2).replace(",", "").rstrip(".")
        result["avg_short_entry"] = float(price_str)
    if long_match:
        result["long_pct"] = float(long_match.group(1))
        price_str = long_match.group(2).replace(",", "").rstrip(".")
        result["avg_long_entry"] = float(price_str)

    # Current Metrics テーブル:
    # "Short\t59 %\t1,226.16 lots\t6,956" / "Long\t41 %\t841.40 lots\t7,775"
    # % はフォールバック、Volume (lots) と Positions 数はここが唯一のソース。
    for side in ("short", "long"):
        m = re.search(
            rf'{side}\s+(\d+(?:\.\d+)?)\s*%\s+([\d,]+(?:\.\d+)?)\s*lots\s+([\d,]+)',
            page_text, re.IGNORECASE
        )
        if m:
            if result[f"{side}_pct"] is None:
                result[f"{side}_pct"] = float(m.group(1))
            result[f"{side}_volume_lots"] = float(m.group(2).replace(",", ""))
            result[f"{side}_positions"] = int(m.group(3).replace(",", ""))

    # フォールバック: テーブルから "Short XX %" / "Long XX %" のみ取得
    if result["short_pct"] is None:
        table_short = re.search(r'Short\s+(\d+)\s*%', page_text)
        table_long = re.search(r'Long\s+(\d+)\s*%', page_text)
        if table_short:
            result["short_pct"] = float(table_short.group(1))
        if table_long:
            result["long_pct"] = float(table_long.group(1))


async def scrape_myfxbook(symbol: str) -> dict:
    """MyFXBookからセンチメントデータを取得する。

    Args:
        symbol: 銘柄名 (例: "XAUUSD", "USDJPY")

    Returns:
        {
            "source": "MyFXBook",
            "symbol": str,
            "long_pct": float | None,
            "short_pct": float | None,
            "avg_long_entry": float | None,
            "avg_short_entry": float | None,
            "long_volume_lots": float | None,
            "short_volume_lots": float | None,
            "long_positions": int | None,
            "short_positions": int | None,
            "error": str | None,
        }
    """
    result = {
        "source": "MyFXBook",
        "symbol": symbol,
        "long_pct": None,
        "short_pct": None,
        "avg_long_entry": None,
        "avg_short_entry": None,
        "long_volume_lots": None,
        "short_volume_lots": None,
        "long_positions": None,
        "short_positions": None,
        "error": None,
    }

    url = f"https://www.myfxbook.com/community/outlook/{symbol}"

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=USER_AGENT, ignore_https_errors=True)
            page = await context.new_page()

            await page.goto(url, timeout=BROWSER_TIMEOUT, wait_until="domcontentloaded")

            # ページが完全にレンダリングされるまで待機
            await page.wait_for_timeout(5000)

            # --- Long/Short パーセンテージ & 平均エントリー価格取得 ---
            # 並列実行時（main.py の Phase 1 で複数銘柄 + CoinGlass 同時取得）は
            # レンダリングが 5s に間に合わないことがあるため、失敗時に 1 回だけ
            # 追加待機して再パースする。
            try:
                page_text = await page.inner_text("body")
                _parse_outlook_text(page_text, result)
                if result["long_pct"] is None and result["short_pct"] is None:
                    await page.wait_for_timeout(7000)
                    page_text = await page.inner_text("body")
                    _parse_outlook_text(page_text, result)
            except Exception as e:
                result["error"] = f"パーセンテージ取得失敗: {str(e)}"

            # --- フォールバック: ページ全体のHTMLをAIに渡すための生テキスト ---
            if result["long_pct"] is None and result["short_pct"] is None:
                try:
                    # ページのメインコンテンツ部分のテキストを取得
                    main_text = await page.inner_text("main, #content, .container, body")
                    # 最初の3000文字だけ保持（トークン節約）
                    result["raw_text"] = main_text[:3000]
                    result["error"] = "セレクタでの取得失敗。raw_textにページテキストを格納済み。"
                except:
                    pass

            await browser.close()

    except Exception as e:
        result["error"] = f"ページ取得失敗: {str(e)}"

    return result


# テスト用
if __name__ == "__main__":
    async def test():
        for sym in ["XAUUSD", "USDJPY"]:
            data = await scrape_myfxbook(sym)
            print(f"\n--- {sym} ---")
            for k, v in data.items():
                if k != "raw_text":
                    print(f"  {k}: {v}")
    asyncio.run(test())
