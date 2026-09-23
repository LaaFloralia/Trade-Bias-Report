"""Off-chart review 2026-09-23: archived Daily bias vs later XAU/USD, and gold vs US real yields.

Standard library only. Inputs come from fetch_prices.py (Twelve Data / FRED JSON) and the
archived machine outputs below. Results are research evidence, not trading signals.

    python evaluate.py DATA_DIR OUT_DIR
"""
import csv
import glob
import json
import math
import os
import sys
from datetime import date, datetime, timedelta, timezone

UTC, JST = timezone.utc, timezone(timedelta(hours=9))
INTEL_GLOB = "/Users/laa/dev/fundamental-macro-analysis/output/intel/intel_daily_*.json"
CHART_INTEL_GLOB = "/Users/laa/.codex/jobs/chart-intel/reports/daily/Daily_Bias_Report_2026-09-*.machine.json"


def load_hourly(data_dir):
    bars = []
    for row in json.load(open(os.path.join(data_dir, "xau_1h.json"))):
        t = datetime.strptime(row["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        wd, hr = t.weekday(), t.hour
        # Twelve Data emits interpolated bars while the market is shut (Fri 21:00 - Sun 22:00 UTC in EDT)
        if wd == 5 or (wd == 4 and hr >= 21) or (wd == 6 and hr < 22):
            continue
        bars.append((t, float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])))
    return bars


def load_records():
    recs = []
    for path in sorted(glob.glob(INTEL_GLOB)):
        d = json.load(open(path))
        stamp, source = d.get("generated_at"), "generated_at"
        if not stamp:
            stamp, source = datetime.fromtimestamp(os.path.getmtime(path), JST).isoformat(), "file_mtime"
        recs.append(dict(date=os.path.basename(path)[12:22], t0=datetime.fromisoformat(stamp).astimezone(UTC),
                         bias=float(d["bias"]), no_trade=bool(d["no_trade"]), conf=float(d["confidence"]),
                         ts_source=source))
    for path in sorted(glob.glob(CHART_INTEL_GLOB)):
        d = json.load(open(path))
        recs.append(dict(date=d["data_as_of"], t0=datetime.fromisoformat(d["generated_at"]).astimezone(UTC),
                         bias=float(d["bias"]), no_trade=bool(d["no_trade"]), conf=float(d["confidence"]),
                         ts_source="generated_at"))
    return sorted(recs, key=lambda r: r["t0"])


def ny_close_after(t):
    c = t.replace(hour=21, minute=0, second=0, microsecond=0)  # 17:00 New York (EDT) = 21:00 UTC, Jun-Sep
    while c <= t + timedelta(hours=2) or c.weekday() >= 5:
        c += timedelta(days=1)
    return c


def evaluate_bias(bars):
    times = [b[0] for b in bars]
    rows = []
    for r in load_records():
        start = r["t0"].replace(minute=0, second=0, microsecond=0)
        if start < r["t0"]:
            start += timedelta(hours=1)
        pos = next(i for i, t in enumerate(times) if t >= start)   # first full traded hour after issue
        seg24 = bars[pos:pos + 24]                                    # next 24 traded hourly bars
        entry = seg24[0][1]
        ny = [b for b in bars[pos:] if b[0] < ny_close_after(bars[pos][0])]
        prev = bars[max(0, pos - 24):pos]
        rows.append({**r, "entry_utc": bars[pos][0].isoformat(),
                     "ret24": seg24[-1][4] / entry - 1 if len(seg24) == 24 else None,
                     "rng24": (max(b[2] for b in seg24) - min(b[3] for b in seg24)) / entry if len(seg24) == 24 else None,
                     "retNY": ny[-1][4] / entry - 1 if ny else None,
                     "prev24": prev[-1][4] / prev[0][1] - 1 if len(prev) == 24 else None})
    return rows


def wilson(k, n, z=1.96):
    ph, den = k / n, 1 + z * z / n
    centre, margin = ph + z * z / (2 * n), z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n))
    return [round((centre - margin) / den, 3), round((centre + margin) / den, 3)]


def binom_two_sided(k, n):
    probs = [math.comb(n, i) / 2 ** n for i in range(n + 1)]
    return round(min(1.0, sum(p for p in probs if p <= probs[k] + 1e-15)), 3)


def ranks(values):
    order = sorted(range(len(values)), key=values.__getitem__)
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return out


def pearson(x, y):
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    return sxy / math.sqrt(sum((a - mx) ** 2 for a in x) * sum((b - my) ** 2 for b in y))


def slope(x, y):
    mx, my = sum(x) / len(x), sum(y) / len(y)
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / sum((a - mx) ** 2 for a in x)


