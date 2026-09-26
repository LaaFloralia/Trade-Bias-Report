"""公開RSSのニュース見出しを集め、Jev で金の材料として関連するものを選ぶ

取得元は規約上の問題が小さい公開RSSだけ（FXStreet・Investing.com・ECB・FRB）。
1回あたり数十〜百件の見出しを、typesafe-ai の triage（見出しごとに「金の価格材料を
具体的に扱っているか」を Jev が確率で返す）で順位付けし、閾値以上と上位数件を残す。
Jev が使えないときはキーワード規則に戻り、選別方法を必ず記録する（fail-open）。

見出しは原文未確認の手掛かりであり、方向・事実の確定には使わない。
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

import requests

JST = timezone(timedelta(hours=9))
FEEDS = (
    ("FXStreet", "https://www.fxstreet.com/rss/news"),
    ("Investing.com 商品", "https://www.investing.com/rss/news_11.rss"),
    ("Investing.com 経済", "https://www.investing.com/rss/news_95.rss"),
    ("ECB", "https://www.ecb.europa.eu/rss/press.html"),
    ("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml"),
)
QUESTION = ("Does this headline specifically report news that can move the gold (XAU/USD) price: gold itself, "
            "Federal Reserve policy or US interest rates, the US dollar, US inflation or jobs data, "
            "geopolitical threats or conflicts, or central bank gold buying?")
TRIAGE = "/Users/laa/.agents/skills/typesafe-ai/scripts/run.sh"
THRESHOLD = 0.5
KEEP_MIN, KEEP_MAX = 5, 25
BUDGET_USD = "0.20"  # 共有台帳の UTC 日次上限。1回の選別は約0.0005ドル（9/26実測）
FALLBACK = re.compile(r"\bgold\b|\bbullion\b|\bXAU|\bFed\b|FOMC|Powell|yield|Treasur|dollar|\bDXY\b|inflation|CPI|PCE|"
                      r"payroll|jobs|tariff|geopolit|war|missile|sanction|central bank", re.I)
MAX_BYTES = 1_000_000


def _stamp(text: str) -> Optional[datetime]:
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else None


def parse_feed(body: bytes, source: str, start: datetime, now: datetime) -> list[dict]:
    """RSS/Atom の item を {source,title,url,published} にする。DTD は拒否する。"""
    if b"<!DOCTYPE" in body[:2000].upper() or b"<!ENTITY" in body.upper():
        raise ValueError("DTD not allowed")
    root = ET.fromstring(body)
    items = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] not in {"item", "entry"}:
            continue
        fields = {child.tag.rsplit("}", 1)[-1]: child for child in node}

        def text(*names):
            for name in names:
                child = fields.get(name)
                if child is not None:
                    return (child.text or child.get("href") or "").strip()
            return ""

        published = _stamp(text("pubDate", "date", "published", "updated"))
        title = " ".join(html.unescape(text("title")).split())
        url = text("link")
        if not title or not url.startswith("https://") or published is None:
            continue
        if not (start <= published <= now):
            continue
        items.append({"source": source, "title": title[:300], "url": url, "published": published.isoformat(),
                      "text": " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text("description", "summary"))).split())[:600]})
    return items


def collect_headlines(now: datetime, lookback_hours: int, fetch=None) -> tuple[list[dict], list[dict]]:
    """全フィードを取得し、同じ見出しを除いた候補と、取得元ごとの状況を返す。"""
    start = now - timedelta(hours=lookback_hours)
    fetch = fetch or (lambda url: requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"}))
    seen, candidates, sources = set(), [], []
    for name, url in FEEDS:
        record = {"source": name, "url": url, "status": "unavailable", "items": 0}
        try:
            resp = fetch(url)
            record["http_status"] = resp.status_code
            if resp.status_code == 200 and len(resp.content) <= MAX_BYTES:
                items = parse_feed(resp.content, name, start, now)
                fresh = [i for i in items if i["title"].lower() not in seen]
                seen.update(i["title"].lower() for i in fresh)
                candidates.extend(fresh)
                record.update(status="ok", items=len(fresh))
        except Exception as e:  # noqa: BLE001 — 取得元単位で失敗を記録し、他は続ける
            record["error"] = type(e).__name__
        sources.append(record)
    for i, c in enumerate(candidates):
        c["id"] = f"n{i + 1}"
    return candidates, sources


def jev_triage(candidates: list[dict], runner=None, timeout: int = 60) -> tuple[Optional[dict], dict]:
    """{id: p} と記録を返す。失敗時は (None, 記録) で、呼び出し側がキーワード規則に戻す。"""
    if os.environ.get("LAA_JEV_DISABLED") == "1":
        return None, {"mode": "fallback", "reason": "disabled"}
    if not candidates:
        return {}, {"mode": "skipped", "reason": "no_candidates"}
    run = runner or subprocess.run
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="xau-news-") as tmp:
            src = Path(tmp) / "candidates.json"
            src.write_text(json.dumps({"candidates": [
                {"id": c["id"], "url": c["url"], "title": c["title"], "text": c.get("text", ""),
                 "author": c["source"], "date": c["published"], "kind": "news_headline"} for c in candidates]},
                ensure_ascii=False))
            proc = run(["/bin/bash", TRIAGE, "triage", "--data-class", "public", "--question", QUESTION,
                        "--input", str(src), "--out-dir", tmp, "--threshold", str(THRESHOLD),
                        "--keep-min", "0", "--keep-max", str(len(candidates)), "--budget-usd", BUDGET_USD,
                        "--timeout", "10"],
                       capture_output=True, timeout=timeout, check=False,
                       env={"HOME": str(Path.home()), "USER": os.environ.get("USER", "laa"),
                            "PATH": "/opt/homebrew/bin:/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"})
            if proc.returncode:
                return None, {"mode": "fallback", "reason": "triage_failed"}
            data = json.loads((Path(tmp) / "triage.json").read_text())
    except subprocess.TimeoutExpired:
        return None, {"mode": "fallback", "reason": "timeout"}
    except (OSError, ValueError, TypeError, KeyError):
        return None, {"mode": "fallback", "reason": "triage_unreadable"}
    report = data.get("report") if isinstance(data, dict) else None
    if not isinstance(report, dict) or report.get("mode") != "jev":
        reason = str((report or {}).get("fallback") or "jev_unavailable")
        return None, {"mode": "fallback", "reason": reason if re.fullmatch(r"[a-z_]{1,48}", reason) else "jev_unavailable"}
    scores = {}
    for row in data.get("candidates") or []:
        p = row.get("p") if isinstance(row, dict) else None
        if isinstance(p, (int, float)) and not isinstance(p, bool) and 0 <= p <= 1:
            scores[str(row.get("id"))] = float(p)
    return scores, {"mode": "jev", "model": report.get("model"), "input_tokens": report.get("input_tokens"),
                    "elapsed_ms": round((time.monotonic() - started) * 1000)}


def select_headlines(candidates: list[dict], scores: Optional[dict]) -> list[dict]:
    """コードの規則: Jev の確率が閾値以上、少なくとも上位 KEEP_MIN 件、最大 KEEP_MAX 件。"""
    if scores is None:
        pool = [dict(c, p=None) for c in candidates if FALLBACK.search(c["title"] + " " + c.get("text", ""))]
        return sorted(pool, key=lambda c: c["published"], reverse=True)[:KEEP_MAX]
    ranked = sorted((dict(c, p=scores.get(c["id"])) for c in candidates),
                    key=lambda c: (c["p"] or 0, c["published"]), reverse=True)
    kept = [c for c in ranked if (c["p"] or 0) >= THRESHOLD]
    if len(kept) < KEEP_MIN:
        kept = ranked[:KEEP_MIN]
    return kept[:KEEP_MAX]


def build_news(now: datetime, lookback_hours: int, fetch=None, runner=None) -> dict:
    candidates, sources = collect_headlines(now, lookback_hours, fetch)
    scores, report = jev_triage(candidates, runner)
    kept = select_headlines(candidates, scores)
    return {"window_hours": lookback_hours, "sources": sources, "candidate_count": len(candidates),
            "kept": [{k: c[k] for k in ("source", "title", "url", "published", "p")} for c in kept],
            "selection": report}


def format_news_lines(news: Optional[dict]) -> list[str]:
    lines = ["### ニュース見出し（公開RSS・Jev選別）"]
    if not isinstance(news, dict):
        lines.append("- 取得不可")
        return lines
    sel = news.get("selection") or {}
    ok = [s["source"] for s in news.get("sources", []) if s.get("status") == "ok"]
    ng = [s["source"] for s in news.get("sources", []) if s.get("status") != "ok"]
    method = (f"Jev（{sel.get('model')}、応答{sel.get('elapsed_ms')}ms、入力{sel.get('input_tokens')}トークン）で"
              f"「金の価格材料を具体的に扱うか」を判定し、確率{THRESHOLD}以上を採用"
              if sel.get("mode") == "jev" else f"キーワード規則（Jev不使用: {sel.get('reason')}）")
    lines.append(f"- 候補 {news.get('candidate_count', 0)} 件（直近{news.get('window_hours')}時間、取得元: {'・'.join(ok) or 'なし'}"
                 f"{'／取得失敗: ' + '・'.join(ng) if ng else ''}）→ 採用 {len(news.get('kept') or [])} 件。選別: {method}")
    for c in news.get("kept") or []:
        when = datetime.fromisoformat(c["published"]).astimezone(JST).strftime("%m/%d %H:%M")
        p = f"p={c['p']:.2f} " if isinstance(c.get("p"), float) else ""
        lines.append(f"- {when} JST {p}[{c['source']}] {c['title']} — {c['url']}")
    lines.append("※ 見出しのみ・本文未確認。p は金の材料として関連する確率で、方向や重要度ではない。"
                 "方向の判断は原文と金・金利・ドルの実際の反応で確認する")
    return lines
