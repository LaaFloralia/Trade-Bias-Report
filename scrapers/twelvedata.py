"""Twelve Data API — 価格データ取得モジュール

対象銘柄: XAUUSD, USDJPY, BTCUSD
取得データ: 現在値, PDH/PDL, PWH/PWL, PMH/PML, IPDA 20/40/60日レンジ

API呼び出し数の最小化:
  - /quote: 1回（3銘柄一括）
  - /time_series: 1回（3銘柄一括、60本）
"""

import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple, List

# scrapers/twelvedata.py として直接実行した場合もプロジェクトルートを参照できるようにする
sys.path.insert(0, str(Path(__file__).parent.parent))

import requests
from config import TWELVEDATA_API_KEY, TWELVEDATA_SYMBOLS

BASE_URL = "https://api.twelvedata.com"

# 銘柄定義は config.yaml（SSoT）から供給される
SYMBOL_MAP = TWELVEDATA_SYMBOLS

def _get(endpoint: str, params: dict) -> Optional[dict]:
    """GETリクエスト。HTTP 429 または JSON level の rate limit 応答時に5秒待って1回リトライ。"""
    url = f"{BASE_URL}{endpoint}"
    p = {**params, "apikey": TWELVEDATA_API_KEY}

    def _do_request():
        resp = requests.get(url, params=p, timeout=15)
        if resp.status_code == 429:
            return None  # リトライ要
        resp.raise_for_status()
        data = resp.json()
        # JSON レベルのレート制限エラー（HTTP 200 だが status: error / code: 429）
        if isinstance(data, dict) and data.get("status") == "error" and data.get("code") == 429:
            return None  # リトライ要
        return data

    try:
        result = _do_request()
        if result is None:
            print(f"  [WARN] Twelve Data rate limit — 5秒後リトライ ({endpoint})")
            time.sleep(5)
            result = _do_request()
        return result
    except Exception as e:
        print(f"  [ERROR] Twelve Data {endpoint}: {e}")
        return None


def _parse_quotes(data: Optional[dict], symbols: List[str]) -> dict:
    """_get の結果をシンボル→quoteデータの辞書に変換する。"""
    if data is None:
        return {}
    # 複数シンボル: {"XXX/YYY": {...}, ...}（キーは TD シンボル）
    if all(sym in data for sym in symbols):
        return data
    # 単一シンボル: {"symbol": "XXX/YYY", "close": "...", ...}
    if "symbol" in data and "close" in data:
        return {data["symbol"]: data}
    # 全体がエラー応答の場合
    if data.get("status") == "error":
        print(f"  [ERROR] Twelve Data quote error: {data.get('message', 'unknown')}")
        return {}
    # フォールバック: キーがシンボルと一致するか試みる
    return data


def _parse_series_batch(data: Optional[dict], symbols: List[str]) -> dict:
    """バッチ time_series 応答をシンボル→valuesリストの辞書に変換する。"""
    if data is None:
        return {}
    if data.get("status") == "error":
        print(f"  [ERROR] Twelve Data time_series error: {data.get('message', 'unknown')}")
        return {}
    result = {}
    for sym in symbols:
        if sym in data and isinstance(data[sym], dict):
            result[sym] = data[sym].get("values", [])
        elif "values" in data:
            # 単一シンボルの場合
            result[sym] = data.get("values", [])
    return result


def _calc_range(values: List[dict], n: int) -> Tuple[Optional[float], Optional[float]]:
    """直近 n 本の high/low の最大・最小を返す（IPDA ローリングレンジ用）。"""
    target = values[:n]
    if not target:
        return None, None
    try:
        highs = [float(v["high"]) for v in target]
        lows = [float(v["low"]) for v in target]
        return max(highs), min(lows)
    except (KeyError, ValueError):
        return None, None


