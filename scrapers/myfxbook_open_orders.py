"""MyFXBook Open Orders / Order Book ヒートマップスクレイパー

対象 URL: https://www.myfxbook.com/community/outlook/{symbol}
取得データ: ページ内の "Order Book (Positions)" セクションから
価格帯別の Bid 注文 (= 下方 SSL 候補) / Ask 注文 (= 上方 BSL 候補) を抽出。

DOM 構造（2026-05 時点）:
  - h3 "Order Book (Positions)" の直下に Bar chart canvas
  - inner_text として volume\nprice の列が並ぶ (前半 ~15 件が Bids、後半 ~15 件が Asks)
  - 末尾に "Asks", "Price", "Bids" の軸ラベル

ICT 用途:
  - 現在価格より上方に集中している Ask 注文 = BSL（Short SL がここで stop hunt されやすい）候補
  - 現在価格より下方に集中している Bid 注文 = SSL（Long SL がここで stop hunt されやすい）候補
  - top-3 クラスタを返し、Smart Money のターゲット帯を実数で示す

注: Bid/Ask の本質はリミット注文の集中度であり、純粋な Buy Stop / Sell Stop ではない。
ただし MyFXBook の outlook ユーザーの保有ポジションから派生した注文集中度として、
ICT の流動性プールの代理指標として使える。
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.async_api import async_playwright
from config import BROWSER_TIMEOUT, USER_AGENT


def _parse_orderbook_pairs(body_text: str) -> tuple[List[dict], List[dict]]:
    """body_text から Order Book の (volume, price) ペアを抽出する。

    Returns: (bids, asks) — それぞれ {"volume": int, "price": float} のリスト。
    """
    # "Order Book (Positions)" から次のセクションマーカーまでを切り出す
    start_marker = "Order Book (Positions)"
    end_markers = ["End of interactive chart.", "Forex Sentiment", "Top Forex Brokers"]

    s_idx = body_text.find(start_marker)
    if s_idx < 0:
        return [], []

    e_idx = len(body_text)
    for em in end_markers:
        em_idx = body_text.find(em, s_idx + len(start_marker))
        if em_idx > 0 and em_idx < e_idx:
            e_idx = em_idx
            break

    section = body_text[s_idx:e_idx]

    # volume(整数) + price(小数、カンマ区切り可) のペアを抽出
    # MyFXBook の order book は volume\nprice\nvolume\nprice... の形
    lines = [ln.strip() for ln in section.splitlines() if ln.strip()]

    pairs: List[tuple[int, float]] = []
    i = 0
    while i < len(lines) - 1:
        v_match = re.fullmatch(r"\d{1,4}", lines[i])
        p_match = re.fullmatch(r"[\d,]+\.\d{1,5}", lines[i + 1])
        if v_match and p_match:
            try:
                volume = int(v_match.group())
                price = float(p_match.group().replace(",", ""))
                pairs.append((volume, price))
                i += 2
                continue
            except ValueError:
                pass
        i += 1

    if not pairs:
        return [], []

    # MyFXBook の order book は前半 ~半分が Bids、後半 ~半分が Asks の慣例
    # ただし "Asks Price Bids" 軸ラベル順を見て調整する余地あり。
    # 安全策: 全ペアを返し、現在価格との上下で BSL/SSL 分類する側で扱う。
    half = len(pairs) // 2
    bids_raw = pairs[:half] if half > 0 else []
    asks_raw = pairs[half:]

    bids = [{"volume": v, "price": p} for v, p in bids_raw]
    asks = [{"volume": v, "price": p} for v, p in asks_raw]
    return bids, asks


def _extract_current_price(body_text: str, symbol: str) -> Optional[float]:
    """body_text から現在価格を抽出する。Mid-Market price セクションから推定。

    XAUUSD/USDJPY 等の outlook ページの Market Depth セクションには
    "Mid-Market price" の表記があり、その近辺の価格列の中央値を採用する。
    """
    # Market Depth セクション
    s = body_text.find("Market Depth")
    if s < 0:
        return None
    e = body_text.find("End of interactive chart.", s)
    if e < 0:
        e = len(body_text)
    section = body_text[s:e]

    # 数値パターン (4 桁以上.数桁、または 2-3 桁.数桁) を全部抜き、中央値を取る
    nums = re.findall(r"[\d,]+\.\d{2,5}", section)
    vals = []
    for n in nums:
        try:
            v = float(n.replace(",", ""))
            vals.append(v)
        except ValueError:
            continue
    if not vals:
        return None

    vals.sort()
    return vals[len(vals) // 2]


def _cluster_and_classify(
    bids: List[dict],
    asks: List[dict],
    current_price: Optional[float],
    cluster_pct: float = 0.005,
    max_distance_pct: float = 0.10,
) -> dict:
    """価格帯クラスタリング + BSL/SSL 分類。

    cluster_pct: 同じクラスタとみなす価格幅の割合（デフォルト 0.5%）。
    max_distance_pct: 現在価格からの許容乖離（デフォルト ±10%）。
        チャート軸ラベル等の混入による異常値（例: 現値 4411 に対し 5175 のクラスタ）を
        排除する妥当性フィルタ。実オーダーでもこの距離のものは当面の流動性として無意味。
    """
    all_pairs = [{"side": "bid", **b} for b in bids] + [{"side": "ask", **a} for a in asks]
    if not all_pairs or current_price is None:
        return {"bsl_candidates": [], "ssl_candidates": []}

    # 妥当性フィルタ: 現在価格 ±max_distance_pct の範囲外は除外
    all_pairs = [
        p for p in all_pairs
        if abs(p["price"] / current_price - 1.0) <= max_distance_pct
    ]
    if not all_pairs:
        return {"bsl_candidates": [], "ssl_candidates": []}

    above = [p for p in all_pairs if p["price"] > current_price]
    below = [p for p in all_pairs if p["price"] < current_price]

    def cluster(items: List[dict]) -> List[dict]:
        if not items:
            return []
        items_sorted = sorted(items, key=lambda x: x["price"])
        clusters: List[dict] = []
        for it in items_sorted:
            if clusters and abs(it["price"] - clusters[-1]["high"]) / max(it["price"], 1.0) < cluster_pct:
                # 既存クラスタに追加
                clusters[-1]["high"] = max(clusters[-1]["high"], it["price"])
                clusters[-1]["low"] = min(clusters[-1]["low"], it["price"])
                clusters[-1]["volume_sum"] += it["volume"]
                clusters[-1]["entries"] += 1
            else:
                clusters.append({
                    "low": it["price"],
                    "high": it["price"],
                    "volume_sum": it["volume"],
                    "entries": 1,
                })
        return clusters

    bsl_clusters = cluster(above)
    ssl_clusters = cluster(below)

    # share_pct: 同サイド（現在価格の上/下）の総ボリュームに占める割合
    def annotate_share(clusters: List[dict], side_pairs: List[dict]) -> None:
        side_total = sum(p["volume"] for p in side_pairs)
        for c in clusters:
            c["share_pct"] = round(c["volume_sum"] / side_total * 100, 1) if side_total > 0 else None

    annotate_share(bsl_clusters, above)
    annotate_share(ssl_clusters, below)

    # 集中度上位 3 件
    bsl_top = sorted(bsl_clusters, key=lambda c: c["volume_sum"], reverse=True)[:3]
    ssl_top = sorted(ssl_clusters, key=lambda c: c["volume_sum"], reverse=True)[:3]

    return {
        "bsl_candidates": bsl_top,
        "ssl_candidates": ssl_top,
    }


async def scrape_myfxbook_open_orders(symbol: str, current_price_hint: Optional[float] = None) -> dict:
    """MyFXBook の Order Book セクションから注文集中度を取得する。

    Args:
        symbol: 銘柄名 (例: "XAUUSD", "USDJPY")
        current_price_hint: TwelveData 等で取得済みの現在価格。
            ページ内 Market Depth 由来の推定中央値と 2% 以上乖離する場合は
            hint を優先する（ページ側パースの異常検知用クロスチェック）。

    Returns:
        {
            "source": "MyFXBook (Order Book Positions)",
            "symbol": str,
            "current_price": float | None,
            "bid_count": int,
            "ask_count": int,
            "bsl_candidates": [{"low": float, "high": float, "volume_sum": int,
                                "entries": int, "share_pct": float | None}, ...],
            "ssl_candidates": 同上,
            "bsl_concentration": dict | None,   # top-1 互換用
            "ssl_concentration": dict | None,   # top-1 互換用
            "summary": {"bid": int, "ask": int},  # 互換用
            "buckets": [],  # 旧スキーマ互換 (空 list、後方互換のため残置)
            "note": str | None,
            "error": str | None,
        }
    """
    result = {
        "source": "MyFXBook (Order Book Positions)",
        "symbol": symbol,
        "current_price": None,
        "bid_count": 0,
        "ask_count": 0,
        "bsl_candidates": [],
        "ssl_candidates": [],
        "bsl_concentration": None,
        "ssl_concentration": None,
        "summary": {},
        "buckets": [],
        "note": None,
        "error": None,
    }

    url = f"https://www.myfxbook.com/community/outlook/{symbol}"

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=USER_AGENT, ignore_https_errors=True)
            page = await context.new_page()

            await page.goto(url, timeout=BROWSER_TIMEOUT, wait_until="domcontentloaded")
            # Order Book canvas の描画完了を待つ
            await page.wait_for_timeout(10000)

            body_text = await page.inner_text("body")
            await browser.close()
    except Exception as exc:
        result["error"] = f"ページ取得失敗: {exc}"
        return result

    bids, asks = _parse_orderbook_pairs(body_text)
    result["bid_count"] = len(bids)
    result["ask_count"] = len(asks)
    result["summary"] = {"bid": len(bids), "ask": len(asks)}

    if not bids and not asks:
        result["error"] = "Order Book ペアを抽出できず (DOM 変更の可能性)"
        return result

    current_price = _extract_current_price(body_text, symbol)

    # クロスチェック: ページ由来の推定値と hint が 2% 以上乖離したら hint を採用
    if current_price_hint is not None:
        if current_price is None:
            current_price = current_price_hint
            result["note"] = "現在価格はページから抽出できず、外部ヒント値を使用"
        elif abs(current_price / current_price_hint - 1.0) > 0.02:
            result["note"] = (
                f"ページ推定価格 {current_price} が外部ヒント {current_price_hint} と "
                f"2% 以上乖離 → ヒント値を採用"
            )
            current_price = current_price_hint

    result["current_price"] = current_price

    if current_price is None:
        result["error"] = "現在価格を抽出できず、BSL/SSL 分類スキップ"
        return result

    cls = _cluster_and_classify(bids, asks, current_price)
    result["bsl_candidates"] = cls["bsl_candidates"]
    result["ssl_candidates"] = cls["ssl_candidates"]

    # 旧スキーマ互換 (top-1 を bsl_concentration / ssl_concentration に格納、type は便宜上のラベル)
    if cls["bsl_candidates"]:
        b = cls["bsl_candidates"][0]
        result["bsl_concentration"] = {
            "type": "Ask cluster (BSL candidate)",
            "low": b["low"],
            "high": b["high"],
            "share_pct": b.get("share_pct"),
            "volume_sum": b["volume_sum"],
        }
    if cls["ssl_candidates"]:
        s = cls["ssl_candidates"][0]
        result["ssl_concentration"] = {
            "type": "Bid cluster (SSL candidate)",
            "low": s["low"],
            "high": s["high"],
            "share_pct": s.get("share_pct"),
            "volume_sum": s["volume_sum"],
        }

    return result


# ---- 旧スキーマ互換ヘルパー (テスト用) ----
def _summarize_buckets(buckets: List[dict]) -> dict:
    """旧スキーマで {type, low, high, share_pct} のリストを受け取り、BSL/SSL 集中帯を返す。

    後方互換のため残置 (test_open_orders_summarizer_picks_max_concentration が使用)。
    新パーサーは _parse_orderbook_pairs + _cluster_and_classify を使う。
    """
    if not buckets:
        return {
            "summary": {},
            "bsl_concentration": None,
            "ssl_concentration": None,
        }

    summary: dict = {}
    for b in buckets:
        cat = b["type"]
        summary.setdefault(cat, []).append(b)

    bsl_max = None
    ssl_max = None
    for b in buckets:
        if "Buy" in b["type"] and "Stop" in b["type"]:
            if bsl_max is None or b["share_pct"] > bsl_max["share_pct"]:
                bsl_max = b
        if "Sell" in b["type"] and "Stop" in b["type"]:
            if ssl_max is None or b["share_pct"] > ssl_max["share_pct"]:
                ssl_max = b

    return {
        "summary": {k: len(v) for k, v in summary.items()},
        "bsl_concentration": bsl_max,
        "ssl_concentration": ssl_max,
    }


if __name__ == "__main__":
    async def _test():
        for sym in ["XAUUSD", "USDJPY"]:
            d = await scrape_myfxbook_open_orders(sym)
            print(f"\n--- {sym} ---")
            print(f"  source: {d['source']} | error: {d['error']}")
            print(f"  current_price: {d['current_price']}")
            print(f"  bids: {d['bid_count']} / asks: {d['ask_count']}")
            print(f"  BSL candidates ({len(d['bsl_candidates'])}):")
            for b in d['bsl_candidates']:
                print(f"    {b}")
            print(f"  SSL candidates ({len(d['ssl_candidates'])}):")
            for s in d['ssl_candidates']:
                print(f"    {s}")
    asyncio.run(_test())
