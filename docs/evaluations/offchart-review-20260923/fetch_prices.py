"""Fetch XAU/USD bars (Twelve Data) and FRED series for the off-chart review.
Keys come from the environment injected by the project's 1Password wrapper; never printed."""
import json, os, sys, time, urllib.parse, urllib.request
from pathlib import Path

OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)

def get(url, params):
    q = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{url}?{q}", timeout=60) as r:
        return json.loads(r.read().decode())

td = os.environ.get("TWELVEDATA_API_KEY")
fred = os.environ.get("FRED_API_KEY")
status = {}
if td:
    for name, interval, start in [("xau_1h", "1h", "2026-05-01 00:00:00"), ("xau_1day", "1day", "2015-01-01 00:00:00")]:
        d = get("https://api.twelvedata.com/time_series", {
            "symbol": "XAU/USD", "interval": interval, "start_date": start,
            "end_date": "2026-09-24 00:00:00", "timezone": "UTC", "outputsize": 5000,
            "order": "ASC", "apikey": td})
        if d.get("status") != "ok":
            status[name] = {"ok": False, "code": d.get("code"), "message": str(d.get("message"))[:160]}
        else:
            vals = d["values"]
            (OUT / f"{name}.json").write_text(json.dumps(vals))
            status[name] = {"ok": True, "rows": len(vals), "first": vals[0]["datetime"], "last": vals[-1]["datetime"]}
        time.sleep(8)
else:
    status["twelvedata"] = "missing_env"
if fred:
    for sid in ["DFII10", "DTWEXBGS", "DGS10"]:
        d = get("https://api.stlouisfed.org/fred/series/observations", {
            "series_id": sid, "observation_start": "2015-01-01", "file_type": "json", "api_key": fred})
        obs = d.get("observations", [])
        (OUT / f"fred_{sid}.json").write_text(json.dumps(obs))
        status[sid] = {"rows": len(obs), "last": obs[-1]["date"] if obs else None}
else:
    status["fred"] = "missing_env"
print(json.dumps(status, ensure_ascii=False, indent=1))
