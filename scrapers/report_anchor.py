"""前回レポート アンカー ローダー (Brain/Calendar のローカルファイル読み込み)

Weekly / Deep レポートは定期実行されず、オンデマンド運用になった (2026-07)。
その前提で Daily を単体で自己完結させるため、生成時に以下を自動差し込みする:

  1. Weekly 大局アンカー: Weekly-Deep-Bias / Weekly-Bias の最新レポートの
     エグゼクティブサマリー (セクション0 / W0)。経過日数付き。
     9 日超は [STALE] を付け、プロンプト側で参考扱い (スコア #4 に使わない)。
  2. 前回 Daily アンカー: 直近 (当日より前) の Daily レポートの
     セクション1.5 (ファンダ大局バイアス)。無ければセクション0 にフォールバック。
     レポート間に記憶がないため、大局バイアスの急変検知に使う。

パス解決: 環境変数 BRAIN_PATH > $HOME/Brain (daily-bias.md コマンドと同じ規約)。
ローカルファイル読み込みのみでネットワークに出ない。Brain が無い環境
(Routines の clone 前など) では note を残して None を返す (graceful degradation)。
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional

# (ディレクトリ名, 種別ラベル)。同日付なら先頭ほど優先 (Deep 版の方が情報量が多い)
WEEKLY_DIRS = [
    ("Calendar/Weekly-Deep-Bias", "Weekly-Deep"),
    ("Calendar/Weekly-Bias", "Weekly"),
]
DAILY_DIRS = [
    ("Calendar/Daily-Bias", "Daily"),
]

WEEKLY_STALE_DAYS = 9   # 週次アンカーの許容鮮度 (1 週間 + 猶予)
DAILY_STALE_DAYS = 3    # 前回 Daily の許容鮮度
MAX_SUMMARY_CHARS = 1200

_DATE_RE = re.compile(r"_(\d{4}-\d{2}-\d{2})\.md$")


def _resolve_brain_path() -> Path:
    return Path(os.environ.get("BRAIN_PATH") or (Path.home() / "Brain"))


def _latest_report(
    brain: Path,
    dirs: list[tuple[str, str]],
    before: Optional[date] = None,
) -> Optional[tuple[Path, date, str]]:
    """dirs 内の `*_YYYY-MM-DD.md` から最新 (date, 優先ディレクトリ順) を返す。
    before 指定時はその日付より前のみ対象 (前回 Daily 用)。"""
    best: Optional[tuple[date, int, Path, str]] = None
    for priority, (rel, kind) in enumerate(dirs):
        d = brain / rel
        if not d.is_dir():
            continue
        for f in d.glob("*.md"):
            m = _DATE_RE.search(f.name)
            if not m:
                continue
            try:
                fdate = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            except ValueError:
                continue
            if before is not None and fdate >= before:
                continue
            # 日付降順 → 優先ディレクトリ昇順
            key = (fdate, -priority)
            if best is None or key > (best[0], -best[1]):
                best = (fdate, priority, f, kind)
    if best is None:
        return None
    return best[2], best[0], best[3]


def _extract_section(text: str, patterns: list[str]) -> Optional[str]:
    """`## ` 見出しに patterns のいずれかを含むセクション本文を抽出する。
    次の `## ` 見出しまでを取り、区切りの `---` は除外。"""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith("## ") and any(p in line for p in patterns):
            start = i
            break
    if start is None:
        return None
    body = []
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        if line.strip() == "---":
            continue
        body.append(line)
    section = "\n".join(body).strip()
    if not section:
        return None
    if len(section) > MAX_SUMMARY_CHARS:
        section = section[:MAX_SUMMARY_CHARS] + "…(截断)"
    return section


def _build_anchor(
    found: tuple[Path, date, str],
    today: date,
    stale_days: int,
    patterns: list[str],
    fallback_patterns: Optional[list[str]] = None,
) -> dict:
    path, fdate, kind = found
    text = path.read_text(encoding="utf-8")
    summary = _extract_section(text, patterns)
    used = patterns[0]
    if summary is None and fallback_patterns:
        summary = _extract_section(text, fallback_patterns)
        used = fallback_patterns[0]
    age = (today - fdate).days
    return {
        "file": path.name,
        "kind": kind,
        "date": fdate.isoformat(),
        "age_days": age,
        "stale": age > stale_days,
        "section_used": used if summary else None,
        "summary": summary,
    }


def load_report_anchor(today: Optional[date] = None) -> dict:
    """前回レポートのアンカーを読み込む。

    Returns:
        {
            "source": "Brain/Calendar (前回レポート)",
            "weekly": dict | None,      # 最新 Weekly 系のセクション0 (経過日数・stale 付き)
            "prev_daily": dict | None,  # 当日より前の最新 Daily のセクション1.5 (無ければ0)
            "note": str | None,
            "error": str | None,
        }
    """
    today = today or date.today()
    base = {"source": "Brain/Calendar (前回レポート)", "weekly": None, "prev_daily": None,
            "note": None, "error": None}
    brain = _resolve_brain_path()
    if not brain.is_dir():
        base["note"] = f"Brain パスが存在しない ({brain})。アンカーなしで続行"
        return base
    try:
        weekly_found = _latest_report(brain, WEEKLY_DIRS)
        if weekly_found:
            base["weekly"] = _build_anchor(
                weekly_found, today, WEEKLY_STALE_DAYS,
                patterns=["セクション0", "W0", "エグゼクティブサマリー"],
            )
        daily_found = _latest_report(brain, DAILY_DIRS, before=today)
        if daily_found:
            base["prev_daily"] = _build_anchor(
                daily_found, today, DAILY_STALE_DAYS,
                patterns=["セクション1.5", "ファンダメンタル大局"],
                fallback_patterns=["セクション0", "エグゼクティブサマリー"],
            )
    except Exception as e:  # noqa: BLE001 — アンカー失敗でパイプラインを止めない
        base["error"] = f"{type(e).__name__}: {e}"
    if base["weekly"] is None and base["prev_daily"] is None and base["error"] is None:
        base["note"] = base["note"] or "Brain/Calendar に参照可能なレポートが見つからない"
    return base


def format_anchor_lines(anchor: dict) -> list[str]:
    """format_scraped_data 用の出力行を組み立てる。"""
    lines = ["### 前回レポート アンカー (Brain 自動読み込み)"]
    if anchor.get("error"):
        lines.append(f"取得不可（{anchor['error']}）")
        return lines
    if anchor.get("note") and not (anchor.get("weekly") or anchor.get("prev_daily")):
        lines.append(f"※ {anchor['note']}")
        return lines

    def _emit(label: str, a: Optional[dict], stale_days: int):
        if not a:
            lines.append(f"[{label}] なし")
            return
        stale_tag = f" [STALE >{stale_days}日: 参考扱い]" if a["stale"] else ""
        lines.append(f"[{label}] {a['file']} ({a['age_days']}日前){stale_tag}")
        if a.get("summary"):
            lines.append(a["summary"])
        else:
            lines.append("(サマリーセクションを抽出できず)")

    _emit("Weekly 大局", anchor.get("weekly"), WEEKLY_STALE_DAYS)
    lines.append("")
    _emit("前回 Daily", anchor.get("prev_daily"), DAILY_STALE_DAYS)
    return lines


if __name__ == "__main__":
    a = load_report_anchor()
    print("\n".join(format_anchor_lines(a)))
