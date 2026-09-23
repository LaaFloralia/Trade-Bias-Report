"""Official release dates: FRED release calendar (Employment Situation, CPI) and Fed FOMC calendar page."""
import json, os, re, sys, urllib.parse, urllib.request
from pathlib import Path
OUT = Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)
key = os.environ["FRED_API_KEY"]
def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 research"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "ignore")
status = {}
for name, rid in [("employment_situation", 50), ("cpi", 10)]:
    meta = json.loads(get("https://api.stlouisfed.org/fred/release?" + urllib.parse.urlencode({"release_id": rid, "file_type": "json", "api_key": key})))
    dates = json.loads(get("https://api.stlouisfed.org/fred/release/dates?" + urllib.parse.urlencode({
        "release_id": rid, "file_type": "json", "api_key": key, "realtime_start": "2021-01-01", "realtime_end": "2026-12-31",
        "include_release_dates_with_no_data": "false", "limit": 1000, "sort_order": "asc"})))
    ds = [d["date"] for d in dates.get("release_dates", [])]
    (OUT / f"release_{name}.json").write_text(json.dumps({"release": meta.get("releases", [{}])[0].get("name"), "dates": ds}))
    status[name] = {"release_name": meta.get("releases", [{}])[0].get("name"), "n": len(ds), "first": ds[:1], "last": ds[-1:]}
html = get("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm")
(OUT / "fomccalendars.htm").write_text(html)
status["fomc_page_bytes"] = len(html)
print(json.dumps(status, indent=1))
