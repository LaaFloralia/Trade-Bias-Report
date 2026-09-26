"""チャート外の流動性・値幅の目安（キリ番と金の予想変動率）

損切り注文はキリ番のすぐ外、利食い注文はキリ番ちょうどに集まりやすく、
損切りが集まる水準を越えると値動きが加速する（Osler 2003, 2005。効果は数時間単位）。
現在値の上下にある 50/100 ドル刻みの水準を距離つきで列挙する。

想定値幅は CBOE Gold ETF Volatility Index（GVZ、FRED GVZCLS）の年率予想変動率から
1日の1標準偏差（現在値 × GVZ / 100 / √252）として示す。市場が織り込む平均的な幅であり、
方向の予測ではない。

FRED の GVZCLS は数営業日遅れることがある。定期実行の親が収集前に TradingView 公式MCPで
CBOE:GVZ を取得していれば（output/history/tv_snapshots.jsonl、3時間以内）、日付の新しい方を使う。
Cboe 公式の履歴CSVは、規約が電子的な保存に事前同意を求めるため自動取得しない。

CME 金オプションの行使価格別建玉は、CME の利用規約が自動取得を禁止しているため取得しない。
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

TRADING_DAYS = 252
TV_HISTORY = Path(__file__).parent.parent / "output" / "history" / "tv_snapshots.jsonl"
TV_MAX_AGE = timedelta(hours=3)
TV_SOURCE = "TradingView CBOE:GVZ（親がMCPで取得）"


def latest_tv_gvz(now: datetime, path: Path = TV_HISTORY, max_age: timedelta = TV_MAX_AGE) -> Optional[dict]:
    """親が保存した直近の TradingView 取得記録から GVZ 日足を返す。古い・壊れた記録は使わない。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-20:]
    except OSError:
        return None
    for line in reversed(lines):
        try:
            row = json.loads(line)
            taken = datetime.fromisoformat(str(row["retrieved_at"]).replace("Z", "+00:00"))
            g = row.get("gvz_latest") or {}
            value, day = g.get("close"), str(g.get("date") or "")
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if taken.tzinfo is None or not timedelta(0) <= now - taken <= max_age:
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0 \
                and len(day) == 10:
            return {"value": float(value), "as_of_date": day, "source": TV_SOURCE, "retrieved_at": row["retrieved_at"]}
        return None
    return None


def choose_gvz(fred: Optional[dict], tv: Optional[dict]) -> dict:
    """日付の新しい GVZ を採る。TV を採ったときは FRED の値と日付も残す。"""
    fred = dict(fred or {})
    fred.setdefault("source", "FRED GVZCLS")
    fred_ok = isinstance(fred.get("value"), (int, float)) and not fred.get("error")
    if tv and (not fred_ok or str(tv["as_of_date"]) > str(fred.get("as_of_date") or "")):
        return dict(tv, stale=False, fred_value=fred.get("value"), fred_as_of=fred.get("as_of_date"))
    return fred


def round_levels(price: float, band_pct: float = 2.0, step: float = 50.0) -> list[dict]:
    """現在値 ±band_pct% 内の step 刻みの水準（100 刻みを major とする）。"""
    if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
        return []
    low, high = price * (1 - band_pct / 100), price * (1 + band_pct / 100)
    level = math.ceil(low / step) * step
    out = []
    while level <= high:
        out.append({"level": level, "major": level % (step * 2) == 0,
                    "distance": round(level - price, 2), "distance_pct": round((level - price) / price * 100, 2)})
        level += step
    return out


def expected_daily_move(price: float, gvz: float) -> Optional[float]:
    """GVZ（年率%）から1日の1標準偏差の値幅（ドル）。"""
    if not (isinstance(price, (int, float)) and isinstance(gvz, (int, float))):
        return None
    if not (math.isfinite(price) and math.isfinite(gvz)) or price <= 0 or gvz <= 0:
        return None
    return price * gvz / 100 / math.sqrt(TRADING_DAYS)


