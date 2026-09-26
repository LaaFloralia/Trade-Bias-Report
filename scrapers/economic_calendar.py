"""経済指標カレンダー スクレイパー

対象URL: https://www.investing.com/economic-calendar/
取得データ: ハイインパクト指標（★★★）の今週・来週分
各指標: 日付、時刻（JST）、国、指標名、結果、前回値、予想値

Investing.comはブラウザのタイムゾーンに合わせて時刻を表示するため、
Playwrightで timezone_id="Asia/Tokyo" を設定し、表示時刻をそのままJSTとして扱う。
"""

from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.async_api import async_playwright
from config import BROWSER_TIMEOUT, USER_AGENT

US_MAJOR_INDICATORS = [
    "PCE", "Core PCE", "GDP", "Jobless Claims", "Initial Jobless",
    "Continuing Jobless", "Durable Goods", "CPI", "Core CPI",
    "NFP", "Nonfarm Payrolls", "Non-Farm", "Employment Change",
    "Unemployment Rate", "PPI", "Core PPI", "Retail Sales",
    "FOMC", "Fed Interest Rate", "ISM Manufacturing",
    "ISM Non-Manufacturing", "ISM Services",
]


def _sanity_check_us_time(indicator: str, country: str, time_jst: str) -> Optional[str]:
    """米国主要指標のJST時刻が妥当かチェックする。"""
    if not time_jst or ":" not in time_jst:
        return None
    if country not in ("United States", "US", "USA"):
        return None

    is_us_major = any(kw.lower() in indicator.lower() for kw in US_MAJOR_INDICATORS)
    if not is_us_major:
        return None

    try:
        hour = int(time_jst.split(":")[0])
    except ValueError:
        return None

    # 06:00-08:00 JST に米国指標 → 変換エラーの可能性極めて高い
    if 6 <= hour <= 8:
        return f"[WARN] {indicator}: JST {time_jst} は変換エラーの可能性が高い（06-08時帯）"

    # 米国主要指標は通常 JST 19:00-翌05:00 の範囲
    if not (19 <= hour <= 23 or 0 <= hour <= 5):
        return f"[WARN] {indicator}: JST {time_jst} は通常の米国指標発表時間帯（19:00-05:00 JST）外"

    return None


async def _scrape_week(page, week_label: str) -> list:
    """現在表示中のカレンダーから★★★イベントを取得する。"""
    events = []  # type: List[dict]
    current_date = ""

    rows = await page.query_selector_all("table tr")
    for row in rows:
        try:
            html = await row.inner_html()

            date_match = re.search(
                r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*(\w+\s+\d{1,2},\s*\d{4})',
                html,
            )
            if date_match:
                current_date = date_match.group(0)
                continue

            if "Holiday" in html and "colspan" not in html:
                text = await row.inner_text()
                if "Holiday" in text:
                    continue

            star_count = html.count("opacity-60")
            if star_count < 5:
                continue

            text = await row.inner_text()
            text = text.strip()
            if not text or len(text) < 5:
                continue

            # 時刻を取得（Investing.comは timezone_id 設定に合わせて表示する）
            time_match = re.search(r'(\d{1,2}:\d{2})', text)
            event_time = time_match.group(1) if time_match else ""

            # timezone_id="Asia/Tokyo" で表示されているため、そのままJST
            time_jst = event_time

            country = ""
            flag_el = await row.query_selector("[data-test^='flag-']")
            if flag_el:
                country = (await flag_el.get_attribute("aria-label") or "").strip()
                if not country:
                    test_attr = await flag_el.get_attribute("data-test") or ""
                    country = test_attr.replace("flag-", "").upper()

            indicator = ""
            link_el = await row.query_selector("a[href*='economic-calendar']")
            if link_el:
                indicator = (await link_el.inner_text()).strip()

            if not indicator:
                parts = text.split("\t")
                for part in parts:
                    part = part.strip()
                    if len(part) > 5 and not re.match(r'^\d', part) and part not in ("Holiday",):
                        indicator = part
                        break

            if not indicator:
                continue

            actual = ""
            previous = ""
            forecast = ""

            cells = await row.query_selector_all("td")
            cell_texts = []
            for cell in cells:
                ct = (await cell.inner_text()).strip()
                cell_texts.append(ct)

            if len(cell_texts) >= 8:
                # 列: 時刻+通貨 / 時刻 / 通貨 / 指標 / 重要度 / 結果 / 予想 / 前回（2026-09-24 実表示で確認）
                actual = cell_texts[5] if cell_texts[5] else ""
                forecast = cell_texts[6] if cell_texts[6] else ""
                previous = cell_texts[7] if cell_texts[7] else ""
            elif len(cell_texts) >= 2:
                for ct in reversed(cell_texts):
                    val = re.search(r'[-+]?[\d,.]+[%KMB]?', ct)
                    if val and not previous:
                        previous = val.group(0)
                    elif val and not forecast:
                        forecast = val.group(0)
                        break

            # サニティチェック
            warning = _sanity_check_us_time(indicator, country, time_jst)
            if warning:
                print(f"  {warning}")

            events.append({
                "date": current_date,
                "time_jst": time_jst,
                "country": country,
                "indicator": indicator,
                "actual": actual or "N/A",
                "previous": previous or "N/A",
                "forecast": forecast or "N/A",
            })

        except Exception:
            continue

    return events


