"""Weekly 前回レビュー入力の組み立て（interactive / headless 共通層）

2026-08-11 の headless Weekly で「セクション1 の大半が照合不能」となった原因は、
前回レビュー入力（前回 Weekly 本文・直近 Daily 本文・intel JSON 群）の Read が
.claude/commands/weekly-bias.md（interactive 専用）にしか存在しなかったこと。

本モジュールは main.py --weekly の実行時にこれらを組み立てて scraped_data に
`### 前回レビュー入力（前回想定との答え合わせ用）` ブロックとして注入する。
これにより interactive / headless の両フローが同一の照合材料を得る。
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from scrapers.report_anchor import _extract_section, _latest_report, WEEKLY_DIRS

PREV_WEEKLY_CHARS = 3000
DAILY_CHARS = 1200
MAX_DAILIES = 2
DAILY_LOOKBACK_DAYS = 7
MAX_INTEL_JSONS = 5

_DATE_RE = re.compile(r"_(\d{4}-\d{2}-\d{2})\.md$")

BLOCK_HEADER = "### 前回レビュー入力（前回想定との答え合わせ用）"


def _resolve_brain_path() -> Path:
    return Path(os.environ.get("BRAIN_PATH") or (Path.home() / "Brain"))


def _extract_weekly_excerpt(text: str) -> str:
    """前回 Weekly からセクション0（サマリー）と銘柄別バイアス/シナリオ系を抜粋する。"""
    parts = []
    s0 = _extract_section(text, ["セクション0", "エグゼクティブサマリー"], max_chars=PREV_WEEKLY_CHARS // 2)
    if s0:
        parts.append(f"[前回 Weekly セクション0]\n{s0}")
    bias = _extract_section(text, ["銘柄別週次バイアス", "セクション7"], max_chars=PREV_WEEKLY_CHARS // 2)
    if bias:
        parts.append(f"[前回 Weekly 銘柄別バイアス]\n{bias}")
    scenario = _extract_section(text, ["注目シナリオ", "セクション8"], max_chars=PREV_WEEKLY_CHARS // 2)
    if scenario:
        parts.append(f"[前回 Weekly シナリオ]\n{scenario}")
    return "\n".join(parts)


def _extract_daily_excerpt(text: str) -> str:
    parts = []
    s0 = _extract_section(text, ["セクション0", "エグゼクティブサマリー"], max_chars=DAILY_CHARS // 2)
    if s0:
        parts.append(s0)
    s8 = _extract_section(text, ["前回照合", "セクション8"], max_chars=DAILY_CHARS // 2)
    if s8:
        parts.append(s8)
    return "\n".join(parts)


def _recent_dailies(brain: Path, today: date) -> list[Path]:
    """直近 DAILY_LOOKBACK_DAYS 日以内の Daily レポートを新しい順に最大 MAX_DAILIES 本。
    非再帰 glob のため銘柄別サブディレクトリ（Daily-Bias/USDJPY/ 等）は対象外。"""
    d = brain / "Calendar" / "Daily-Bias"
    if not d.is_dir():
        return []
    candidates = []
    for f in d.glob("*.md"):
        m = _DATE_RE.search(f.name)
        if not m:
            continue
        try:
            fdate = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        age = (today - fdate).days
        if 0 < age <= DAILY_LOOKBACK_DAYS:
            candidates.append((fdate, f))
    candidates.sort(reverse=True)
    return [f for _, f in candidates[:MAX_DAILIES]]


def _recent_intel_jsons(intel_dir: Path) -> list[Path]:
    if not intel_dir.is_dir():
        return []
    files = sorted(intel_dir.glob("intel_*.json"))
    return files[-MAX_INTEL_JSONS:]


def build_weekly_review_block(
    brain: Optional[Path] = None,
    intel_dir: Optional[Path] = None,
    today: Optional[date] = None,
) -> Optional[str]:
    """前回レビュー入力ブロックを組み立てる。材料が 1 つもなければ None。"""
    brain = brain or _resolve_brain_path()
    intel_dir = intel_dir or (Path(__file__).parent.parent / "output" / "intel")
    today = today or date.today()

    lines = [BLOCK_HEADER]
    found_any = False

    # 1. 前回 Weekly（today より前）
    try:
        prev = _latest_report(brain, WEEKLY_DIRS, before=today)
    except Exception:  # noqa: BLE001
        prev = None
    if prev is not None:
        path, fdate, kind = prev
        excerpt = _extract_weekly_excerpt(path.read_text(encoding="utf-8"))
        if excerpt:
            age = (today - fdate).days
            lines.append(f"[前回 Weekly] {path.name}（{age}日前・{kind}）")
            lines.append(excerpt)
            lines.append("")
            found_any = True

    # 2. 直近 Daily（最大 2 本）
    for f in _recent_dailies(brain, today):
        excerpt = _extract_daily_excerpt(f.read_text(encoding="utf-8"))
        if excerpt:
            lines.append(f"[直近 Daily] {f.name}")
            lines.append(excerpt)
            lines.append("")
            found_any = True

    # 3. intel JSON 群（小さいので原文）
    intel_files = _recent_intel_jsons(intel_dir)
    if intel_files:
        lines.append(f"[機械用 intel JSON 直近 {len(intel_files)} 件]（bias / no_trade / confidence の推移確認用）")
        for f in intel_files:
            try:
                obj = json.loads(f.read_text(encoding="utf-8"))
                compact = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
                lines.append(f"- {f.name}: {compact}")
                found_any = True
            except (json.JSONDecodeError, OSError):
                lines.append(f"- {f.name}: 読み込み不可")
        lines.append("")

    return "\n".join(lines).rstrip() if found_any else None


if __name__ == "__main__":
    block = build_weekly_review_block()
    print(block or "(前回レビュー入力なし)")
