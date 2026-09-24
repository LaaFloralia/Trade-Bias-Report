"""経済指標のサプライズ（結果 − 予想）と ForexFactory による予想値の補完

価格を動かすのは発表値そのものではなく予想との差（Andersen et al. 2003, AER）。
Investing.com の表から結果・予想を取り、予想が空欄の指標だけ ForexFactory の
週次フィード（予想・前回・重要度。結果は含まない）で補う。

金への方向は「米指標が予想より強い → 米金利・ドル高 → 金に下向き」という一般的な
反応を機械的に付けるだけで、実際の値動きの確認は別途行う。単位が異なる値同士や
数値化できない値は計算しない。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

JST = timezone(timedelta(hours=9))
FF_WEEK_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FF_COUNTRY = {
    "USD": "United States", "EUR": "Euro Zone", "GBP": "United Kingdom", "JPY": "Japan",
    "CHF": "Switzerland", "AUD": "Australia", "CAD": "Canada", "NZD": "New Zealand", "CNY": "China",
}

# 米指標で「予想より高い」が金にとって下向き (-1) か上向き (+1) か。先に一致したものを使う。
US_GOLD_SIGN = (
    ("unemployment rate", +1),
    ("jobless claims", +1),
    ("continuing claims", +1),
    ("nonfarm payrolls", -1), ("non-farm", -1), ("nfp", -1),
    ("average hourly earnings", -1),
    ("cpi", -1), ("pce", -1), ("ppi", -1),
    ("retail sales", -1),
    ("gdp", -1),
    ("ism", -1), ("pmi", -1),
    ("durable goods", -1),
    ("jolts", -1),
    ("consumer confidence", -1), ("consumer sentiment", -1),
    ("home sales", -1), ("housing starts", -1), ("building permits", -1),
    ("interest rate decision", -1), ("fed interest rate", -1),
)

_VALUE_RE = re.compile(r"^\s*([-+]?\d[\d,]*\.?\d*)\s*([%KMBT]?)\s*$", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z]{3,}")
_STOP_WORDS = {"the", "and", "mom", "yoy", "qoq", "aug", "sep", "oct", "nov", "dec", "jan", "feb",
               "mar", "apr", "may", "jun", "jul", "final", "prelim", "flash"}


def parse_value(text) -> Optional[tuple[float, str]]:
    """'201K' → (201.0, 'K')、'-0.3%' → (-0.3, '%')。数値化できなければ None。"""
    if text is None:
        return None
    m = _VALUE_RE.match(str(text))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "")), m.group(2).upper()
    except ValueError:
        return None


def event_datetime_jst(event: dict) -> Optional[datetime]:
    """Investing の 'Thursday, September 24, 2026' + '21:30' を JST の datetime にする。"""
    date_text = str(event.get("date", "")).split(",", 1)[-1].strip()
    time_text = str(event.get("time_jst", "")).strip()
    if not re.fullmatch(r"\d{1,2}:\d{2}", time_text):
        return None
    try:
        return datetime.strptime(f"{date_text} {time_text}", "%B %d, %Y %H:%M").replace(tzinfo=JST)
    except ValueError:
        return None


def gold_sign(event: dict) -> int:
    """米指標のみ。予想より高い値が金に上向きなら +1、下向きなら -1、対象外は 0。"""
    if event.get("country") not in ("United States", "US", "USA"):
        return 0
    name = str(event.get("indicator", "")).lower()
    for keyword, sign in US_GOLD_SIGN:
        if keyword in name:
            return sign
    return 0


def compute_surprise(event: dict) -> Optional[dict]:
    """結果と予想が同じ単位の数値なら差を返す。"""
    actual, forecast = parse_value(event.get("actual")), parse_value(event.get("forecast"))
    if actual is None or forecast is None or actual[1] != forecast[1]:
        return None
    diff = actual[0] - forecast[0]
    sign = gold_sign(event)
    if sign == 0 or diff == 0:
        direction = "判定対象外" if sign == 0 else "予想どおり"
    else:
        direction = "金に上向き" if diff * sign > 0 else "金に下向き"
    return {"diff": round(diff, 6), "unit": actual[1],
            "relative": round(diff / abs(forecast[0]), 4) if forecast[0] else None,
            "gold_direction": direction}


def released_surprises(events: list, now: datetime, lookback_hours: int = 36) -> list[dict]:
    """発表済み（結果あり）で直近 lookback_hours 以内の指標にサプライズを付けて返す。"""
    out = []
    for ev in events or []:
        when = event_datetime_jst(ev)
        if when is None or not (now - timedelta(hours=lookback_hours) <= when <= now):
            continue
        if parse_value(ev.get("actual")) is None:
            continue
        out.append({**ev, "released_at_jst": when.isoformat(), "surprise": compute_surprise(ev)})
    return out


def fetch_ff_week(timeout: int = 15) -> dict:
    """ForexFactory 週次フィード（今週分）。失敗しても例外を上げず error を返す。"""
    try:
        resp = requests.get(FF_WEEK_URL, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        rows = resp.json()
    except Exception as e:  # noqa: BLE001 — 補完用。本体の取得は止めない
        return {"events": [], "error": f"ForexFactory 取得失敗: {type(e).__name__}"}
    if not isinstance(rows, list):
        return {"events": [], "error": "ForexFactory 応答形式が不正"}
    events = []
    for row in rows:
        try:
            when = datetime.fromisoformat(row["date"]).astimezone(JST)
        except (KeyError, TypeError, ValueError):
            continue
        events.append({"country": FF_COUNTRY.get(row.get("country"), row.get("country")),
                       "title": row.get("title", ""), "impact": row.get("impact", ""),
                       "forecast": row.get("forecast", ""), "previous": row.get("previous", ""),
                       "datetime_jst": when})
    return {"events": events, "error": None}


def _words(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower())) - _STOP_WORDS


def fill_missing_forecasts(events: list, ff_events: list) -> int:
    """予想が N/A の指標を、同じ国・同じ時刻（±5分）・名称の語が重なる FF 行で補う。"""
    filled = 0
    for ev in events or []:
        if parse_value(ev.get("forecast")) is not None:
            continue
        when = event_datetime_jst(ev)
        if when is None:
            continue
        words = _words(str(ev.get("indicator", "")))
        for ff in ff_events:
            if (ff["country"] == ev.get("country") and parse_value(ff["forecast"]) is not None
                    and abs((ff["datetime_jst"] - when).total_seconds()) <= 300
                    and words & _words(ff["title"])):
                ev["forecast"] = ff["forecast"]
                ev["forecast_source"] = "ForexFactory"
                filled += 1
                break
    return filled


def format_surprise_lines(surprises: list) -> list[str]:
    lines = ["### 指標サプライズ（発表済み・直近36時間）"]
    if not surprises:
        lines.append("- 該当なし（直近36時間に結果が出た★★★指標なし、または結果未取得）")
        return lines
    for ev in surprises:
        s = ev.get("surprise")
        head = (f"- {ev.get('released_at_jst', '')[:16].replace('T', ' ')} JST | {ev.get('country', '')} | "
                f"{ev.get('indicator', '')} | 結果 {ev.get('actual')} / 予想 {ev.get('forecast')} / 前回 {ev.get('previous')}")
        if s is None:
            lines.append(head + " | 差: 計算不可（予想欠測または単位不一致）")
        else:
            lines.append(head + f" | 差 {s['diff']:+g}{s['unit']} | 一般的な反応: {s['gold_direction']}")
    lines.append("※ 反応方向は米指標の一般則。実際の金の値動きが逆なら、別の買い手・売り手の存在を示す材料として扱う")
    return lines