async def _load_with_retry(load, attempts: int = 2):
    """週タブの切替が効かず当日表示（休日は「No Events Scheduled」）だけを読む一時的な失敗に備え、
    イベントが0件なら読み直す。戻り値は (events, missing_tabs, body_text, 試行回数)。"""
    events, missing_tabs, body = [], [], ""
    for attempt in range(1, attempts + 1):
        events, missing_tabs, body = await load()
        if events:
            return events, missing_tabs, body, attempt
    return events, missing_tabs, body, attempts


async def scrape_economic_calendar() -> dict:
    """Investing.com から★★★経済指標を取得する。

    Returns:
        {
            "source": str,
            "events": list[dict],
            "error": str | None,
        }
    """
    result = {
        "source": "Investing.com Economic Calendar",
        "events": [],
        "error": None,
    }  # type: dict

    url = "https://www.investing.com/economic-calendar/"

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=USER_AGENT,
                timezone_id="Asia/Tokyo",
                ignore_https_errors=True,
            )
            async def load_once():
                page = await context.new_page()
                try:
                    await page.goto(url, timeout=BROWSER_TIMEOUT, wait_until="domcontentloaded")
                    await page.wait_for_timeout(5000)
                    events, missing_tabs = [], []
                    for label in ("This Week", "Next Week"):
                        button = await page.query_selector(f'a:has-text("{label}"), button:has-text("{label}")')
                        if not button:
                            missing_tabs.append(label)
                            continue
                        await button.click()
                        await page.wait_for_timeout(3000)
                        events.extend(await _scrape_week(page, label))
                    body = "" if events else (await page.inner_text("body"))[:5000]
                    return events, missing_tabs, body
                finally:
                    await page.close()

            events, missing_tabs, body, attempts = await _load_with_retry(load_once)
            result["events"] = events
            result["attempts"] = attempts
            if missing_tabs:
                result["missing_tabs"] = missing_tabs
            if not events:
                result["raw_text"] = body
                result["error"] = ("★★★イベントが見つかりませんでした。"
                                   + (f"（週タブ未検出: {'・'.join(missing_tabs)}）" if missing_tabs else ""))

            await browser.close()

    except Exception as e:
        result["error"] = f"ページ取得失敗: {str(e)}"

    return result


if __name__ == "__main__":
    data = asyncio.run(scrape_economic_calendar())
    print(f"Source: {data['source']}")
    print(f"Events: {len(data['events'])}")
    if data.get("error"):
        print(f"Error: {data['error']}")
    for ev in data["events"][:20]:
        print(f"  {ev['date']} {ev['time_jst']} JST | {ev['country']} | {ev['indicator']} | 前回: {ev['previous']} | 予想: {ev['forecast']}")
