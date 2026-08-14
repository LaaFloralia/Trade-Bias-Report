"""統一信頼度スコアの機械検証 — セクション0 の表記を内訳表と一致させる。

master_prompt は「内訳を再集計した値がセクション0 と食い違う場合は、再計算値を正として
セクション0 を書き換えてから出力を確定する」と定めているが、LLM は代わりに末尾へ
「セクション0 は N と読み替えること」という注記を足して済ませることがある
（2026-08-14 Daily で発生）。セクション0 の信頼度は PDF 表紙バッジと intel JSON の
confidence 換算に使われるため、1 ページ目だけを見る運用では誤った信頼度で執行判断に
つながる。ここで機械的に訂正して、その事実を run ログに残す。

対象フォーマット:
    Daily  : `信頼度: Med ｜ スコア 5 ｜ NO-TRADE: なし`
    Weekly : `信頼度: Med-cautious（プラン1 のスコア合計 3 点。内訳はセクション8）`

内訳表は Daily = セクション1-3 / Weekly = セクション8 にあり、いずれも
`| <1〜8> | 項目 | 判定 | 点 |` の 8 行で構成される（最初に見つかった完全な表を使う）。
"""

from __future__ import annotations

import re
from typing import Optional

# 合計 → 判定（master_prompt.md「判定（Daily / Weekly 完全共通）」と同一）
SCORE_THRESHOLDS: tuple[tuple[int, str], ...] = (
    (7, "High"),
    (5, "Med"),
    (3, "Med-cautious"),
)
LOW_LABEL = "Low"
VALID_LABELS = ("High", "Med-cautious", "Med", "Low")

# `| 3 | XAU-TF構造整合 | D1 上昇 / H4 混在 | 0 |`
_SCORE_ROW_RE = re.compile(r"^\|\s*([1-8])\s*\|(?P<body>[^|]*\|[^|]*)\|\s*(?P<pt>[+-]?\d+)\s*\|\s*$")
_CONFIDENCE_LINE_RE = re.compile(r"信頼度\s*[:：]")
_LABEL_RE = re.compile(r"(High|Med-cautious|Med|Low)")
_SCORE_NUM_RE = re.compile(r"(スコア(?:\s*合計)?\s*)(\d+)")
# `合計 4+... 再集計: 0+2+0+1+1+0-1+1 = **4** → 判定 **Med-cautious**`
_RECALC_LINE_RE = re.compile(r"^\s*合計.*再集計")
# `*(注: セクション0 の1行目は ... と読み替えること。)*`
_READ_AS_NOTE_RE = re.compile(r"読み替え")


def label_for_score(total: int) -> str:
    """合計点から信頼度ラベルを返す。"""
    for threshold, label in SCORE_THRESHOLDS:
        if total >= threshold:
            return label
    return LOW_LABEL


def extract_score_table_total(md_text: str) -> Optional[int]:
    """最初に現れる「1〜8 が揃った内訳表」の合計点を返す。無ければ None。"""
    current: dict[int, int] = {}
    for line in md_text.splitlines():
        m = _SCORE_ROW_RE.match(line)
        if m:
            idx = int(m.group(1))
            # 同じ番号が再出現したら別の表に入ったとみなして作り直す
            if idx in current:
                current = {}
            current[idx] = int(m.group("pt"))
            if len(current) == 8:
                return sum(current.values())
        elif current and not line.lstrip().startswith("|"):
            # 表が途切れた（不完全）→ リセットして次の表を探す
            current = {}
    return None


def _find_section0_confidence_line(md_text: str) -> Optional[int]:
    """セクション0 内の「信頼度:」行の行番号を返す。"""
    lines = md_text.splitlines()
    in_section0 = False
    for i, line in enumerate(lines):
        if line.startswith("## "):
            if in_section0:
                break
            in_section0 = "セクション0" in line
            continue
        if in_section0 and _CONFIDENCE_LINE_RE.search(line):
            return i
    return None


def extract_declared(md_text: str) -> tuple[Optional[str], Optional[int]]:
    """セクション0 が宣言している (ラベル, スコア) を返す。"""
    idx = _find_section0_confidence_line(md_text)
    if idx is None:
        return None, None
    line = md_text.splitlines()[idx]
    label_m = _LABEL_RE.search(line)
    score_m = _SCORE_NUM_RE.search(line)
    return (label_m.group(1) if label_m else None,
            int(score_m.group(2)) if score_m else None)


def enforce_score_consistency(md_text: str) -> tuple[str, dict]:
    """セクション0 の信頼度・スコアを内訳表の再集計値に合わせる。

    Returns:
        (訂正後の MD, レポート dict)

    レポート:
        status         : "ok" | "corrected" | "skipped" | "needs_regeneration"
        table_total    : 内訳表の合計（取れなければ None）
        declared_score : セクション0 のスコア表記
        declared_label : セクション0 の信頼度ラベル
        expected_label : 合計から導かれる正しいラベル
        residue_cleaned: 削除・正規化した行数（読み替え注記・再集計の生成残骸）
        warning        : 自動訂正しなかった理由
    """
    report: dict = {
        "status": "skipped",
        "table_total": None,
        "declared_score": None,
        "declared_label": None,
        "expected_label": None,
        "residue_cleaned": 0,
        "warning": None,
    }

    total = extract_score_table_total(md_text)
    declared_label, declared_score = extract_declared(md_text)
    report["table_total"] = total
    report["declared_label"] = declared_label
    report["declared_score"] = declared_score

    if total is None or declared_label is None:
        report["warning"] = "内訳表またはセクション0 の信頼度表記を検出できず（検証スキップ）"
        return md_text, report

    expected_label = label_for_score(total)
    report["expected_label"] = expected_label
    consistent = (declared_label == expected_label
                  and (declared_score is None or declared_score == total))

    # Low へ落ちる場合、プラン非提示など本文構成そのものが変わるため機械訂正しない
    if not consistent and expected_label == LOW_LABEL:
        report["status"] = "needs_regeneration"
        report["warning"] = (
            f"再集計 {total} 点は Low（プラン非提示）に該当するが、本文は "
            f"{declared_label} 前提で書かれているため自動訂正しない"
        )
        return md_text, report

    lines = md_text.splitlines()
    cleaned: list[str] = []
    residue = 0
    for line in lines:
        # 「セクション0 は N と読み替えること」型の注記は、訂正後に虚偽になるため落とす
        if _READ_AS_NOTE_RE.search(line) and "セクション0" in line:
            residue += 1
            continue
        # `合計 4+... 再集計: ...` の生成残骸を正規形に置き換える
        if _RECALC_LINE_RE.match(line):
            cleaned.append(f"合計: **{total}** → 判定 **{expected_label}**")
            residue += 1
            continue
        cleaned.append(line)

    if not consistent:
        idx = _find_section0_confidence_line("\n".join(cleaned))
        if idx is not None:
            line = cleaned[idx]
            line = _LABEL_RE.sub(expected_label, line, count=1)
            line = _SCORE_NUM_RE.sub(lambda m: f"{m.group(1)}{total}", line, count=1)
            cleaned[idx] = line
            report["status"] = "corrected"
        else:
            report["warning"] = "訂正対象のセクション0 行を再検出できず"
            report["status"] = "skipped"
    else:
        report["status"] = "ok"

    report["residue_cleaned"] = residue
    out = "\n".join(cleaned)
    if md_text.endswith("\n") and not out.endswith("\n"):
        out += "\n"
    return out, report
