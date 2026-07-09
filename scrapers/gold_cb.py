"""中銀ゴールド購入 スクレイパー (IMF IRFCL)

ソース: IMF SDMX 2.1 API / IRFCL (International Reserves and Foreign Currency Liquidity)
  https://api.imf.org/external/sdmx/2.1/data/IMF.STA,IRFCL/.IRFCLDT1_IRFCL56V_FTO..M
  指標 IRFCLDT1_IRFCL56V_FTO = Monetary Gold, Volume in Fine Troy Ounces (月次・国別)

World Gold Council の月次中銀統計はダウンロードが CAPTCHA 保護されているため、
大元の IMF 公式データから報告国ベースの純購入量を集計する。
master_prompt セクション1.5 (ファンダ大局バイアス) の XAUUSD ドライバー入力。

集計上の注意 (WGC 統計との違い):
  - IRFCL 報告国のみの集計 (未報告国・遅延報告は含まれない)
  - 一部の国は単位変更・報告訂正で物理的にあり得ない月次変化を出すため、
    レベル/変化量の妥当性フィルタで除外する (excluded に記録)
  - 方向性・レジーム判定用であり、WGC のヘッドライン値とは一致しない
"""

from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import requests
from config import USER_AGENT

IMF_URL_TMPL = (
    "https://api.imf.org/external/sdmx/2.1/data/"
    "IMF.STA,IRFCL/.IRFCLDT1_IRFCL56V_FTO..M?startPeriod={start}"
)
HTTP_TIMEOUT = 90

OZT_TO_TONNE = 31.1034768 / 1e6  # troy oz → トン

# 妥当性フィルタ:
#   MAX_LEVEL_OZT: 米国 (261.5M ozt ≒ 8,133 t) を超えるレベルは単位アーティファクト
#   MAX_MONTHLY_CHANGE_T: 単一国の月次 ±100 t 超は報告訂正・単位変更とみなす
MAX_LEVEL_OZT = 300_000_000
MAX_MONTHLY_CHANGE_T = 100.0
MOVER_MIN_T = 2.0        # 「主な動き」に載せる下限
MIN_REPORTERS = 10       # 集計月として採用する最低報告国数
PARTIAL_REPORTERS = 30   # これ未満は速報扱い (直近月は報告が出揃っていない)
REGIME_THRESHOLD_T = 20  # 3ヶ月累計 ±20 t でレジーム判定

# 主要国の表示名 (ISO3 → 日本語)
COUNTRY_JA = {
    "CHN": "中国", "POL": "ポーランド", "TUR": "トルコ", "RUS": "ロシア",
    "IND": "インド", "KAZ": "カザフスタン", "UZB": "ウズベキスタン",
    "AZE": "アゼルバイジャン", "SGP": "シンガポール", "CZE": "チェコ",
    "HUN": "ハンガリー", "QAT": "カタール", "IRQ": "イラク", "JOR": "ヨルダン",
    "GHA": "ガーナ", "PHL": "フィリピン", "BRA": "ブラジル", "EGY": "エジプト",
    "THA": "タイ", "JPN": "日本", "USA": "米国", "DEU": "ドイツ",
}


def _label(iso3: str) -> str:
    ja = COUNTRY_JA.get(iso3)
    return f"{iso3}({ja})" if ja else iso3


def _parse_sdmx_series(xml_text: str) -> dict[str, dict[str, float]]:
    """StructureSpecificData XML → {ISO3: {period: ozt}}。0 値は欠測として捨てる。"""
    per_country: dict[str, dict[str, float]] = {}
    for m in re.finditer(r"<Series ([^>]+)>(.*?)</Series>", xml_text, re.S):
        attrs, body = m.group(1), m.group(2)
        cm = re.search(r'COUNTRY="([^"]+)"', attrs)
        if not cm:
            continue
        obs = {}
        for om in re.finditer(r'TIME_PERIOD="([^"]+)" OBS_VALUE="([^"]+)"', body):
            try:
                v = float(om.group(2))
            except ValueError:
                continue
            if v > 0:
                obs[om.group(1)] = v
        if obs:
            per_country[cm.group(1)] = obs
    return per_country


