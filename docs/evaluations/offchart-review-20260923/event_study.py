"""Diagnostic (not PIT-grade): US NFP/CPI/FOMC hours vs XAUUSD range/spread, and existing price_only trades.
Event dates are actual release dates (FRED release calendar, Fed FOMC calendar page), i.e. an ex-post list."""
import json, re, sys, math
from datetime import datetime, date, timedelta, timezone
import pandas as pd

S = sys.argv[1]; LAB = "/Users/laa/dev/xau-strategy-lab"
UTC = timezone.utc

def us_dst(d):  # second Sunday of March .. first Sunday of November
    m = date(d.year, 3, 8); m += timedelta(days=(6 - m.weekday()) % 7)
    n = date(d.year, 11, 1); n += timedelta(days=(6 - n.weekday()) % 7)
    return m <= d < n

def et_to_utc(d, hh, mm):
    off = 4 if us_dst(d) else 5
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=UTC) + timedelta(hours=off)

events = []
for name in ("employment_situation", "cpi"):
    for s in json.load(open(f"{S}/data/release_{name}.json"))["dates"]:
        d = date.fromisoformat(s)
        events.append(("NFP" if name == "employment_situation" else "CPI", et_to_utc(d, 8, 30)))
html = open(f"{S}/data/fomccalendars.htm").read()
MONTHS = {m: i for i, m in enumerate(["January","February","March","April","May","June","July","August","September","October","November","December"], 1)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
pos = [(m.start(), int(m.group(1))) for m in re.finditer(r"(\d{4}) FOMC Meetings", html)]
fomc = []
for m in re.finditer(r'fomc-meeting__month[^"]*"><strong>([^<]+)</strong>.*?fomc-meeting__date[^>]*>([^<]+)<', html, re.S):
    before = [(p, y) for p, y in pos if p < m.start()]
    year = max(before)[1] if before else None  # nearest preceding year heading
    month_txt, day_txt = m.group(1).strip(), m.group(2).strip()
    if year is None or "notation" in day_txt.lower() or "unscheduled" in day_txt.lower():
        continue
    month = MONTHS[month_txt.split("/")[-1].strip()]
    last_day = int(re.findall(r"\d+", day_txt)[-1])
    d = date(year, month, last_day)
    fomc.append(d)
    events.append(("FOMC", et_to_utc(d, 14, 0)))
ev = pd.DataFrame(events, columns=["kind", "t"]).drop_duplicates().sort_values("t")
ev = ev[(ev["t"] >= "2021-06-01") & (ev["t"] <= "2026-06-11")]

h1 = pd.read_parquet(f"{LAB}/data/xauusd_dukascopy_v1/H1.parquet")
h1 = h1[h1.index.weekday < 5].copy()
h1["rng"] = (h1["high"] - h1["low"]) / h1["open"] * 1e4       # bp
h1["hour"] = h1.index.hour
event_hours = {}
for k, t in ev.itertuples(index=False):
    event_hours[t.floor("h")] = k
event_days = {t.normalize() for t in ev["t"]}
rows = []
for bar_t, kind in event_hours.items():
    if bar_t not in h1.index: continue
    ctrl = h1[(h1["hour"] == bar_t.hour) & (h1.index < bar_t) & (h1.index >= bar_t - pd.Timedelta(days=45))]
    ctrl = ctrl[~ctrl.index.normalize().isin(event_days)].tail(20)
    if len(ctrl) < 10: continue
    b = h1.loc[bar_t]
    rows.append(dict(kind=kind, t=bar_t, rng=b["rng"], ctrl_rng=ctrl["rng"].median(), ctrl_p95=ctrl["rng"].quantile(0.95),
                     spread_max=b["spread_max"], ctrl_spread_max=ctrl["spread_max"].median(),
                     spread_mean=b["spread_mean"], ctrl_spread_mean=ctrl["spread_mean"].median()))
er = pd.DataFrame(rows)
market = {}
for kind, g in er.groupby("kind"):
    market[kind] = dict(n=len(g), median_range_ratio=round(float((g["rng"] / g["ctrl_rng"]).median()), 2),
                        share_above_ctrl_p95=round(float((g["rng"] > g["ctrl_p95"]).mean()), 2),
                        median_spread_max_ratio=round(float((g["spread_max"] / g["ctrl_spread_max"]).median()), 2),
                        median_spread_mean_ratio=round(float((g["spread_mean"] / g["ctrl_spread_mean"]).median()), 2),
                        median_event_range_bp=round(float(g["rng"].median()), 1), median_ctrl_range_bp=round(float(g["ctrl_rng"].median()), 1))
print(json.dumps({"events_in_price_window": ev["kind"].value_counts().to_dict(), "fomc_dates_parsed": len(fomc), "market": market}, indent=1, default=str))

def trade_windows(df, label):
    df = df.copy()
    df["et"] = pd.to_datetime(df["EntryTime"]).dt.tz_localize("UTC")
    tt = ev["t"].to_numpy()
    def classify(x):
        deltas = [(x - t).total_seconds() / 60 for t in ev["t"] if abs((x - t).total_seconds()) < 86400]
        if any(-30 <= d < 0 for d in deltas): return "pre30 (lab filter blocks)"
        if any(0 <= d < 120 for d in deltas): return "post0-120m"
        if x.normalize() in event_days: return "event_day_other"
        return "non_event_day"
    df["bucket"] = df["et"].apply(classify)
    out = {}
    for b, g in df.groupby("bucket"):
        wins = int((g["PnL"] > 0).sum()); n = len(g)
        out[b] = dict(n=n, win_rate=round(wins / n, 3), avg_R=round(float(g["R"].mean()), 3), sum_R=round(float(g["R"].sum()), 1))
    return {label: out}

res = {}
v15 = pd.read_csv(f"{LAB}/runs/ictsmc-v15-review-20260914/trades.csv")
res.update(trade_windows(v15, "v15 A 2024-25 base price_only"))
v6 = pd.read_csv(f"{LAB}/runs/ictsmc-v6-review-20260913/all-trades.csv")
v6b = v6[(v6["cost"] == "base") & (v6["news_mode"] == "price_only") & (v6["year"].astype(str).isin(["2024", "2025"]))]
res.update(trade_windows(v6b.drop_duplicates(subset=["EntryTime", "Size"]), f"v6 base price_only 2024+2025, variants {sorted(v6b['variant'].unique())} pooled, duplicate entries removed"))
for var, g in v6b.groupby("variant"):
    res.update(trade_windows(g, f"v6 variant {var}"))
print(json.dumps(res, indent=1, ensure_ascii=False))
json.dump({"market": market, "trades": res, "events": ev["kind"].value_counts().to_dict()}, open(f"{S}/event_study.json", "w"), indent=1, default=str)


def event_day_stats(df, label):
    """Event day (any NFP/CPI/FOMC date) vs other days, with a day-block bootstrap of the mean-R gap."""
    import random
    df = df.copy(); df["et"] = pd.to_datetime(df["EntryTime"]).dt.tz_localize("UTC")
    df["event_day"] = df["et"].dt.normalize().isin(event_days)
    a, b = df[df["event_day"]], df[~df["event_day"]]
    random.seed(20260923)
    days_a = [g["R"].tolist() for _, g in a.groupby(a["et"].dt.normalize())]
    days_b = [g["R"].tolist() for _, g in b.groupby(b["et"].dt.normalize())]
    diffs = []
    for _ in range(2000):
        sa = [x for day in random.choices(days_a, k=len(days_a)) for x in day]
        sb = [x for day in random.choices(days_b, k=len(days_b)) for x in day]
        diffs.append(sum(sa) / len(sa) - sum(sb) / len(sb))
    diffs.sort()
    return {label: dict(event_day_n=len(a), other_n=len(b), avg_R_event_day=round(float(a["R"].mean()), 3),
                        avg_R_other=round(float(b["R"].mean()), 3), gap_boot95=[round(diffs[50], 3), round(diffs[1949], 3)],
                        win_event_day=round(float((a["PnL"] > 0).mean()), 3), win_other=round(float((b["PnL"] > 0).mean()), 3),
                        avg_R_all=round(float(df["R"].mean()), 3), trades_removed_pct=round(100 * len(a) / len(df), 1))}

day_stats = {}
day_stats.update(event_day_stats(v15, "v15 A 2024-25 base price_only"))
day_stats.update(event_day_stats(v6b.drop_duplicates(subset=["EntryTime", "Size"]), "v6 base price_only 2024+2025 pooled unique"))
print(json.dumps(day_stats, indent=1))
json.dump({"note": "Ex-post release dates (FRED release calendar; Fed FOMC calendar page). Diagnostic only, not PIT-grade news data.",
           "market": market, "trade_windows": res, "event_day": day_stats, "events": ev["kind"].value_counts().to_dict()},
          open(f"{S}/event_study.json", "w"), indent=1, default=str)
