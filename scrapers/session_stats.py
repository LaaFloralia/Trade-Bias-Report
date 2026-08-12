"""セッション統計 — ICT の時間概念を実データの頻度で裏付ける

チャートを睨んでも分からない種類の情報。「アジアレンジは London で刈られやすい」という
ICT の一般論を、XAUUSD の実データで「直近 N 日で何%」と数値化する。
社長がチャート上で Judas swing を待つとき、その待ちが統計的に妥当かを判断する材料になる。

旧「季節性」セクションは LLM の記憶頼み（ハルシネーション源）だったため 2026-08-11 に
全廃したが、本モジュールは Dukascopy H1（検証済みパイプライン）からの決定論計算であり、
その問題を持たない。

計算する統計（すべて UTC ベースの H1 から集約）:
  1. アジアレンジ（UTC 00:00-06:59）の高値/安値が London（UTC 07:00-11:59）で刈られた比率
  2. 刈った後に反転した比率（= Judas swing 的挙動の発生率）
  3. PDH / PDL が当日中に刈られた比率
  4. 曜日別の日中レンジ（ATR 比）

セッション定義（JST 併記）:
  アジア   UTC 00-07 = JST 09-16
  London  UTC 07-12 = JST 16-21（London killzone）
  NY      UTC 12-17 = JST 21-02（NY killzone）
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

LOOKBACK_DAYS = 250          # 統計の標本期間（約 1 年）
ASIA_HOURS = range(0, 7)     # UTC
LONDON_HOURS = range(7, 12)
NY_HOURS = range(12, 17)
# 刈り取り後の「反転」判定: セッション終値がレンジ内へ戻ったか
WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]


def _load_daily_sessions(h1_path: Path, lookback: int = LOOKBACK_DAYS) -> list[dict]:
    """H1 CSV を日付ごとのセッション別 OHLC に集約する。"""
    days: dict[str, dict] = {}
    try:
        with open(h1_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                ts = datetime.fromtimestamp(int(row["timestamp"]) / 1000, tz=timezone.utc)
                key = ts.date().isoformat()
                d = days.setdefault(key, {
                    "date": key, "weekday": ts.weekday(),
                    "high": None, "low": None, "close": None,
                    "asia": {"high": None, "low": None},
                    "london": {"high": None, "low": None, "close": None},
                    "ny": {"high": None, "low": None, "close": None},
                })
                hi, lo, cl = float(row["high"]), float(row["low"]), float(row["close"])
                d["high"] = hi if d["high"] is None else max(d["high"], hi)
                d["low"] = lo if d["low"] is None else min(d["low"], lo)
                d["close"] = cl
                for name, hours in (("asia", ASIA_HOURS), ("london", LONDON_HOURS), ("ny", NY_HOURS)):
                    if ts.hour in hours:
                        s = d[name]
                        s["high"] = hi if s["high"] is None else max(s["high"], hi)
                        s["low"] = lo if s["low"] is None else min(s["low"], lo)
                        if "close" in s:
                            s["close"] = cl
    except (KeyError, ValueError, OSError):
        return []

    ordered = [days[k] for k in sorted(days)]
    # 週末の断片日（土日でバー数が極端に少ない日）は除外
    ordered = [d for d in ordered if d["weekday"] < 5 and d["high"] is not None]
    return ordered[-lookback:]


def _pct(n: int, total: int) -> Optional[float]:
    return round(n / total * 100, 1) if total else None


def compute_session_stats(h1_path: Optional[Path], lookback: int = LOOKBACK_DAYS) -> dict:
    """セッション統計を計算する。"""
    result = {"sample_days": 0, "asia_sweep": {}, "pd_sweep": {}, "weekday_range": [], "error": None}
    if not h1_path or not Path(h1_path).exists():
        result["error"] = "H1 データなし（セッション統計スキップ）"
        return result

    days = _load_daily_sessions(Path(h1_path), lookback)
    if len(days) < 30:
        result["error"] = f"標本不足（{len(days)}日、30日未満）"
        return result
    result["sample_days"] = len(days)

    # --- 1&2. アジアレンジのスイープと反転（London セッション） ---
    n = up = down = both = neither = 0
    rev_up = rev_down = 0
    for d in days:
        a, l = d["asia"], d["london"]
        if a["high"] is None or l["high"] is None or l["close"] is None:
            continue
        n += 1
        swept_up = l["high"] > a["high"]
        swept_down = l["low"] < a["low"]
        if swept_up and swept_down:
            both += 1
        elif swept_up:
            up += 1
        elif swept_down:
            down += 1
        else:
            neither += 1
        # 反転 = 刈った後、London 終値がアジアレンジ内へ戻った（Judas swing 的挙動）
        if swept_up and l["close"] < a["high"]:
            rev_up += 1
        if swept_down and l["close"] > a["low"]:
            rev_down += 1

    result["asia_sweep"] = {
        "n": n,
        "up_only_pct": _pct(up, n), "down_only_pct": _pct(down, n),
        "both_pct": _pct(both, n), "none_pct": _pct(neither, n),
        "any_pct": _pct(up + down + both, n),
        # 上抜けした日のうち、London 終値がレンジ内へ戻った比率
        "reversal_after_up_pct": _pct(rev_up, up + both),
        "reversal_after_down_pct": _pct(rev_down, down + both),
    }

    # --- 3. PDH / PDL が当日中に刈られた比率 ---
    n2 = pdh_hit = pdl_hit = both_hit = 0
    for prev, cur in zip(days[:-1], days[1:]):
        if prev["high"] is None or cur["high"] is None:
            continue
        n2 += 1
        h = cur["high"] > prev["high"]
        l = cur["low"] < prev["low"]
        if h and l:
            both_hit += 1
        elif h:
            pdh_hit += 1
        elif l:
            pdl_hit += 1
    result["pd_sweep"] = {
        "n": n2,
        "pdh_only_pct": _pct(pdh_hit, n2), "pdl_only_pct": _pct(pdl_hit, n2),
        "both_pct": _pct(both_hit, n2),
        "any_pct": _pct(pdh_hit + pdl_hit + both_hit, n2),
    }

    # --- 4. 曜日別の日中レンジ（全体平均比） ---
    ranges: dict[int, list[float]] = {}
    for d in days:
        ranges.setdefault(d["weekday"], []).append(d["high"] - d["low"])
    all_avg = sum(sum(v) for v in ranges.values()) / max(sum(len(v) for v in ranges.values()), 1)
    for wd in sorted(ranges):
        vals = ranges[wd]
        avg = sum(vals) / len(vals)
        result["weekday_range"].append({
            "weekday": WEEKDAY_JA[wd],
            "avg_range": round(avg, 1),
            "vs_all_pct": round((avg / all_avg - 1) * 100, 1) if all_avg else None,
            "n": len(vals),
        })
    return result


def format_session_stats_lines(st: dict) -> list[str]:
    """main.format_scraped_data 用の整形行。"""
    lines = ["### セッション統計（XAUUSD 実データ、ICT 時間概念の頻度裏付け）"]
    if not isinstance(st, dict) or st.get("error"):
        lines.append(f"- 取得不可（{(st or {}).get('error', '不明')}）")
        return lines

    a = st.get("asia_sweep") or {}
    if a.get("n"):
        lines.append(
            f"- アジアレンジ（JST 09-16）の London（JST 16-21）でのスイープ率 "
            f"[標本 {a['n']}日]: いずれか {a['any_pct']}%"
            f"（上のみ {a['up_only_pct']}% / 下のみ {a['down_only_pct']}% / "
            f"両側 {a['both_pct']}% / なし {a['none_pct']}%）"
        )
        lines.append(
            f"  刈った後に London 終値がレンジ内へ戻った比率（Judas swing 的挙動）: "
            f"上抜け後 {a['reversal_after_up_pct']}% / 下抜け後 {a['reversal_after_down_pct']}%"
        )
    p = st.get("pd_sweep") or {}
    if p.get("n"):
        lines.append(
            f"- PDH/PDL の当日スイープ率 [標本 {p['n']}日]: いずれか {p['any_pct']}%"
            f"（PDH のみ {p['pdh_only_pct']}% / PDL のみ {p['pdl_only_pct']}% / 両側 {p['both_pct']}%）"
        )
    wr = st.get("weekday_range") or []
    if wr:
        detail = " / ".join(f"{w['weekday']} {w['vs_all_pct']:+.0f}%" for w in wr)
        lines.append(f"- 曜日別の日中レンジ（全体平均比）: {detail}")
    lines.append(
        f"- 用途: 待ちの妥当性判断。例えばスイープ率が高い日にレンジ内エントリーを急ぐのは分が悪い。"
        f"（標本 {st.get('sample_days')}営業日、Dukascopy H1・UTC 集計）"
    )
    return lines
