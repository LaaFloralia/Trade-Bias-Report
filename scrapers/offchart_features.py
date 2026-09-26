"""チャート外の特徴量を、LLM を通さずにコードだけで機械可読の JSON にする

裁量レポートの本文とは別に、自動売買（Argus 等）や後日の検証が同じ値を時点付きで
読めるようにする。値はすべて収集時点で得られたもので、判断・方向の決定は含めない。

- event_blackouts: 高重要度指標の前後の取引停止候補（発表30分前〜60分後、FOMC 等は120分後）
- expected_move: GVZ からの参考変動額（252営業日・365暦日換算）
- round_levels / positioning / surprises / fedwatch / rates / correlation / flows / news / input_status

保存先: output/offchart_features_latest.json と output/history/offchart_features.jsonl（追記）
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from scrapers.macro_surprise import event_datetime_jst
from scrapers.positioning_history import load_history, positioning_numbers

OUTPUT = Path(__file__).parent.parent / "output"
SCHEMA_VERSION = 1
LONG_WINDOW_WORDS = ("interest rate decision", "press conference", "fomc", "monetary policy statement")


def _blackouts(events: list, now: datetime, horizon_hours: int) -> list[dict]:
    out = []
    for ev in events or []:
        when = event_datetime_jst(ev)
        if when is None or not (now - timedelta(hours=2) <= when <= now + timedelta(hours=horizon_hours)):
            continue
        name = str(ev.get("indicator", ""))
        after = 120 if any(w in name.lower() for w in LONG_WINDOW_WORDS) else 60
        out.append({"event": name, "country": ev.get("country"), "time_jst": when.isoformat(),
                    "window_start": (when - timedelta(minutes=30)).isoformat(),
                    "window_end": (when + timedelta(minutes=after)).isoformat(),
                    "forecast": ev.get("forecast"), "previous": ev.get("previous")})
    return sorted(out, key=lambda e: e["time_jst"])


def _fred(results: dict, sid: str) -> Optional[dict]:
    d = (results.get("fred") or {}).get(sid) or {}
    if not isinstance(d.get("value"), (int, float)):
        return None
    return {"value": d["value"], "change": d.get("change"), "change_20obs": d.get("change_20obs"),
            "as_of": d.get("as_of_date"), "stale": bool(d.get("stale"))}


def build_features(results: dict, now: datetime, weekly: bool = False) -> dict:
    calendar = results.get("economic_calendar") or {}
    liq = results.get("liquidity_levels") or {}
    pos = results.get("positioning") or {}
    etf = results.get("gold_etf") or {}
    news = results.get("news_headlines") or {}
    fed = results.get("fedwatch") or {}
    try:
        positioning = positioning_numbers(pos["snapshot"], load_history()) if pos.get("snapshot") else None
    except Exception:  # noqa: BLE001 — 特徴量の一部が欠けても全体は保存する
        positioning = None
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "weekly" if weekly else "daily",
        "generated_at": now.isoformat(),
        "collection_started_at": str(results.get("timestamp")),
        "event_blackouts": _blackouts(calendar.get("events") or [], now, 7 * 24 if weekly else 36),
        "expected_move": {k: liq.get(k) for k in ("price", "gvz", "gvz_as_of", "gvz_stale",
                                                  "expected_move_1sd", "expected_move_calendar")},
        "round_levels": [lv["level"] for lv in liq.get("levels") or []],
        "positioning": positioning,
        "surprises": [{"indicator": e.get("indicator"), "released_at": e.get("released_at_jst"),
                       "actual": e.get("actual"), "forecast": e.get("forecast"),
                       "pre_release_forecast": str(e.get("forecast_provenance", "")).startswith("発表前記録"),
                       **{k: (e.get("surprise") or {}).get(k) for k in ("diff", "unit", "gold_direction")}}
                      for e in results.get("macro_surprises") or []],
        "fedwatch": {"target_rates": fed.get("target_rates"), "next_meeting": fed.get("meeting_date")},
        "rates": {sid: _fred(results, sid) for sid in ("DFII10", "DGS2", "DGS10", "DTWEXBGS")},
        "correlation": [{k: p.get(k) for k in ("pair", "r_20d", "r_60d", "verdict", "period_20d")}
                        for p in (results.get("correlation") or {}).get("pairs") or []],
        "flows": {"gld_tonnes": etf.get("tonnes"), "gld_change_5d_t": etf.get("change_5d_t"),
                  "gld_change_20d_t": etf.get("change_20d_t"), "as_of": etf.get("as_of_date")},
        "news": {"candidates": news.get("candidate_count"), "kept": len(news.get("kept") or []),
                 "selection": (news.get("selection") or {}).get("mode"),
                 "top": [{k: c.get(k) for k in ("title", "url", "published", "p")} for c in (news.get("kept") or [])[:5]]},
        "input_status": results.get("input_status"),
    }


def save_features(features: dict, output: Path = OUTPUT) -> None:
    (output / "history").mkdir(parents=True, exist_ok=True)
    text = json.dumps(features, ensure_ascii=False, default=str)
    (output / "offchart_features_latest.json").write_text(text + "\n", encoding="utf-8")
    with (output / "history" / "offchart_features.jsonl").open("a", encoding="utf-8") as f:
        f.write(text + "\n")
