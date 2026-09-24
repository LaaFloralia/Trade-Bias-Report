"""チャート外の流動性の目安（キリ番）

損切り注文はキリ番のすぐ外、利食い注文はキリ番ちょうどに集まりやすく、
損切りが集まる水準を越えると値動きが加速する（Osler 2003, 2005。効果は数時間単位）。
現在値の上下にある 50/100 ドル刻みの水準を距離つきで列挙する。

CME 金オプションの行使価格別建玉は QuikStrike の利用条件を確認するまで取得しない。
"""

from __future__ import annotations

import math
from typing import Optional


def round_levels(price: float, band_pct: float = 2.0, step: float = 50.0) -> list[dict]:
    """現在値 ±band_pct% 内の step 刻みの水準（100 刻みを major とする）。"""
    if not math.isfinite(price) or price <= 0:
        return []
    low, high = price * (1 - band_pct / 100), price * (1 + band_pct / 100)
    level = math.ceil(low / step) * step
    out = []
    while level <= high:
        out.append({"level": level, "major": level % (step * 2) == 0,
                    "distance": round(level - price, 2), "distance_pct": round((level - price) / price * 100, 2)})
        level += step
    return out


def format_liquidity_lines(price: Optional[float]) -> list[str]:
    lines = ["### 流動性の目安（キリ番・オプション建玉）"]
    levels = round_levels(price) if isinstance(price, (int, float)) else []
    if not levels:
        lines.append("- キリ番: 取得不可（現在値なし）")
    else:
        above = [lv for lv in levels if lv["distance"] > 0]
        below = [lv for lv in reversed(levels) if lv["distance"] <= 0]
        for label, group in (("上", above), ("下", below)):
            text = " / ".join(f"{lv['level']:,.0f}{'★' if lv['major'] else ''}（{lv['distance']:+,.1f}, {lv['distance_pct']:+.2f}%）"
                              for lv in group[:4])
            lines.append(f"- {label}のキリ番: {text or 'なし（±2%内）'}")
        lines.append("  ★=100ドル刻み。損切りは水準のすぐ外、利食いは水準ちょうどに集まりやすい（Osler 2003/2005、数時間単位の効果）")
    lines.append("- CME金オプションの行使価格別建玉: 未取得（QuikStrike の利用条件確認待ち）")
    return lines