def build_liquidity(price: Optional[float], gvz: Optional[dict] = None) -> dict:
    """収集時点の現在値と GVZ から、保存・再整形できる形で結果を作る。"""
    gvz = gvz or {}
    value = gvz.get("value") if isinstance(gvz.get("value"), (int, float)) else None
    move = expected_daily_move(price, value) if value is not None else None
    calendar_move = price * value / 100 / math.sqrt(365) if move else None
    levels = round_levels(price)
    above = [lv["level"] for lv in levels if lv["distance"] > 0]
    below = [lv["level"] for lv in levels if lv["distance"] <= 0]
    return {"price": price, "levels": levels,
            "band_upper": round(price + move, 2) if move else None,
            "band_lower": round(price - move, 2) if move else None,
            "nearest_above": min(above) if above else None,
            "nearest_below": max(below) if below else None,
            "expected_move_calendar": round(calendar_move, 1) if calendar_move else None,
            "gvz": value, "gvz_as_of": gvz.get("as_of_date"), "gvz_stale": bool(gvz.get("stale")),
            "gvz_error": gvz.get("error"), "gvz_source": gvz.get("source") or "FRED GVZCLS",
            "gvz_fred": gvz.get("fred_value"), "gvz_fred_as_of": gvz.get("fred_as_of"),
            "expected_move_1sd": round(move, 1) if move else None}


def format_liquidity_lines(liq: Optional[dict]) -> list[str]:
    lines = ["### 流動性の目安（キリ番・想定値幅）"]
    liq = liq or {}
    price, levels, move = liq.get("price"), liq.get("levels") or [], liq.get("expected_move_1sd")
    lines.append("- 観測した注文集中（価格帯別の注文量）: 取得不可（無料で規約上自動取得できる金の全市場データなし）")
    if move:
        cal = liq.get("expected_move_calendar")
        lines.append(f"- 参考変動額（1日・1標準偏差）: ±{move:,.1f}ドル → {price - move:,.1f}〜{price + move:,.1f}"
                     f"（GVZ {liq['gvz']:.2f}、{liq.get('gvz_as_of')}時点・{liq.get('gvz_source') or 'FRED GVZCLS'}"
                     f"{'・古い値' if liq.get('gvz_stale') else ''}"
                     + (f"。FRED GVZCLS は {liq['gvz_fred']:.2f}（{liq.get('gvz_fred_as_of')}時点）" if isinstance(liq.get('gvz_fred'), (int, float)) else "")
                     + "。"
                     "GLDオプション由来の30日予想変動率を252営業日で日次換算。"
                     + (f"暦日365日換算なら±{cal:,.1f}ドル。" if isinstance(cal, (int, float)) else "") +
                     "到達範囲や日中高安幅の予測ではない）")
    else:
        lines.append(f"- 参考変動額: 取得不可（GVZ {liq.get('gvz_error') or '値なし'}）")
    if not levels:
        lines.append("- キリ番: 取得不可（現在値なし）")
    else:
        above = [lv for lv in levels if lv["distance"] > 0]
        below = [lv for lv in reversed(levels) if lv["distance"] <= 0]

        def text(lv):
            inside = "・参考変動額内" if move and abs(lv["distance"]) <= move else ""
            return f"{lv['level']:,.0f}{'★' if lv['major'] else ''}（{lv['distance']:+,.1f}, {lv['distance_pct']:+.2f}%{inside}）"

        for label, group in (("上", above), ("下", below)):
            lines.append(f"- {label}のキリ番: {' / '.join(text(lv) for lv in group[:4]) or 'なし（±2%内）'}")
        lines.append("  参考キリ番（★=100ドル刻み）。為替では損切りが水準のすぐ外、利食いが水準ちょうどに集まりやすい"
                     "（Osler 2003/2005、数時間単位）。金での有効性・注文量は未検証")
    lines.append("- CME金オプションの行使価格別建玉: 自動取得しない（CMEの利用規約が自動取得を禁止）。満期週は社長がQuikStrikeで目視確認")
    return lines
