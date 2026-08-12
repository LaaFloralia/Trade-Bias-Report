"""リテール分析モジュール — Retail P/L 構造・リクイディティプール・スイープ検証

チャート外分析の中核となる派生計算（純計算・ネットワークなし・stdlib のみ）:

  1. P/L 構造: リテールの Long/Short 比率と平均エントリー価格を現値と突き合わせ、
     どちら側が含み損（= 損切り燃料）でどちら側が含み益（= 利確圧力）かを判定する。
     含み損側の SL がリクイディティプールになる（Short の SL = BSL / Long の SL = SSL）。
  2. リクイディティプール: MyFXBook Order Book のクラスタ（妥当性フィルタ済み）に
     現値からの距離を付与し、ボリューム順で提示する。
  3. スイープ検証: 前日に保存したプールに対して直近 24h の H1 価格が
     「プールを刈り取った後に反転したか」を判定する。
     判定: sweep→reversal / sweep→continuation / no sweep
     反転閾値: 0.5 × ATR20d（プール到達後の戻り幅）

プール履歴は output/history/liquidity_pools.json に日付キーで保存し、
翌日の答え合わせ（前日プール vs 実際の値動き）に使う。
H1 データは xauusd-smc-quant の data/live-h1.csv（Dukascopy、UTC、ms epoch）を読む。
"""

from __future__ import annotations

import csv
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

POOLS_HISTORY_PATH = Path(__file__).parent.parent / "output" / "history" / "liquidity_pools.json"
POOLS_RETENTION_DAYS = 60
BASELINE_WINDOW_DAYS = (1, 4)   # 前日プール探索: 1〜4 日前（週末耐性）
SWEEP_WINDOW_HOURS = 24         # スイープ判定対象の直近時間幅
REVERSAL_ATR_MULT = 0.5         # 反転確認の閾値（ATR20d 倍率）
H1_STALE_HOURS = 36             # H1 最終バーがこれより古ければ参考扱い


# ---------------------------------------------------------------- H1 / ATR