def _calc_prev_periods(values: List[dict]) -> dict:
    """真の前週 (月曜起点) / 前月 (カレンダー月) の高安を返す。

    ICT の PWH/PWL/PMH/PML は「前の完結した週・月」のレンジを指す。
    旧実装はローリング 5 本 / 22 本の高安を PWH/PMH と誤ラベルしており
    (現行週を混入・前月高値の取り逃し)、xauusd-smc-quant (Dukascopy) の
    カレンダー境界と乖離していた。dxy.py の _calculate_levels と同じ境界定義。

    values: 新しい順の日足 ({"datetime": "YYYY-MM-DD", "high", "low"})。
    """
    out = {"pwh": None, "pwl": None, "pmh": None, "pml": None}
    bars = []
    for v in values:
        try:
            bars.append(
                (
                    datetime.strptime(str(v["datetime"])[:10], "%Y-%m-%d").date(),
                    float(v["high"]),
                    float(v["low"]),
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    if not bars:
        return out

    today = bars[0][0]  # 最新バーの日付を基準にする

    # 前週: 今週月曜より前の 7 日間 (月〜日)
    this_monday = today - timedelta(days=today.weekday())
    prev_monday = this_monday - timedelta(days=7)
    prev_week = [(h, l) for d, h, l in bars if prev_monday <= d < this_monday]
    if prev_week:
        out["pwh"] = max(h for h, _ in prev_week)
        out["pwl"] = min(l for _, l in prev_week)

    # 前月: カレンダー前月の 1 日〜末日
    first_of_this_month = today.replace(day=1)
    last_of_prev_month = first_of_this_month - timedelta(days=1)
    first_of_prev_month = last_of_prev_month.replace(day=1)
    prev_month = [
        (h, l) for d, h, l in bars
        if first_of_prev_month <= d <= last_of_prev_month
    ]
    if prev_month:
        out["pmh"] = max(h for h, _ in prev_month)
        out["pml"] = min(l for _, l in prev_month)
    return out


def _fmt(h: Optional[float], l: Optional[float]) -> str:
    if h is None or l is None:
        return "取得不可"
    return f"{h:,.2f} / {l:,.2f}"


def _format_instrument(instrument: str, q: dict, series: List[dict]) -> List[str]:
    """1銘柄分の出力行を生成する。"""
    lines = [f"[{instrument}]"]

    if not q or not q.get("close"):
        lines.append("Twelve Data 取得不可")
        lines.append("")
        return lines

    try:
        current = float(q["close"])
        prev_close = float(q["previous_close"])
        change = float(q["change"])
        change_pct = float(q["percent_change"])
        open_ = float(q["open"])
        high_ = float(q["high"])
        low_ = float(q["low"])
    except (KeyError, ValueError, TypeError):
        lines.append("Twelve Data 取得不可（データ解析エラー）")
        lines.append("")
        return lines

    lines.append(
        f"現在値: {current:,.2f} | 前日終値: {prev_close:,.2f} | "
        f"前日比: {change:+.2f} ({change_pct:+.3f}%)"
    )
    lines.append(
        f"当日: O {open_:,.2f} / H {high_:,.2f} / L {low_:,.2f} / C {current:,.2f}"
    )

    # series[0] = 当日足, series[1] = 前日
    prev = series[1:] if len(series) >= 2 else []

    if prev:
        try:
            pdh = float(series[1]["high"])
            pdl = float(series[1]["low"])
            lines.append(f"PDH: {pdh:,.2f} / PDL: {pdl:,.2f}")
        except (IndexError, KeyError, ValueError):
            lines.append("PDH/PDL: 取得不可")
    else:
        lines.append("PDH/PDL: 取得不可")

    periods = _calc_prev_periods(series)
    wh, wl = periods["pwh"], periods["pwl"]
    mh, ml = periods["pmh"], periods["pml"]
    i20h, i20l = _calc_range(prev, 20)
    i40h, i40l = _calc_range(prev, 40)
    i60h, i60l = _calc_range(prev, 60)

    lines.append(f"PWH: {wh:,.2f} / PWL: {wl:,.2f}" if wh else "PWH/PWL: 取得不可")
    lines.append(f"PMH: {mh:,.2f} / PML: {ml:,.2f}" if mh else "PMH/PML: 取得不可")
    lines.append(f"IPDA 20日 High/Low: {_fmt(i20h, i20l)}")
    lines.append(f"IPDA 40日 High/Low: {_fmt(i40h, i40l)}")
    lines.append(f"IPDA 60日 High/Low: {_fmt(i60h, i60l)}")
    lines.append("")
    return lines


def fetch_price_data_with_raw() -> Tuple[str, dict, dict]:
    """全銘柄の価格データを取得し、テキストと検証用 raw データを返す。"""
    lines = ["=== Price Data (Twelve Data API) ===", ""]

    main_symbols = list(SYMBOL_MAP.values())  # config.yaml の twelvedata_symbol 一覧
    symbol_str = ",".join(main_symbols)

    # --- 1回目: /quote（3銘柄一括）---
    quote_raw = _get("/quote", {"symbol": symbol_str, "dp": "2"})
    quotes = _parse_quotes(quote_raw, main_symbols)

    # --- 2回目: /time_series（3銘柄一括、60本）---
    series_raw = _get(
        "/time_series",
        {"symbol": symbol_str, "interval": "1day", "outputsize": "60", "dp": "2"},
    )
    series_map = _parse_series_batch(series_raw, main_symbols)

    quotes_by_instrument = {}
    series_by_instrument = {}
    for instrument, td_symbol in SYMBOL_MAP.items():
        q = quotes.get(td_symbol, {})
        series = series_map.get(td_symbol, [])
        quotes_by_instrument[instrument] = q
        series_by_instrument[instrument] = series
        lines.extend(_format_instrument(instrument, q, series))

    # --- DXY: Twelve Data 非対応のため固定テキスト ---
    lines.append("[DXY]")
    lines.append("DXY: Twelve Data非対応。EUR/USDの逆数またはチャートで確認してください")
    lines.append("")

    return "\n".join(lines), quotes_by_instrument, series_by_instrument


def fetch_price_data() -> str:
    """全銘柄の価格データを取得してテキスト形式で返す。"""
    return fetch_price_data_with_raw()[0]


if __name__ == "__main__":
    print(fetch_price_data())
