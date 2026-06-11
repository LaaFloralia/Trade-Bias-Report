"""FRED (Federal Reserve Economic Data) — Treasury yields & USD macro proxy

Replaces previous Investing.com / CNBC US10Y scraping. Adds DGS2 and DTWEXBGS
(Broad USD Index, NOT a DXY substitute — emitted as separate USD macro proxy).

API key resolution order (never logged):
  1. Environment variable FRED_API_KEY
  2. macOS Keychain (service: FRED_API_KEY, account: $USER)
  3. .env file via python-dotenv

Fetched series:
  - DGS10:    US 10Y Treasury constant maturity rate
  - DGS2:     US  2Y Treasury constant maturity rate
  - DTWEXBGS: Broad USD Index (Goods & Services), USD macro proxy
  - DFII10:   US 10Y TIPS yield (real rate) — XAUUSD の主ドライバー
  - T10YIE:   10Y Breakeven Inflation Rate（DGS10 - DFII10 の派生系列）

Identity: DGS10 ≒ DFII10 + T10YIE（validation.py が恒等式チェックを行う）

Each result conforms to the project metadata schema:
  source / series_id / value / prev_value / change / timestamp /
  as_of_date / prev_as_of_date / stale / fallback_used / error? / note?

Stale fallback: on fetch failure, attempts to recover the most recent
prior successful value from `output/scraped_data_*.json`, marking
stale=true and fallback_used=true.

Public API: https://fred.stlouisfed.org/docs/api/fred/series_observations.html
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

import requests
from dotenv import load_dotenv

FRED_BASE = "https://api.stlouisfed.org/fred"

# 系列別設定。stale_days は as_of_date と現在日（UTC）の差が
# この閾値（calendar days）を超えた時点で stale=true をマークする。
#   DGS10 / DGS2 ... daily release（営業日）。週末跨ぎを許容して 5 暦日。
#   DTWEXBGS    ... weekly release（金曜更新）。2 サイクル分余裕を見て 14 暦日。
SERIES_CONFIG = {
    "DGS10":    {"label": "US 10Y Treasury yield",          "stale_days": 5},
    "DGS2":     {"label": "US  2Y Treasury yield",          "stale_days": 5},
    "DTWEXBGS": {"label": "Broad USD Index (Goods+Services)", "stale_days": 14},
    # Daily release（営業日）。XAUUSD バイアスの実質金利・インフレ期待ペア。
    "DFII10":   {"label": "US 10Y TIPS yield (real)",       "stale_days": 5},
    "T10YIE":   {"label": "10Y Breakeven Inflation",        "stale_days": 5},
    # OECD monthly + ~6-week publication lag; >1 cycle gap = stale.
    "IRLTLT01DEM156N": {"label": "Germany Long-term Government Bond Yield", "stale_days": 75},
    "IRLTLT01JPM156N": {"label": "Japan Long-term Government Bond Yield",   "stale_days": 75},
    "IRLTLT01GBM156N": {"label": "UK Long-term Government Bond Yield",      "stale_days": 75},
    "IRLTLT01CAM156N": {"label": "Canada Long-term Government Bond Yield",  "stale_days": 75},
    # Weekly, Wednesday release.
    "WALCL": {"label": "Fed Total Assets", "stale_days": 10},
    # Business-daily.
    "RRPONTSYD": {"label": "Overnight Reverse Repo Operations", "stale_days": 5},
    # Weekly.
    "WTREGEN": {"label": "Treasury General Account", "stale_days": 10},
    # Business-daily.
    "VIXCLS": {"label": "CBOE Volatility Index: VIX", "stale_days": 5},
    # Business-daily.
    "VXVCLS": {"label": "CBOE 3-Month Volatility Index", "stale_days": 5},
}
# main フロー（fetch_fred_data）で取得する系列。他系列は各スクレイパーが個別取得。
SERIES_IDS = ["DGS10", "DGS2", "DTWEXBGS", "DFII10", "T10YIE"]

HTTP_TIMEOUT = 15
RETRY_ATTEMPTS = 2
PROJECT_ROOT = Path(__file__).parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"
STALE_LOOKBACK_DAYS = 7  # how many recent JSON files to scan for fallback


def _read_keychain(service: str, account: Optional[str] = None) -> Optional[str]:
    """Read a secret from macOS Keychain. Returns None on miss / error.
    NEVER prints the value."""
    if account is None:
        account = os.getenv("USER", "")
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            value = result.stdout.strip()
            return value or None
    except Exception:
        pass
    return None


def get_fred_api_key() -> Optional[str]:
    """Resolve the FRED API key with the documented precedence.
    Returns None if no source provides one. Never logs the value."""
    # 1. environment variable (process inherited from shell)
    key = os.getenv("FRED_API_KEY")
    if key:
        return key

    # 2. macOS Keychain (silent access via -A registration)
    key = _read_keychain("FRED_API_KEY")
    if key:
        return key

    # 3. .env file (project root)
    dotenv_path = PROJECT_ROOT / ".env"
    if dotenv_path.exists():
        load_dotenv(dotenv_path, override=False)
        key = os.getenv("FRED_API_KEY")
        if key:
            return key

    return None


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _empty_metadata(series_id: str) -> dict:
    return {
        "source": "FRED",
        "series_id": series_id,
        "value": None,
        "prev_value": None,
        "change": None,
        "timestamp": _now_utc_iso(),
        "as_of_date": None,
        "prev_as_of_date": None,
        "stale": False,
        "fallback_used": False,
    }


def _load_last_known(series_id: str) -> Optional[dict]:
    """Find the most recent successful FRED entry for `series_id` from prior
    output JSON files. Returns the cached metadata dict (with value) or None.
    Used as a stale fallback when live fetch fails."""
    if not OUTPUT_DIR.exists():
        return None
    candidates = sorted(OUTPUT_DIR.glob("scraped_data_*.json"), reverse=True)
    for path in candidates[:STALE_LOOKBACK_DAYS]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        fred = data.get("fred")
        if not isinstance(fred, dict):
            continue
        entry = fred.get(series_id)
        if isinstance(entry, dict) and entry.get("value") is not None:
            return entry
    return None


def _fetch_series_observations(series_id: str, api_key: str) -> dict:
    """Fetch the latest valid observations for a series. Returns:
        {"value": float, "as_of_date": "YYYY-MM-DD",
         "prev_value": float|None, "prev_as_of_date": str|None}
    or {"error": "..."} on failure. Skips '.' (no-data marker)."""
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 10,
    }
    last_err: Optional[str] = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            resp = requests.get(
                f"{FRED_BASE}/series/observations",
                params=params,
                timeout=HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            payload = resp.json()
            valid = []
            for obs in payload.get("observations", []):
                raw = (obs.get("value") or "").strip()
                if not raw or raw == ".":
                    continue
                try:
                    valid.append((float(raw), obs.get("date")))
                except ValueError:
                    continue
                if len(valid) >= 2:
                    break
            if not valid:
                return {"error": "no valid observation in latest 10 (all '.')"}
            value, as_of = valid[0]
            prev_value, prev_as_of = (valid[1] if len(valid) >= 2 else (None, None))
            return {
                "value": value,
                "as_of_date": as_of,
                "prev_value": prev_value,
                "prev_as_of_date": prev_as_of,
            }
        except requests.HTTPError as exc:
            last_err = f"HTTP {exc.response.status_code if exc.response is not None else '?'}"
        except requests.Timeout:
            last_err = "timeout"
        except Exception as exc:  # noqa: BLE001
            last_err = type(exc).__name__
    return {"error": last_err or "unknown error"}


def fetch_fred_series(series_id: str, api_key: Optional[str] = None) -> dict:
    """Fetch one FRED series and return a metadata-conformant dict.
    Falls back to last-known cached value (stale=true) on fetch failure."""
    if api_key is None:
        api_key = get_fred_api_key()

    meta = _empty_metadata(series_id)

    if not api_key:
        meta["error"] = "FRED_API_KEY not configured (env / Keychain / .env all empty)"
        # try stale fallback regardless
        cached = _load_last_known(series_id)
        if cached:
            meta["value"] = cached.get("value")
            meta["prev_value"] = cached.get("prev_value")
            meta["change"] = cached.get("change")
            meta["as_of_date"] = cached.get("as_of_date")
            meta["prev_as_of_date"] = cached.get("prev_as_of_date")
            meta["stale"] = True
            meta["fallback_used"] = True
            meta["note"] = "API key unavailable; using cached value"
        return meta

    obs = _fetch_series_observations(series_id, api_key)

    if "error" in obs:
        cached = _load_last_known(series_id)
        if cached:
            meta["value"] = cached.get("value")
            meta["prev_value"] = cached.get("prev_value")
            meta["change"] = cached.get("change")
            meta["as_of_date"] = cached.get("as_of_date")
            meta["prev_as_of_date"] = cached.get("prev_as_of_date")
            meta["stale"] = True
            meta["fallback_used"] = True
            meta["error"] = obs["error"]
            meta["note"] = "fetch failed; using last-known value from output cache"
        else:
            meta["error"] = obs["error"]
        return meta

    value = obs["value"]
    prev_value = obs.get("prev_value")
    change = (value - prev_value) if (prev_value is not None) else None
    meta.update({
        "value": value,
        "prev_value": prev_value,
        "change": change,
        "as_of_date": obs["as_of_date"],
        "prev_as_of_date": obs.get("prev_as_of_date"),
    })

    # 系列別 stale 判定: as_of_date が SERIES_CONFIG[series_id]["stale_days"] を超えたら stale=true
    threshold = SERIES_CONFIG.get(series_id, {}).get("stale_days")
    as_of_str = obs.get("as_of_date")
    if threshold and as_of_str:
        try:
            as_of = datetime.strptime(as_of_str, "%Y-%m-%d").date()
            age_days = (datetime.now(timezone.utc).date() - as_of).days
            if age_days > threshold:
                meta["stale"] = True
                meta["note"] = (
                    f"as_of {as_of_str} is {age_days}d old "
                    f"(series threshold {threshold} calendar days)"
                )
        except ValueError:
            pass

    return meta


async def fetch_fred_data() -> dict:
    """Fetch all configured FRED series concurrently (sync HTTP in thread pool).
    Returns: {series_id: metadata_dict}. Single-source failure does NOT abort
    the others (graceful degradation)."""
    api_key = get_fred_api_key()
    loop = asyncio.get_event_loop()
    tasks = [
        loop.run_in_executor(None, fetch_fred_series, sid, api_key)
        for sid in SERIES_IDS
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    out: dict = {}
    for sid, res in zip(SERIES_IDS, results):
        if isinstance(res, Exception):
            entry = _empty_metadata(sid)
            entry["error"] = f"unhandled: {type(res).__name__}: {res}"
            out[sid] = entry
            print(f"  [ERROR] FRED/{sid}: {entry['error']}")
            continue
        out[sid] = res
        if res.get("value") is not None:
            stale_tag = " [STALE]" if res.get("stale") else ""
            print(f"  [OK]    FRED/{sid}: {res['value']} (as_of {res['as_of_date']}){stale_tag}")
        else:
            print(f"  [ERROR] FRED/{sid}: {res.get('error', 'unknown')}")
    return out


if __name__ == "__main__":
    api_key = get_fred_api_key()
    print(f"FRED_API_KEY resolved: {bool(api_key)} (len={len(api_key) if api_key else 0})")
    if not api_key:
        print()
        print("Setup: register FRED_API_KEY to macOS Keychain with silent access:")
        print('  security add-generic-password -a "$USER" -s "FRED_API_KEY" -w "<key>" -A -U')
        print("Get a free key at https://fred.stlouisfed.org/")
        sys.exit(1)
    print()
    results = asyncio.run(fetch_fred_data())
    print()
    print("--- FRED Results (metadata schema) ---")
    for sid, r in results.items():
        for k, v in r.items():
            print(f"  {sid}.{k}: {v}")
        print()