def load_h1_bars(h1_path: Path, max_rows: int = 800) -> list[dict]:
    """live-h1.csv の末尾 max_rows 本を読み込む。

    形式: timestamp(ms epoch),open,high,low,close
    """
    if not h1_path or not Path(h1_path).exists():
        return []
    bars = []
    try:
        with open(h1_path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        for row in rows[-max_rows:]:
            bars.append({
                "ts": datetime.fromtimestamp(int(row["timestamp"]) / 1000, tz=timezone.utc),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            })
    except (KeyError, ValueError, OSError):
        return []
    return bars


def compute_atr20d(bars: list[dict]) -> Optional[float]:
    """H1 バーを日足に集約し ATR20d を返す。最終日（部分足の可能性）は除外。"""
    if not bars:
        return None
    days: dict[str, dict] = {}
    for b in bars:
        key = b["ts"].date().isoformat()
        d = days.setdefault(key, {"high": b["high"], "low": b["low"], "close": b["close"]})
        d["high"] = max(d["high"], b["high"])
        d["low"] = min(d["low"], b["low"])
        d["close"] = b["close"]  # 最後のバーの close が日足 close

    keys = sorted(days)
    if len(keys) < 22:
        return None
    complete = keys[:-1]  # 最終日は部分足の可能性があるため除外

    trs = []
    for prev_key, key in zip(complete[:-1], complete[1:]):
        prev_close = days[prev_key]["close"]
        d = days[key]
        tr = max(d["high"] - d["low"], abs(d["high"] - prev_close), abs(d["low"] - prev_close))
        trs.append(tr)
    last20 = trs[-20:]
    if len(last20) < 20:
        return None
    return round(sum(last20) / len(last20), 2)


# ------------------------------------------------------------ プール履歴

def _load_pools_history(path: Path = POOLS_HISTORY_PATH) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def record_pools(open_orders: dict, current_price: Optional[float],
                 today: Optional[date] = None, path: Path = POOLS_HISTORY_PATH) -> bool:
    """当日のプールマップを履歴に保存する（翌日以降の答え合わせ用）。"""
    if not isinstance(open_orders, dict):
        return False
    bsl = open_orders.get("bsl_candidates") or []
    ssl = open_orders.get("ssl_candidates") or []
    if not bsl and not ssl:
        return False
    today = today or date.today()

    history = _load_pools_history(path)
    history[today.isoformat()] = {
        "current_price": current_price,
        "bsl": bsl,
        "ssl": ssl,
    }
    cutoff = (today - timedelta(days=POOLS_RETENTION_DAYS)).isoformat()
    history = {k: v for k, v in history.items() if k >= cutoff}

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def load_baseline_pools(today: Optional[date] = None,
                        path: Path = POOLS_HISTORY_PATH) -> Optional[tuple[str, dict]]:
    """直近の過去プールマップ（1〜4 日前、最新優先）を返す。無ければ None。"""
    today = today or date.today()
    history = _load_pools_history(path)
    candidates = {}
    for key, snap in history.items():
        try:
            snap_date = datetime.strptime(key, "%Y-%m-%d").date()
        except ValueError:
            continue
        age = (today - snap_date).days
        if BASELINE_WINDOW_DAYS[0] <= age <= BASELINE_WINDOW_DAYS[1]:
            candidates[age] = (key, snap)
    if not candidates:
        return None
    return candidates[min(candidates)]


# ------------------------------------------------------------ スイープ検証

def detect_sweeps(pools: dict, bars: list[dict], atr: Optional[float]) -> list[dict]:
    """プールマップ（bsl/ssl リスト）に対する直近 24h のスイープ→反転を判定する。

    BSL プール: 高値がプール下端を超えたら sweep。その後、post-sweep 高値から
                終値までの戻りが REVERSAL_ATR_MULT × ATR 以上なら reversal。
    SSL プール: 対称。
    """
    if not bars:
        return []
    last_ts = bars[-1]["ts"]
    window_start = last_ts - timedelta(hours=SWEEP_WINDOW_HOURS)
    window = [b for b in bars if b["ts"] >= window_start]
    if not window:
        return []
    last_close = window[-1]["close"]
    threshold = (atr or 0) * REVERSAL_ATR_MULT

    events = []
    for side, key in (("BSL", "bsl"), ("SSL", "ssl")):
        for pool in pools.get(key) or []:
            low, high = pool.get("low"), pool.get("high")
            if low is None or high is None:
                continue
            sweep_idx = None
            for i, b in enumerate(window):
                if side == "BSL" and b["high"] > low:
                    sweep_idx = i
                    break
                if side == "SSL" and b["low"] < high:
                    sweep_idx = i
                    break
            event = {
                "side": side,
                "low": low,
                "high": high,
                "volume_sum": pool.get("volume_sum"),
                "swept_at_utc": None,
                "verdict": "no sweep",
                "retrace_atr": None,
            }
            if sweep_idx is not None:
                post = window[sweep_idx:]
                if side == "BSL":
                    extreme = max(b["high"] for b in post)
                    retrace = extreme - last_close
                else:
                    extreme = min(b["low"] for b in post)
                    retrace = last_close - extreme
                event["swept_at_utc"] = window[sweep_idx]["ts"].strftime("%Y-%m-%d %H:%M")
                if atr and retrace >= threshold:
                    event["verdict"] = "sweep→reversal"
                else:
                    event["verdict"] = "sweep→continuation"
                event["retrace_atr"] = round(retrace / atr, 2) if atr else None
            events.append(event)
    return events


# ------------------------------------------------------------ P/L 構造

def build_pl_structure(retail: dict, current_price: Optional[float]) -> dict:
    """リテールの Long/Short 建玉と平均エントリーから含み損益構造を計算する。"""
    out = {}
    if not isinstance(retail, dict) or current_price is None:
        return out
    for side in ("long", "short"):
        pct = retail.get(f"{side}_pct")
        avg = retail.get(f"avg_{side}_entry")
        entry = {
            "pct": pct,
            "avg_entry": avg,
            "volume_lots": retail.get(f"{side}_volume_lots"),
            "positions": retail.get(f"{side}_positions"),
            "pl_pct": None,
            "state": None,
        }
        if avg:
            if side == "long":
                pl = (current_price - avg) / avg * 100
            else:
                pl = (avg - current_price) / avg * 100
            entry["pl_pct"] = round(pl, 2)
            entry["state"] = "含み益" if pl >= 0 else "含み損"
        if pct is not None:
            out[side] = entry
    return out


# ------------------------------------------------------------ 統合ビルダー

def build_retail_analytics(
    retail: dict,
    open_orders: dict,
    current_price: Optional[float],
    h1_path: Optional[Path],
    today: Optional[date] = None,
    pools_path: Path = POOLS_HISTORY_PATH,
) -> dict:
    """リテール分析ブロックの全要素を構築する。

    Args:
        retail: retail_sentiment[symbol]（myfxbook.py の戻り値）
        open_orders: myfxbook_open_orders[symbol]（妥当性フィルタ済みクラスタ）
        current_price: 現在価格（open_orders.current_price または TwelveData close）
        h1_path: Dukascopy H1 CSV のパス（スイープ検証・ATR 用）
        today: テスト用の日付固定
    """
    today = today or date.today()
    result = {
        "current_price": current_price,
        "atr20d": None,
        "h1_last_bar_utc": None,
        "h1_stale": False,
        "pl_structure": {},
        "top_pools": {"bsl": [], "ssl": []},
        "baseline_date": None,
        "baseline_note": None,
        "sweep_events": [],
        "error": None,
    }

    # 1. P/L 構造
    result["pl_structure"] = build_pl_structure(retail, current_price)

    # 2. プール（距離付き）
    if isinstance(open_orders, dict) and current_price:
        for side, key in (("bsl", "bsl_candidates"), ("ssl", "ssl_candidates")):
            pools = []
            for c in open_orders.get(key) or []:
                mid = (c["low"] + c["high"]) / 2 if c.get("low") is not None else None
                pools.append({
                    **c,
                    "distance_pct": round((mid - current_price) / current_price * 100, 2)
                    if mid is not None else None,
                })
            result["top_pools"][side] = pools

    # 3. H1 / ATR
    bars = load_h1_bars(h1_path) if h1_path else []
    if bars:
        result["atr20d"] = compute_atr20d(bars)
        last_ts = bars[-1]["ts"]
        result["h1_last_bar_utc"] = last_ts.strftime("%Y-%m-%d %H:%M")
        age_hours = (datetime.now(timezone.utc) - last_ts).total_seconds() / 3600
        result["h1_stale"] = age_hours > H1_STALE_HOURS
    else:
        result["error"] = "H1 データなし（スイープ検証・ATR はスキップ）"

    # 4. スイープ検証（前日プール優先、無ければ当日プールを参考扱い）
    baseline = load_baseline_pools(today, pools_path)
    if baseline is not None:
        result["baseline_date"], pools_map = baseline
    else:
        pools_map = {
            "bsl": result["top_pools"]["bsl"],
            "ssl": result["top_pools"]["ssl"],
        }
        result["baseline_note"] = "前日プール履歴なし → 当日プールで参考判定"
    if bars:
        result["sweep_events"] = detect_sweeps(pools_map, bars, result["atr20d"])

    # 5. 当日プールを履歴へ保存（翌日の答え合わせ用）
    record_pools(open_orders, current_price, today, pools_path)

    return result


# ------------------------------------------------------------ 整形

def format_retail_analytics_lines(ra: dict) -> list[str]:
    """main.format_scraped_data 用の整形行を返す。"""
    if not isinstance(ra, dict):
        return []
    lines = ["### リテール分析 (Retail P/L・Liquidity Pools・Sweep 検証)"]

    cp, atr = ra.get("current_price"), ra.get("atr20d")
    head = f"- 現値 {cp}" if cp else "- 現値 N/A"
    if atr:
        head += f" | ATR20d {atr}"
    if ra.get("h1_last_bar_utc"):
        head += f" | H1 最終バー {ra['h1_last_bar_utc']} UTC"
        if ra.get("h1_stale"):
            head += "（36h 超過 — スイープ判定は参考）"
    lines.append(head)

    pl = ra.get("pl_structure") or {}
    if pl:
        lines.append("- リテール損益構造（含み損側の SL がリクイディティプールになる）:")
        fuel = {"short": "上方向に損切り燃料（Short SL = BSL）",
                "long": "下方向に損切り燃料（Long SL = SSL）"}
        tp = {"short": "下方向への利確圧力", "long": "上方向への利確圧力"}
        for side in ("short", "long"):
            e = pl.get(side)
            if not e:
                continue
            label = side.capitalize()
            vol = f" ({e['volume_lots']} lots / {e['positions']} positions)" \
                if e.get("volume_lots") is not None else ""
            if e.get("pl_pct") is not None:
                implication = fuel[side] if e["pl_pct"] < 0 else tp[side]
                lines.append(
                    f"  * {label} {e['pct']}%{vol}: 平均 {e['avg_entry']} → "
                    f"{e['state']} {e['pl_pct']:+.2f}% → {implication}"
                )
            else:
                lines.append(f"  * {label} {e['pct']}%{vol}: 平均エントリー取得不可")

    pools = ra.get("top_pools") or {}
    if pools.get("bsl") or pools.get("ssl"):
        lines.append("- リクイディティプール（オーダーブック由来、ボリューム順）:")
        for side_label, key in (("BSL(上)", "bsl"), ("SSL(下)", "ssl")):
            for c in pools.get(key) or []:
                share = f", side内シェア {c['share_pct']}%" if c.get("share_pct") is not None else ""
                dist = f", 現値から {c['distance_pct']:+.2f}%" if c.get("distance_pct") is not None else ""
                lines.append(
                    f"  * {side_label} {c.get('low')} – {c.get('high')} "
                    f"(volume {c.get('volume_sum')}{share}{dist})"
                )

    events = ra.get("sweep_events") or []
    if events:
        base = ra.get("baseline_date") or "当日"
        note = f"（{ra['baseline_note']}）" if ra.get("baseline_note") else ""
        lines.append(
            f"- スイープ検証（基準プール: {base}{note}、直近{SWEEP_WINDOW_HOURS}h、"
            f"反転閾値 {REVERSAL_ATR_MULT}×ATR）:"
        )
        for ev in events:
            detail = ""
            if ev["verdict"] != "no sweep":
                retrace = f", 戻り {ev['retrace_atr']}×ATR" if ev.get("retrace_atr") is not None else ""
                detail = f" (sweep {ev['swept_at_utc']} UTC{retrace})"
            lines.append(f"  * {ev['side']} {ev['low']} – {ev['high']}: {ev['verdict']}{detail}")

    if ra.get("error"):
        lines.append(f"- ※ {ra['error']}")
    return lines
