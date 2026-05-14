"""CME FedWatch 確率スクレイパー（マルチソース）

ソース優先順:
  1. Investing.com Fed Rate Monitor — Stealth Playwright で「Target Rate / Probability」テーブル抽出
     URL: https://www.investing.com/central-banks/fed-rate-monitor
     取得: 次回 FOMC 日、Future Price、各 Target Rate ごとの (現在/前日/前週) 確率
     CME 公式の Fed Funds Futures から計算しているため数値は CME FedWatch Tool と同一。
  2. CME 公式 Page (Playwright) — 60s timeout が頻発 (Cloudflare bot mitigation)。フォールバックのみ。

旧 CME 直接 Page.goto は Cloudflare timeout で殆ど失敗するため、Investing.com を最優先化した
（2026-05-14 改修）。CME 公式 paid API (`https://markets.api.cmegroup.com/fedwatch/v1`) は
OAuth2 + 有償のため非採用。
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.async_api import async_playwright
from config import BROWSER_TIMEOUT, USER_AGENT


async def _scrape_investing_fedwatch() -> Optional[dict]:
    """Investing.com Fed Rate Monitor から FedWatch データを取得する。

    ページ構造（2026-05 時点）:
        Meeting Time: <next FOMC date>
        Future Price: <CME Fed Funds future price>
        <Target Rate range> <Current Probability%>
        ...
        Target Rate | Current Probability% | Previous Day Probability% | Previous Week Probability%
        <range> | <cur%> | <prev_day%> | <prev_week%>
        ...

    抽出データ:
        - next_fomc_date (例: "Jun 17, 2026")
        - future_price (例: 96.370)
        - target_rates: [{"range": "3.50 - 3.75", "current": 99.0, "prev_day": 99.2, "prev_week": 94.6}, ...]
        - 互換: hold_pct / cut_25bp_pct / cut_50bp_pct / hike_25bp_pct (現在の Fed Funds rate を基準に算出)
    """
    url = "https://www.investing.com/central-banks/fed-rate-monitor"
    result = {
        "source": "Investing.com Fed Rate Monitor (CME Fed Funds futures)",
        "next_fomc_date": None,
        "future_price": None,
        "target_rates": [],
        "cut_25bp_pct": None,
        "cut_50bp_pct": None,
        "hold_pct": None,
        "hike_25bp_pct": None,
        "raw_probabilities": None,
        "error": None,
    }

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
                },
                ignore_https_errors=True,
            )
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = await context.new_page()
            await page.goto(url, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(8000)

            body_text = await page.inner_text("body")
            await browser.close()
    except Exception as e:
        result["error"] = f"Investing.com fed-rate-monitor 失敗: {e}"
        return result

    # 1) Meeting Time: <date>
    meet = re.search(r"Meeting Time:\s*(\w{3}\s+\d{1,2},\s*\d{4})", body_text)
    if meet:
        result["next_fomc_date"] = meet.group(1)

    # 2) Future Price: <price>
    fp = re.search(r"Future Price:\s*([\d.]+)", body_text)
    if fp:
        try:
            result["future_price"] = float(fp.group(1))
        except ValueError:
            pass

    # 3) Target Rate テーブル: "X.XX - X.XX \tCUR%\tPREV_DAY%\tPREV_WEEK%"
    # 行ごとに数値 4 列をパース
    table_re = re.compile(
        r"(\d\.\d{2}\s*-\s*\d\.\d{2})\s*\t\s*(\d{1,3}(?:\.\d+)?)\s*%\s*\t\s*"
        r"(\d{1,3}(?:\.\d+)?)\s*%\s*\t\s*(\d{1,3}(?:\.\d+)?)\s*%"
    )
    for m in table_re.finditer(body_text):
        try:
            result["target_rates"].append({
                "range": m.group(1).replace(" ", ""),
                "current": float(m.group(2)),
                "prev_day": float(m.group(3)),
                "prev_week": float(m.group(4)),
            })
        except ValueError:
            continue

    # 4) 互換用フィールド: 現在 Fed Funds 政策金利を推定し、それに対する据置/利下げ/利上げ確率を出す。
    # 現在の Fed Funds rate range は「最大確率の Target Rate range」を使う近似で十分。
    # ただし Investing は通常 2 つの rate range のみ表示するので、シンプルに最大確率を hold_pct とみなす。
    if result["target_rates"]:
        # 最大確率の range を「直近の市場予想」として hold_pct に
        top = max(result["target_rates"], key=lambda r: r["current"])
        result["hold_pct"] = top["current"]
        # 残りの range の合計確率を「変動確率」として hike/cut に振り分け
        # range 表記から数値を取り出して、top より低ければ cut、高ければ hike
        try:
            top_low, top_high = [float(x) for x in re.findall(r"\d\.\d{2}", top["range"])]
            for tr in result["target_rates"]:
                if tr is top:
                    continue
                lo, hi = [float(x) for x in re.findall(r"\d\.\d{2}", tr["range"])]
                if hi <= top_low:
                    # range は top より下 → 利下げ
                    diff_bp = round((top_high - hi) * 100)
                    if diff_bp <= 25:
                        result["cut_25bp_pct"] = (result.get("cut_25bp_pct") or 0) + tr["current"]
                    elif diff_bp <= 50:
                        result["cut_50bp_pct"] = (result.get("cut_50bp_pct") or 0) + tr["current"]
                elif lo >= top_high:
                    diff_bp = round((lo - top_low) * 100)
                    if diff_bp <= 25:
                        result["hike_25bp_pct"] = (result.get("hike_25bp_pct") or 0) + tr["current"]
        except Exception:
            pass

    if result["next_fomc_date"] is None and not result["target_rates"]:
        result["error"] = "Investing.com fed-rate-monitor 値抽出失敗"
        return result

    return result


async def scrape_fedwatch() -> dict:
    """FedWatch 確率データを取得する。

    優先順:
      1. Investing.com Fed Rate Monitor (CME Fed Funds Futures 経由) — 安定取得可能
      2. CME 公式 Page — Cloudflare timeout で殆ど失敗するためフォールバック扱い

    Returns:
        {
            "source": str,
            "next_fomc_date": str | None,
            "future_price": float | None (Investing 経由のみ),
            "target_rates": list[dict] (Investing 経由のみ、各 rate range の確率),
            "cut_25bp_pct": float | None,
            "cut_50bp_pct": float | None,
            "hold_pct": float | None,
            "hike_25bp_pct": float | None,
            "raw_probabilities": dict | None,
            "error": str | None,
        }
    """
    # 1. Investing.com を最優先で試行
    inv = await _scrape_investing_fedwatch()
    if inv and inv.get("error") is None and (
        inv.get("hold_pct") is not None or inv.get("target_rates")
    ):
        return inv

    # 2. CME 公式へフォールバック (殆ど通らないが残置)
    result = {
        "source": "CME FedWatch (fallback)",
        "next_fomc_date": None,
        "future_price": None,
        "target_rates": [],
        "cut_25bp_pct": None,
        "cut_50bp_pct": None,
        "hold_pct": None,
        "hike_25bp_pct": None,
        "raw_probabilities": None,
        "error": None,
    }

    url = "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html"

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-http2"],
            )
            context = await browser.new_context(
                user_agent=USER_AGENT,
                extra_http_headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                ignore_https_errors=True,
            )
            page = await context.new_page()

            await page.goto(url, timeout=60000, wait_until="domcontentloaded")
            # FedWatch はJS重いので長めに待機
            await page.wait_for_timeout(10000)

            body_text = await page.inner_text("body")

            # 次回FOMC日を取得
            fomc_date_match = re.search(
                r'(?:Meeting Date|FOMC Meeting|Next Meeting)[:\s]*(\w+\s+\d{1,2},?\s*\d{4}|\d{1,2}\s+\w+\s+\d{4})',
                body_text, re.IGNORECASE
            )
            if fomc_date_match:
                result["next_fomc_date"] = fomc_date_match.group(1).strip()

            # テーブルからFOMC日と確率データを取得
            # CMEのページ構造は動的だが、通常テーブルに確率が表示される
            # パターン1: "XX.X%" 形式の確率値を探す
            probabilities = {}

            # テーブル行から確率データを探す
            table_rows = await page.query_selector_all('table tr, [class*="meeting"] [class*="row"], [class*="probability"]')
            for row in table_rows:
                try:
                    text = await row.inner_text()
                    pcts = re.findall(r'(\d+(?:\.\d+)?)\s*%', text)
                    if pcts and len(pcts) >= 1:
                        # 日付を探す
                        date_in_row = re.search(r'(\w{3,9}\s+\d{1,2})', text)
                        if date_in_row:
                            if not result["next_fomc_date"]:
                                result["next_fomc_date"] = date_in_row.group(1)
                except Exception:
                    continue

            # テキストベースのパースを試みる
            # パターン: "No Change XX.X%", "25bp Cut XX.X%", etc.
            hold_match = re.search(r'(?:No\s*Change|Hold|Unchanged|据え置き)\s*[-:=]?\s*(\d+(?:\.\d+)?)\s*%', body_text, re.IGNORECASE)
            if hold_match:
                result["hold_pct"] = float(hold_match.group(1))

            cut25_match = re.search(r'(?:25\s*(?:bp|bps)\s*(?:Cut|Decrease|Lower))\s*[-:=]?\s*(\d+(?:\.\d+)?)\s*%', body_text, re.IGNORECASE)
            if not cut25_match:
                cut25_match = re.search(r'(\d+(?:\.\d+)?)\s*%\s*[-–]?\s*25\s*(?:bp|bps)\s*(?:Cut|Decrease|Lower)', body_text, re.IGNORECASE)
            if cut25_match:
                result["cut_25bp_pct"] = float(cut25_match.group(1))

            cut50_match = re.search(r'(?:50\s*(?:bp|bps)\s*(?:Cut|Decrease|Lower))\s*[-:=]?\s*(\d+(?:\.\d+)?)\s*%', body_text, re.IGNORECASE)
            if not cut50_match:
                cut50_match = re.search(r'(\d+(?:\.\d+)?)\s*%\s*[-–]?\s*50\s*(?:bp|bps)\s*(?:Cut|Decrease|Lower)', body_text, re.IGNORECASE)
            if cut50_match:
                result["cut_50bp_pct"] = float(cut50_match.group(1))

            hike_match = re.search(r'(?:25\s*(?:bp|bps)\s*(?:Hike|Increase|Raise))\s*[-:=]?\s*(\d+(?:\.\d+)?)\s*%', body_text, re.IGNORECASE)
            if hike_match:
                result["hike_25bp_pct"] = float(hike_match.group(1))

            # チャートの値を取得する試み（barやsvg等の内部テキスト）
            chart_texts = await page.query_selector_all('[class*="chart"] text, [class*="bar"] span, [class*="prob"] span')
            raw_probs = []
            for el in chart_texts:
                try:
                    t = await el.inner_text()
                    pct_m = re.search(r'(\d+(?:\.\d+)?)\s*%', t)
                    if pct_m:
                        raw_probs.append(float(pct_m.group(1)))
                except Exception:
                    continue

            if raw_probs:
                result["raw_probabilities"] = raw_probs

            # 何も取得できなかった場合
            if all(v is None for v in [result["hold_pct"], result["cut_25bp_pct"], result["cut_50bp_pct"]]):
                # body_textからすべての確率値を抽出
                all_pcts = re.findall(r'(\d+(?:\.\d+)?)\s*%', body_text)
                if all_pcts:
                    result["raw_probabilities"] = [float(p) for p in all_pcts[:20]]

                result["raw_text"] = body_text[:5000]
                result["error"] = "確率データの自動パース失敗。raw_textにページテキストを格納済み。"

            await browser.close()

    except Exception as e:
        result["error"] = f"CME取得失敗: {str(e)}"

    # CME 失敗時 (Investing.com は冒頭で既に試行済み): 取得不可として返す
    if all(v is None for v in [result["hold_pct"], result["cut_25bp_pct"], result["cut_50bp_pct"]]):
        if result.get("error") is None:
            result["error"] = "Investing.com + CME 両方失敗"

    return result


if __name__ == "__main__":
    data = asyncio.run(scrape_fedwatch())
    print("\n--- FedWatch ---")
    for k, v in data.items():
        if k not in ("raw_text", "raw_probabilities"):
            print(f"  {k}: {v}")
    if data.get("raw_probabilities"):
        print(f"  raw_probabilities: {data['raw_probabilities'][:10]}")
