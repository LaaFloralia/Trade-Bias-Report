"""相関レジームの定量化（ローリング相関係数）

従来のセクション6 は「乖離あり/なし」の定性判断のみで、相関の強さが数値化されていなかった。
DXY-XAU の相関が -0.8 なのか -0.2 なのかで、スコア #1（DXY バイアス整合）に置くべき
重みは変わる。本モジュールは既存データから決定論的に相関係数を計算する。

方法論:
  - **リターン（変化率）ベース**で計算する。価格水準そのものの相関は見せかけ相関
    （spurious correlation）になるため使わない。金利系は差分（bp 変化）を使う。
  - 20 日（短期レジーム）と 60 日（基準レジーム）の 2 本を出し、乖離を検出する。
  - 通常相関の符号と現在の符号・強度を比較して 3 値判定:
      正常 / 弱化（|r| が基準を大きく下回る）/ 反転（符号が逆）

入力（すべて取得済みデータの再利用。ネットワーク呼び出しなし）:
  - XAUUSD 日足終値: xauusd-smc-quant の live-h1.csv を日足集約
  - 実質金利 / 名目金利 / Broad USD: FRED の observations（fred.py が保持）
"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Optional

SHORT_WINDOW = 20
LONG_WINDOW = 60

# (表示名, FRED series_id, 期待される相関の符号, 系列の差分の取り方)
#   diff: "pct" = 変化率 / "abs" = 差分（金利は水準差 = bp 変化を使う）
PAIRS = [
    ("XAUUSD vs 実質金利 (DFII10)", "DFII10", -1, "abs"),
    ("XAUUSD vs 名目金利 (DGS10)", "DGS10", -1, "abs"),
    ("XAUUSD vs Broad USD (DTWEXBGS)", "DTWEXBGS", -1, "pct"),
]

# |r| がこの値未満なら「相関が効いていない」と判定する
WEAK_THRESHOLD = 0.3


def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def daily_closes_from_h1(h1_path: Optional[Path], max_days: int = 120) -> dict[str, float]:
    """H1 CSV を日足終値（UTC 日付キー）に集約する。"""
    if not h1_path or not Path(h1_path).exists():
        return {}
    import csv
    from datetime import timezone

    closes: dict[str, float] = {}
    try:
        with open(h1_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                ts = datetime.fromtimestamp(int(row["timestamp"]) / 1000, tz=timezone.utc)
                closes[ts.date().isoformat()] = float(row["close"])
    except (KeyError, ValueError, OSError):
        return {}
    keys = sorted(closes)[-max_days:]
    return {k: closes[k] for k in keys}


def _series_from_fred(fred: dict, series_id: str) -> dict[str, float]:
    """FRED の observations を {date: value} に変換する。"""
    entry = (fred or {}).get(series_id) or {}
    obs = entry.get("observations") or []
    out = {}
    for item in obs:
        try:
            date, value = item[0], float(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if date:
            out[date] = value
    return out


def _changes(series: dict[str, float], dates: list[str], mode: str) -> list[float]:
    """連続する日付ペアの変化を返す（mode: pct = 変化率 / abs = 差分）。"""
    out = []
    for prev_d, cur_d in zip(dates[:-1], dates[1:]):
        prev_v, cur_v = series[prev_d], series[cur_d]
        if mode == "pct":
            if prev_v == 0:
                out.append(0.0)
            else:
                out.append((cur_v - prev_v) / prev_v * 100)
        else:
            out.append(cur_v - prev_v)
    return out


def _classify(r_short: Optional[float], r_long: Optional[float], expected_sign: int) -> str:
    if r_short is None:
        return "判定不能"
    if abs(r_short) < WEAK_THRESHOLD:
        return "無相関化（相関が効いていない）"
    if (r_short > 0) != (expected_sign > 0):
        return "反転（通常と逆符号）"
    if r_long is not None and abs(r_short) < abs(r_long) * 0.6:
        return "弱化（基準レジームより大幅に低下）"
    return "正常"


def build_correlations(xau_closes: dict[str, float], fred: dict) -> dict:
    """ペアごとの 20日/60日ローリング相関を計算する。"""
    result = {"pairs": [], "error": None}
    if not xau_closes:
        result["error"] = "XAUUSD 日足データなし（相関計算スキップ）"
        return result

    for label, series_id, expected_sign, mode in PAIRS:
        other = _series_from_fred(fred, series_id)
        entry = {"pair": label, "series_id": series_id,
                 "r_20d": None, "r_60d": None, "n": 0, "verdict": "判定不能"}
        if not other:
            entry["verdict"] = "判定不能（FRED 観測列なし）"
            result["pairs"].append(entry)
            continue

        # 両系列に存在する日付のみで揃える（FRED は営業日ベース・祝日欠損あり）
        common = sorted(set(xau_closes) & set(other))
        if len(common) < SHORT_WINDOW + 1:
            entry["verdict"] = f"判定不能（共通日数 {len(common)} 本）"
            result["pairs"].append(entry)
            continue

        xau_chg = _changes(xau_closes, common, "pct")
        oth_chg = _changes(other, common, mode)
        entry["n"] = len(xau_chg)
        entry["r_20d"] = _pearson(xau_chg[-SHORT_WINDOW:], oth_chg[-SHORT_WINDOW:])
        if len(xau_chg) >= LONG_WINDOW:
            entry["r_60d"] = _pearson(xau_chg[-LONG_WINDOW:], oth_chg[-LONG_WINDOW:])
        entry["verdict"] = _classify(entry["r_20d"], entry["r_60d"], expected_sign)
        for k in ("r_20d", "r_60d"):
            if entry[k] is not None:
                entry[k] = round(entry[k], 2)
        result["pairs"].append(entry)
    return result


def format_correlation_lines(corr: dict) -> list[str]:
    """main.format_scraped_data 用の整形行。"""
    lines = ["### 相関レジーム定量（ローリング相関係数、日次リターンベース）"]
    if not isinstance(corr, dict) or corr.get("error"):
        lines.append(f"- 取得不可（{(corr or {}).get('error', '不明')}）")
        return lines
    if not corr.get("pairs"):
        lines.append("- 算出対象なし")
        return lines

    lines.append("| ペア | 20日 r | 60日 r | 判定 | 標本 |")
    lines.append("|---|---|---|---|---|")
    for p in corr["pairs"]:
        r20 = f"{p['r_20d']:+.2f}" if p["r_20d"] is not None else "N/A"
        r60 = f"{p['r_60d']:+.2f}" if p["r_60d"] is not None else "N/A"
        lines.append(f"| {p['pair']} | {r20} | {r60} | {p['verdict']} | {p['n']}本 |")
    lines.append(
        "- 読み方: 水準ではなく日次リターンの相関。|r|<0.3 は「その相関に依拠した判断をしない」"
        "の意。反転・無相関化が出ているペアは、そのドライバーを根拠から外すか重みを下げる。"
    )
    return lines
