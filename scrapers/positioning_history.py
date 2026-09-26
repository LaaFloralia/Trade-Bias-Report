"""ポジショニングの時点付き記録と、自前履歴に対する百分位

個人の売買比率は取得元ごとに母集団が違い、過去データも公開されていない。固定の
60% 基準ではなく「同じ取得元の自前履歴の中で今がどこか」で偏りを判定するため、
実行ごとに取得時刻つきで追記する（append-only JSONL）。件数が少ないうちは百分位を出さない。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

HISTORY_PATH = Path(__file__).parent.parent / "output" / "history" / "positioning.jsonl"
MIN_SAMPLES = 20


def snapshot_from_data(data: dict, recorded_at: str) -> dict:
    """collect_all_data() の結果から記録する値だけを抜き出す。"""
    retail = {}
    for symbol, d in (data.get("retail_sentiment") or {}).items():
        if isinstance(d, dict) and isinstance(d.get("long_pct"), (int, float)) and not d.get("stale"):
            retail[symbol] = {"source": d.get("source"), "long_pct": float(d["long_pct"]),
                              "source_timestamp": d.get("timestamp")}
    cot = None
    cd = (data.get("cot_disaggregated") or {}).get("data") if isinstance(data.get("cot_disaggregated"), dict) else None
    mm = (cd or {}).get("managed_money") or {}
    if isinstance(mm.get("net_pct_oi"), (int, float)):
        cot = {"report_date": cd.get("date"), "mm_net_pct_oi": float(mm["net_pct_oi"])}
    return {"recorded_at": recorded_at, "retail": retail, "cot_gold_mm": cot}


def append_snapshot(snapshot: dict, path: Path = HISTORY_PATH) -> bool:
    if not snapshot.get("retail") and not snapshot.get("cot_gold_mm"):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
    return True


def load_history(path: Path = HISTORY_PATH) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def percentile_rank(history: Iterable[float], value: float) -> Optional[float]:
    """value 以下の割合（%）。履歴が MIN_SAMPLES 未満なら None。"""
    values = [v for v in history if isinstance(v, (int, float))]
    if len(values) < MIN_SAMPLES:
        return None
    return round(100 * sum(1 for v in values if v <= value) / len(values), 1)


def _retail_series(rows: list[dict], symbol: str, source: str, before: str) -> list[float]:
    # 同じ取得元の値だけ、1時間に1件へまとめる（同一データの重複記録で分布を歪めない）
    by_hour = {}
    for row in rows:
        r = (row.get("retail") or {}).get(symbol)
        if r and r.get("source") == source and row.get("recorded_at", "") < before:
            by_hour[row["recorded_at"][:13]] = r["long_pct"]
    return list(by_hour.values())


def _cot_series(rows: list[dict], before_report: str) -> list[float]:
    return list(_cot_by_week(rows, before_report).values())


def _cot_by_week(rows: list[dict], before_report: str) -> dict:
    by_week = {}
    for row in rows:
        c = row.get("cot_gold_mm")
        if c and c.get("report_date") and c["report_date"] < before_report:
            by_week[c["report_date"]] = c["mm_net_pct_oi"]
    return dict(sorted(by_week.items()))


def format_positioning_lines(snapshot: dict, rows: list[dict]) -> list[str]:
    lines = ["### ポジショニング履歴（自前記録の百分位）"]
    now = snapshot.get("recorded_at", "")
    for symbol, r in (snapshot.get("retail") or {}).items():
        series = _retail_series(rows, symbol, r["source"], now)
        pct = percentile_rank(series, r["long_pct"])
        rank = f"{pct}パーセンタイル" if pct is not None else f"判定保留（履歴{len(series)}件、{MIN_SAMPLES}件未満）"
        lines.append(f"- {symbol} 個人ロング比率（{r['source']}）: {r['long_pct']}% → 同じ取得元の履歴で {rank}")
    cot = snapshot.get("cot_gold_mm")
    if cot:
        weeks = _cot_by_week(rows, cot["report_date"])
        series = list(weeks.values())
        pct = percentile_rank(series, cot["mm_net_pct_oi"])
        window = f"比較窓 {min(weeks)}〜{max(weeks)} の{len(weeks)}週、CFTC現在公表値" if weeks else "比較窓なし"
        rank = (f"{pct}パーセンタイル（{window}）" if pct is not None
                else f"判定保留（履歴{len(series)}週、{MIN_SAMPLES}週未満）")
        lines.append(f"- 金 Managed Money 純ロング/OI（{cot['report_date']}時点）: {cot['mm_net_pct_oi']}% → {rank}")
    if len(lines) == 1:
        lines.append("- 取得不可（比率・COT とも今回値なし）")
    lines.append("※ 90以上・10以下だけを偏りの極端値として扱う。取得元が違う値は比べない")
    return lines


def backfill_from_scraped(files: Iterable[Path], path: Path = HISTORY_PATH) -> int:
    """過去の scraped_data_*.json から記録を作る（取得時刻は各ファイルの timestamp）。"""
    existing = {row.get("recorded_at") for row in load_history(path)}
    added = 0
    for file in sorted(files):
        try:
            data = json.loads(Path(file).read_text(encoding="utf-8"))
            stamp = datetime.fromisoformat(str(data["timestamp"])).isoformat()
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if stamp in existing:
            continue
        snap = snapshot_from_data(data, stamp)
        snap["backfill_from"] = Path(file).name
        if append_snapshot(snap, path):
            existing.add(stamp)
            added += 1
    return added


def backfill_cot_history(market_name: str, weeks: int = 156, path: Path = HISTORY_PATH) -> int:
    """CFTC Disaggregated の過去週を COT 百分位用に記録する（現在の公表値。後日訂正は反映済みの値）。"""
    import requests
    from scrapers.cot_disaggregated import BASE_URL, FIELDS, _parse_row

    params = {"$where": f"market_and_exchange_names='{market_name}'",
              "$order": "report_date_as_yyyy_mm_dd DESC", "$limit": str(weeks), "$select": ",".join(FIELDS)}
    resp = requests.get(BASE_URL, params=params, timeout=30)
    resp.raise_for_status()
    known = {(row.get("cot_gold_mm") or {}).get("report_date") for row in load_history(path)}
    added = 0
    for raw in reversed(resp.json()):
        parsed = _parse_row(raw)
        pct = (parsed.get("managed_money") or {}).get("net_pct_oi")
        if parsed.get("date") in known or not isinstance(pct, (int, float)):
            continue
        append_snapshot({"recorded_at": f"{parsed['date']}T00:00:00", "retail": {},
                         "cot_gold_mm": {"report_date": parsed["date"], "mm_net_pct_oi": float(pct)},
                         "backfill_from": "cftc_disaggregated_api"}, path)
        known.add(parsed["date"])
        added += 1
    return added
