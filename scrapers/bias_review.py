"""Bias-Review-Log — レポート振り返りの構造化ナレッジベース

「前回の予測と実際の結果を照合し、どこが合っていて、どういう視点が抜けていたか」を
PDF 本文には載せず（本文はコンパクトな照合のみ）、AI 側のナレッジとして蓄積する。
直近エントリは report_anchor 経由で次回レポートのプロンプトに再注入され、
抜けていた視点が次回の分析で継承される学習ループを構成する。

保存先: $BRAIN_PATH/Atlas/Bias-Review-Log.md（append-only、同一日・同一モードは置換）

エントリ標準形式（LLM / コマンドはこの形式で生成すること）:

    ## 2026-08-12 Daily
    - 判定: 当たり | 外れ | 未決着 | 照合不能
    - 前回想定: Bullish / High (7点) / 注目ゾーン 4,390-4,410
    - 実際: +0.6%。BSL 4,438 sweep→reversal（リテール分析検出）
    - 外し要因: （外れ・未決着時のみ。それ以外は「-」）
    - 学び: （次回に継承すべき視点を 1〜2 行。抜けていた視点を優先）
    <!-- review-json: {"date": "2026-08-12", "mode": "daily", "verdict": "hit"} -->

verdict の機械値: hit（当たり）/ miss（外れ）/ open（未決着）/ n/a（照合不能）。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

LOG_RELPATH = Path("Atlas") / "Bias-Review-Log.md"
MAX_INJECT_CHARS = 2000  # report_anchor 注入時の上限

FILE_HEADER = """# Bias Review Log

チャート外分析レポート（Daily / Weekly Bias）の振り返り蓄積。生成パイプラインが自動追記する。
形式の定義は `~/dev/fundamental-macro-analysis/scrapers/bias_review.py` を参照。
直近エントリは次回レポート生成時にプロンプトへ再注入される（学習ループ）。
"""

_ENTRY_HEAD_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2}) (Daily|Weekly)\s*$", re.MULTILINE)
_REVIEW_JSON_RE = re.compile(r"<!--\s*review-json:\s*(\{.*?\})\s*-->", re.DOTALL)


def _log_path(path: Optional[Path] = None) -> Path:
    if path is not None:
        return Path(path)
    brain = Path(os.environ.get("BRAIN_PATH") or (Path.home() / "Brain"))
    return brain / LOG_RELPATH


def validate_entry(entry_md: str, entry_date: str, mode: str) -> list[str]:
    """エントリ形式の検証。違反メッセージのリスト（空 = 合格）。"""
    errors = []
    mode_label = mode.capitalize()
    if not entry_md.strip().startswith(f"## {entry_date} {mode_label}"):
        errors.append(f"見出しが `## {entry_date} {mode_label}` で始まっていない")
    for field in ("- 判定:", "- 前回想定:", "- 実際:", "- 学び:"):
        if field not in entry_md:
            errors.append(f"必須フィールド欠落: {field}")
    m = _REVIEW_JSON_RE.search(entry_md)
    if not m:
        errors.append("review-json コメント欠落")
    else:
        try:
            obj = json.loads(m.group(1))
            if obj.get("verdict") not in ("hit", "miss", "open", "n/a"):
                errors.append(f"verdict が不正: {obj.get('verdict')}")
        except json.JSONDecodeError:
            errors.append("review-json が JSON としてパース不能")
    return errors


def extract_verdict(entry_md: str) -> Optional[str]:
    m = _REVIEW_JSON_RE.search(entry_md)
    if not m:
        return None
    try:
        return json.loads(m.group(1)).get("verdict")
    except json.JSONDecodeError:
        return None


def _split_entries(text: str) -> tuple[str, list[tuple[str, str, str]]]:
    """ログ本文を (ヘッダ部, [(date, mode, entry_md)]) に分割する。"""
    matches = list(_ENTRY_HEAD_RE.finditer(text))
    if not matches:
        return text, []
    header = text[: matches[0].start()]
    entries = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        entries.append((m.group(1), m.group(2), text[m.start():end].rstrip() + "\n"))
    return header, entries


def append_entry(entry_md: str, entry_date: str, mode: str,
                 path: Optional[Path] = None) -> Path:
    """エントリを追記する。同一日・同一モードの既存エントリは置換（再生成安全）。"""
    log_path = _log_path(path)
    mode_label = mode.capitalize()

    if log_path.exists():
        text = log_path.read_text(encoding="utf-8")
    else:
        text = FILE_HEADER
    header, entries = _split_entries(text)

    entries = [e for e in entries if not (e[0] == entry_date and e[1] == mode_label)]
    entries.append((entry_date, mode_label, entry_md.strip() + "\n"))

    body = "\n".join(e[2].rstrip() + "\n" for e in entries)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(header.rstrip() + "\n\n" + body, encoding="utf-8")
    return log_path


def load_recent_entries(n: int = 5, path: Optional[Path] = None) -> Optional[str]:
    """直近 n 件のエントリを返す（プロンプト注入用、MAX_INJECT_CHARS で截断）。"""
    log_path = _log_path(path)
    if not log_path.exists():
        return None
    _, entries = _split_entries(log_path.read_text(encoding="utf-8"))
    if not entries:
        return None
    recent = entries[-n:]
    text = "\n".join(e[2].rstrip() for e in recent)
    if len(text) > MAX_INJECT_CHARS:
        text = text[-MAX_INJECT_CHARS:]
        # 截断で先頭エントリが壊れた場合は次の見出しから始める
        m = _ENTRY_HEAD_RE.search(text)
        if m:
            text = text[m.start():]
    return text or None