def _aggregate_monthly(per_country: dict[str, dict[str, float]]) -> list[dict]:
    """国別時系列から月次の報告国ベース純変化 (トン) を集計する。

    - レベルが MAX_LEVEL_OZT 超の国は系列ごと除外 (単位アーティファクト)
    - 月次変化 |Δ| > MAX_MONTHLY_CHANGE_T は当該国の当月寄与を除外し excluded に記録
    - 前月・当月の両方を報告した国のみ集計 (報告国構成の変化によるノイズ排除)
    """
    clean = {
        c: obs for c, obs in per_country.items()
        if max(obs.values()) <= MAX_LEVEL_OZT
    }
    periods = sorted({p for obs in clean.values() for p in obs})
    months: list[dict] = []
    for prev, cur in zip(periods, periods[1:]):
        net, reporters = 0.0, 0
        movers: list[tuple[str, float]] = []
        excluded: list[tuple[str, float]] = []
        for c, obs in clean.items():
            if prev not in obs or cur not in obs:
                continue
            diff_t = (obs[cur] - obs[prev]) * OZT_TO_TONNE
            if abs(diff_t) > MAX_MONTHLY_CHANGE_T:
                excluded.append((c, round(diff_t, 1)))
                continue
            net += diff_t
            reporters += 1
            if abs(diff_t) >= MOVER_MIN_T:
                movers.append((c, round(diff_t, 1)))
        movers.sort(key=lambda x: -abs(x[1]))
        months.append({
            "period": cur,
            "net_tonnes": round(net, 1),
            "reporters": reporters,
            "partial": reporters < PARTIAL_REPORTERS,
            "top_movers": movers[:4],
            "excluded": excluded,
        })
    return [m for m in months if m["reporters"] >= MIN_REPORTERS]


def _fetch_xml(start: str) -> str:
    url = IMF_URL_TMPL.format(start=start)
    resp = requests.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/xml"},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.text


async def scrape_gold_cb() -> dict:
    """中銀ゴールド購入 (IMF IRFCL 報告国ベース) を取得・集計する。

    Returns (metadata schema 準拠):
        {
            "source": "IMF IRFCL (monthly)",
            "symbol": "XAU_CB",
            "months": [{"period", "net_tonnes", "reporters", "partial", "top_movers", "excluded"}],
                       # 新しい順・最大 3 ヶ月 (partial=True は報告未出揃いの速報月)
            "cumulative_3m_t": float,      # 確定月 (partial 除く・最大 3 ヶ月) の純変化合計
            "regime": "net_buying" | "net_selling" | "neutral" | "unknown",
            "as_of_date": str | None,      # 最新集計月 ("2026-M05" 形式)
            "note": str,
            "error": str | None,
        }
    """
    base = {
        "source": "IMF IRFCL (monthly)",
        "symbol": "XAU_CB",
        "note": "IRFCL 報告国ベースの集計。WGC 統計とは範囲・定義が異なる (レジーム判定用)",
    }
    start = (datetime.now(timezone.utc) - timedelta(days=300)).strftime("%Y-%m")
    print("  中銀ゴールド (IMF IRFCL): 全報告国の保有量を取得中...")
    try:
        xml_text = await asyncio.to_thread(_fetch_xml, start)
        per_country = _parse_sdmx_series(xml_text)
        months = _aggregate_monthly(per_country)
    except Exception as e:
        print(f"  [WARN] 中銀ゴールド (IMF IRFCL): {e}")
        return {**base, "error": f"{type(e).__name__}: {e}"}
    if not months:
        return {**base, "error": "集計可能な月次データなし (報告国不足)"}

    recent = list(reversed(months[-3:]))  # 新しい順 (速報月含む、表示用)
    # レジームは報告が出揃った確定月のみで判定 (速報月の少数報告に引きずられない)
    confirmed = [m for m in reversed(months) if not m["partial"]][:3]
    cumulative = round(sum(m["net_tonnes"] for m in confirmed), 1)
    if len(confirmed) < 2:
        regime = "unknown"
    elif cumulative >= REGIME_THRESHOLD_T:
        regime = "net_buying"
    elif cumulative <= -REGIME_THRESHOLD_T:
        regime = "net_selling"
    else:
        regime = "neutral"
    print(
        f"  [OK]    中銀ゴールド (IMF IRFCL): 直近 {recent[0]['period']} "
        f"{recent[0]['net_tonnes']:+} t / 3ヶ月累計 {cumulative:+} t ({regime})"
    )
    return {
        **base,
        "months": recent,
        "cumulative_3m_t": cumulative,
        "regime": regime,
        "as_of_date": recent[0]["period"],
        "error": None,
    }


if __name__ == "__main__":
    d = asyncio.run(scrape_gold_cb())
    if d.get("error"):
        print(f"Error: {d['error']}")
    else:
        for m in d["months"]:
            movers = ", ".join(f"{_label(c)} {v:+}" for c, v in m["top_movers"])
            print(f"  {m['period']}: {m['net_tonnes']:+} t (n={m['reporters']}) {movers}")
        print(f"3ヶ月累計: {d['cumulative_3m_t']:+} t → {d['regime']}")