def summarize_bias(rows):
    out = {"records": len(rows), "window_note": "24 traded hourly bars from the first full hour after issue; NY close = 21:00 UTC"}
    for name, subset in {"directional": [r for r in rows if r["bias"] != 0],
                         "plan_shown": [r for r in rows if not r["no_trade"] and r["bias"] != 0]}.items():
        for col in ("ret24", "retNY"):
            s = [r for r in subset if r[col] is not None]
            k = sum(1 for r in s if math.copysign(1, r["bias"]) * r[col] > 0)
            out[f"{name}|{col}"] = dict(n=len(s), hits=k, hit_rate=round(k / len(s), 3), wilson95=wilson(k, len(s)),
                                        p_vs_50=binom_two_sided(k, len(s)),
                                        always_long=sum(1 for r in s if r[col] > 0),
                                        prev24_momentum=sum(1 for r in s if r["prev24"] is not None and (r["prev24"] > 0) == (r[col] > 0)))
    ev = [r for r in rows if r["ret24"] is not None]
    out["spearman_bias_ret24"] = round(pearson(ranks([r["bias"] for r in ev]), ranks([r["ret24"] for r in ev])), 3)
    for flag in (False, True):
        rng = sorted(r["rng24"] for r in ev if r["no_trade"] is flag)
        out[f"range24_median|no_trade={flag}"] = dict(n=len(rng), median=round(rng[len(rng) // 2] if len(rng) % 2 else (rng[len(rng) // 2 - 1] + rng[len(rng) // 2]) / 2, 4))
    return out


def macro_regime(data_dir):
    gold = {r["datetime"]: float(r["close"]) for r in json.load(open(os.path.join(data_dir, "xau_1day.json")))}

    def fred(series_id):
        return {o["date"]: float(o["value"]) for o in json.load(open(os.path.join(data_dir, f"fred_{series_id}.json")))
                if o["value"] != "."}
    real, usd = fred("DFII10"), fred("DTWEXBGS")
    days = sorted(set(gold) & set(real) & set(usd))
    years = {}
    for prev, cur in zip(days, days[1:]):
        years.setdefault(cur[:4], []).append((math.log(gold[cur] / gold[prev]) * 100, (real[cur] - real[prev]) * 100,
                                              math.log(usd[cur] / usd[prev]) * 100, prev, cur))
    table = []
    for year, obs in sorted(years.items()):
        g, dr, du = [o[0] for o in obs], [o[1] for o in obs], [o[2] for o in obs]
        table.append(dict(year=int(year), n=len(obs), corr_gold_real_yield=round(pearson(g, dr), 2),
                          gold_pct_per_10bp=round(slope(dr, g) * 10, 2), corr_gold_usd=round(pearson(g, du), 2),
                          gold_change_pct=round((gold[obs[-1][4]] / gold[obs[0][3]] - 1) * 100, 1),
                          real_yield_change_bp=round((real[obs[-1][4]] - real[obs[0][3]]) * 100)))
    level_days = sorted(set(gold) & set(real))
    fit = [d for d in level_days if d < "2022-01-01"]
    b = slope([real[d] for d in fit], [math.log(gold[d]) for d in fit])
    a = sum(math.log(gold[d]) for d in fit) / len(fit) - b * sum(real[d] for d in fit) / len(fit)
    checks = []
    for target in ("2021-12-31", "2023-12-29", "2024-12-31", "2025-12-31", "2026-09-18"):
        d = max(x for x in level_days if x <= target)
        checks.append(dict(date=d, gold=round(gold[d]), real_yield=real[d], implied_by_2015_2021_fit=round(math.exp(a + b * real[d]))))
    return dict(yearly=table, level_fit_2015_2021=dict(log_pct_per_1pt=round(b * 100, 1),
                r=round(pearson([real[d] for d in fit], [math.log(gold[d]) for d in fit]), 2), checks=checks))


def main(data_dir, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    rows = evaluate_bias(load_hourly(data_dir))
    with open(os.path.join(out_dir, "bias_rows.csv"), "w", newline="") as f:
        cols = ["date", "t0_jst", "ts_source", "bias", "no_trade", "conf", "prev24", "ret24", "retNY", "rng24"]
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({**{c: r[c] for c in cols if c in r}, "t0_jst": r["t0"].astimezone(JST).strftime("%Y-%m-%d %H:%M"),
                        **{c: None if r[c] is None else round(r[c] * 100, 2) for c in ("prev24", "ret24", "retNY", "rng24")}})
    summary = {"generated_on": date.today().isoformat(), "bias_history": summarize_bias(rows), "macro_regime": macro_regime(data_dir)}
    with open(os.path.join(out_dir, "summary.json"), "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(json.dumps(summary["bias_history"], ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
