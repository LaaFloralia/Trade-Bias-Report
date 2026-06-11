"""Intel pipeline — ヘッドレス Bias 分析

データ取得 (main.py) → claude -p ヘッドレス分析 (既存 master_prompt 使用) →
二重出力 (人間用 Markdown を Brain へ / 機械用 JSON を output/intel/ へ) を
一気通貫で実行する。LLM 呼び出しは Claude Code CLI のヘッドレスモード
(`claude -p`) を使用し、Anthropic API 直叩きは行わない（サブスク運用方針）。

使い方:
    python scripts/intel.py brief --daily            # 日次ブリーフ一式
    python scripts/intel.py brief --weekly           # 週次（COT 込み）
    python scripts/intel.py brief --daily --reuse-data
        # 当日の scraped_data_*.txt が既にあれば再スクレイピングを省略

出力:
    1. 人間用 MD : $BRAIN_PATH/Calendar/{Daily-Bias|Weekly-Bias}/
                   {Daily|Weekly}_Bias_Report_YYYY-MM-DD.md（既存形式を維持）
    2. 機械用 JSON: output/intel/intel_{daily|weekly}_YYYY-MM-DD.json
       schema: { bias: -1.0〜+1.0, no_trade: bool, no_trade_reason: str|null,
                 risk_events_next_24h: [str], positioning_summary: str,
                 confidence: 0.0〜1.0 }
    3. 実行ログ : logs/intel_runs.jsonl（全実行の入出力を JSONL で追記）

JSON パース/スキーマ検証に失敗した場合はリトライ 1 回 → なお失敗なら
no_trade=true の安全側 JSON にフォールバックする（exit 0 のまま）。

環境変数:
    BRAIN_PATH           Brain リポジトリのルート（既定: ~/Brain）
    INTEL_CLAUDE_BIN     claude CLI のパス（既定: claude）
    INTEL_CLAUDE_TIMEOUT claude 1 呼び出しのタイムアウト秒（既定: 900）
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"
INTEL_DIR = OUTPUT_DIR / "intel"
LOGS_DIR = PROJECT_ROOT / "logs"
JSONL_PATH = LOGS_DIR / "intel_runs.jsonl"

CLAUDE_BIN = os.environ.get("INTEL_CLAUDE_BIN", "claude")
CLAUDE_TIMEOUT = int(os.environ.get("INTEL_CLAUDE_TIMEOUT", "900"))

MODES = {
    "daily": {
        "master_prompt": "master_prompt.md",
        "report_kind": "ICT Daily Bias Report",
        "brain_subdir": "Calendar/Daily-Bias",
        "md_prefix": "Daily_Bias_Report",
        "weekly_flag": False,
        "length_hint": "全体 2000-3500 字を目安",
    },
    "weekly": {
        "master_prompt": "master_prompt_weekly.md",
        "report_kind": "ICT Weekly Bias Report",
        "brain_subdir": "Calendar/Weekly-Bias",
        "md_prefix": "Weekly_Bias_Report",
        "weekly_flag": True,
        "length_hint": "全体 3000-5000 字を目安",
    },
}

# ---------------------------------------------------------------------------
# 機械用 JSON スキーマ
# ---------------------------------------------------------------------------

INTEL_JSON_KEYS = (
    "bias",
    "no_trade",
    "no_trade_reason",
    "risk_events_next_24h",
    "positioning_summary",
    "confidence",
)


def validate_intel_json(obj) -> List[str]:
    """機械用 JSON のスキーマ検証。違反メッセージのリストを返す（空 = 合格）。"""
    errors = []
    if not isinstance(obj, dict):
        return ["root が JSON オブジェクトではない"]

    for key in INTEL_JSON_KEYS:
        if key not in obj:
            errors.append(f"必須キー欠落: {key}")
    if errors:
        return errors

    bias = obj["bias"]
    if not isinstance(bias, (int, float)) or isinstance(bias, bool):
        errors.append("bias が数値ではない")
    elif not -1.0 <= float(bias) <= 1.0:
        errors.append(f"bias が範囲外 (-1.0〜+1.0): {bias}")

    if not isinstance(obj["no_trade"], bool):
        errors.append("no_trade が bool ではない")

    reason = obj["no_trade_reason"]
    if reason is not None and not isinstance(reason, str):
        errors.append("no_trade_reason が str | null ではない")
    if obj.get("no_trade") is True and not reason:
        errors.append("no_trade=true なのに no_trade_reason が空")

    events = obj["risk_events_next_24h"]
    if not isinstance(events, list) or any(not isinstance(e, str) for e in events):
        errors.append("risk_events_next_24h が文字列配列ではない")

    if not isinstance(obj["positioning_summary"], str) or not obj["positioning_summary"].strip():
        errors.append("positioning_summary が非空文字列ではない")

    conf = obj["confidence"]
    if not isinstance(conf, (int, float)) or isinstance(conf, bool):
        errors.append("confidence が数値ではない")
    elif not 0.0 <= float(conf) <= 1.0:
        errors.append(f"confidence が範囲外 (0.0〜1.0): {conf}")

    return errors


def normalize_intel_json(obj: dict) -> dict:
    """スキーマ外の余剰キーを落とし、正規の 6 キーのみ順序固定で返す。"""
    return {
        "bias": round(float(obj["bias"]), 3),
        "no_trade": bool(obj["no_trade"]),
        "no_trade_reason": obj["no_trade_reason"],
        "risk_events_next_24h": list(obj["risk_events_next_24h"]),
        "positioning_summary": obj["positioning_summary"].strip(),
        "confidence": round(float(obj["confidence"]), 3),
    }


def fallback_intel_json(reason: str) -> dict:
    """JSON 生成失敗時の安全側フォールバック（トレード抑止）。"""
    return {
        "bias": 0.0,
        "no_trade": True,
        "no_trade_reason": f"機械用 JSON の生成に失敗したため安全側に固定: {reason}",
        "risk_events_next_24h": [],
        "positioning_summary": "JSON 生成失敗のためポジショニング要約なし（MD レポートを参照）",
        "confidence": 0.0,
    }


def extract_json_object(text: str) -> Optional[dict]:
    """LLM 応答からコードフェンス・前後の説明文を剥がして JSON object を抽出。"""
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# claude -p ヘッドレス実行
# ---------------------------------------------------------------------------

def run_claude(prompt: str, timeout: int = None) -> str:
    """claude CLI をヘッドレス (-p) で実行し、stdout を返す。失敗は RuntimeError。"""
    timeout = timeout or CLAUDE_TIMEOUT
    try:
        proc = subprocess.run(
            [CLAUDE_BIN, "-p", "--output-format", "text"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(PROJECT_ROOT),
        )
    except FileNotFoundError:
        raise RuntimeError(f"claude CLI が見つかりません: {CLAUDE_BIN}")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"claude -p がタイムアウト ({timeout}s)")

    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "")[-500:]
        raise RuntimeError(f"claude -p exit {proc.returncode}: {stderr_tail}")
    out = (proc.stdout or "").strip()
    if not out:
        raise RuntimeError("claude -p の出力が空")
    return out


# ---------------------------------------------------------------------------
# Step 1: データ取得
# ---------------------------------------------------------------------------

def collect_data(weekly: bool, reuse: bool, date_str: str) -> Path:
    """main.py を実行して scraped_data_<date>.txt を生成し、そのパスを返す。"""
    txt_path = OUTPUT_DIR / f"scraped_data_{date_str}.txt"
    if reuse and txt_path.exists():
        print(f"[intel] --reuse-data: 既存の {txt_path.name} を使用")
        return txt_path

    cmd = [sys.executable, str(PROJECT_ROOT / "main.py")]
    if weekly:
        cmd.append("--weekly")
    print(f"[intel] Step 1: データ取得を実行 ({' '.join(cmd)})")
    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), timeout=1200)
    if proc.returncode != 0:
        raise RuntimeError(f"main.py がexit {proc.returncode} で失敗")
    if not txt_path.exists():
        raise RuntimeError(f"データ取得後も {txt_path} が存在しない")
    return txt_path


# ---------------------------------------------------------------------------
# Step 2: 人間用 Markdown レポート生成
# ---------------------------------------------------------------------------

def build_report_prompt(mode: str, master_prompt_text: str, scraped_text: str) -> str:
    """slash command (/daily-bias) Step 3 のメンタルモデルをヘッドレスで再現する。"""
    cfg = MODES[mode]
    return f"""あなたはヘッドレスパイプラインから起動された分析エージェントである。
ツール（Read/Bash/WebSearch 等）は一切使わず、このプロンプト内の情報のみで完結すること。
出力は Markdown レポート本文のみとし、前置き・後書き・コードフェンスで全体を包むことを禁止する。

以下のマスタープロンプトの指示（セクション構成、テーブル形式、出力ルール、ICT 用語規則）に厳密に従い、
本日の {cfg['report_kind']} を生成してください。

=============== マスタープロンプト ここから ===============
{master_prompt_text}
=============== マスタープロンプト ここまで ===============

## 取得済みデータ (最優先で使用すること)

{scraped_text}

## 指示

- 取得済みデータを最優先で使用すること
- データ取得不可の項目は『取得不可』と明記すること
- 推測値には必ず『（推定）』と注記すること
- マスタープロンプトのセクション順序、テーブル形式、出力ルールに厳密に従うこと
- Markdown 形式、テーブル積極使用、絵文字禁止、時刻はすべて JST、{cfg['length_hint']}
"""


def generate_report_md(mode: str, scraped_text: str,
                       runner: Callable[[str], str]) -> Tuple[str, str]:
    """MD レポートを生成して (md_text, prompt) を返す。"""
    cfg = MODES[mode]
    master_prompt_text = (PROJECT_ROOT / cfg["master_prompt"]).read_text(encoding="utf-8")
    prompt = build_report_prompt(mode, master_prompt_text, scraped_text)
    print(f"[intel] Step 2: claude -p で {cfg['report_kind']} を生成中...")
    md = runner(prompt)
    return md, prompt


# ---------------------------------------------------------------------------
# Step 3: 機械用 JSON 生成（リトライ 1 回 → 安全側フォールバック）
# ---------------------------------------------------------------------------

def build_json_prompt(md_text: str, feedback: Optional[str] = None) -> str:
    schema_doc = """{
  "bias": <number>,                  // XAUUSD の日次バイアス。-1.0(強Bearish)〜+1.0(強Bullish)。Neutral=0.0
  "no_trade": <boolean>,             // レポートが「様子見 / プラン非提示 / 信頼度不足」相当なら true
  "no_trade_reason": <string|null>,  // no_trade=true の理由。false なら null
  "risk_events_next_24h": [<string>],// 今後24時間(JST)の高重要度イベント。"HH:MM JST イベント名" 形式。なければ []
  "positioning_summary": <string>,   // リテール/機関ポジショニングの要約（1〜3文）
  "confidence": <number>             // レポートの信頼度を 0.0〜1.0 に正規化（高確度=0.9, 標準=0.7, 慎重=0.5, 様子見=0.3 目安）
}"""
    feedback_block = ""
    if feedback:
        feedback_block = f"""
## 前回出力の問題点（必ず修正すること）

{feedback}
"""
    return f"""あなたはヘッドレスパイプラインの JSON 変換エージェントである。
ツールは一切使わず、出力は JSON オブジェクト 1 個のみ。コードフェンス・説明文・前置きを一切付けないこと。

以下の Bias Report (Markdown) を読み、次のスキーマの JSON に変換せよ。
{feedback_block}
## スキーマ

{schema_doc}

## 変換ルール

- bias はレポートの XAUUSD バイアス（方向と信頼度）から換算する。XAUUSD の記載が乏しい場合は DXY バイアスの逆相関から推定し、その分 confidence を下げる
- no_trade はレポートが様子見・プラン非提示・データ異常多発を示す場合に true
- risk_events_next_24h はレポートの経済指標カレンダーから今後 24 時間以内の★★★級のみ抽出
- 事実はレポート本文のみに依拠し、推測で補わない

## 変換対象レポート

{md_text}
"""


def generate_machine_json(md_text: str, runner: Callable[[str], str],
                          ) -> Tuple[dict, bool, List[dict]]:
    """機械用 JSON を生成。(json_obj, fallback_used, call_records) を返す。

    パース/スキーマ失敗時はエラー内容をフィードバックして 1 回だけリトライし、
    それでも失敗したら no_trade=true の安全側 JSON に倒す。
    """
    calls = []
    feedback = None
    for attempt in (1, 2):
        prompt = build_json_prompt(md_text, feedback=feedback)
        print(f"[intel] Step 3: 機械用 JSON 生成 (attempt {attempt}/2)...")
        record = {"purpose": f"json_attempt_{attempt}", "prompt": prompt}
        try:
            raw = runner(prompt)
        except RuntimeError as exc:
            record["error"] = str(exc)
            calls.append(record)
            feedback = f"claude 実行エラー: {exc}"
            continue
        record["response"] = raw
        obj = extract_json_object(raw)
        if obj is None:
            record["error"] = "JSON パース失敗"
            calls.append(record)
            feedback = "出力から JSON オブジェクトを抽出できなかった。JSON のみを出力すること。"
            continue
        errors = validate_intel_json(obj)
        if errors:
            record["error"] = f"スキーマ違反: {errors}"
            calls.append(record)
            feedback = "スキーマ違反: " + " / ".join(errors)
            continue
        calls.append(record)
        return normalize_intel_json(obj), False, calls

    print("[intel] [WARN] JSON 生成にリトライ含め失敗 → no_trade=true で安全側にフォールバック")
    return fallback_intel_json(feedback or "原因不明"), True, calls


# ---------------------------------------------------------------------------
# 出力・ログ
# ---------------------------------------------------------------------------

def brain_path() -> Path:
    return Path(os.environ.get("BRAIN_PATH", str(Path.home() / "Brain")))


def save_md_to_brain(md_text: str, mode: str, date_str: str) -> Path:
    cfg = MODES[mode]
    target_dir = brain_path() / cfg["brain_subdir"]
    target_dir.mkdir(parents=True, exist_ok=True)
    md_path = target_dir / f"{cfg['md_prefix']}_{date_str}.md"
    md_path.write_text(md_text, encoding="utf-8")
    return md_path


def save_intel_json(obj: dict, mode: str, date_str: str) -> Path:
    INTEL_DIR.mkdir(parents=True, exist_ok=True)
    json_path = INTEL_DIR / f"intel_{mode}_{date_str}.json"
    json_path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return json_path


def append_run_log(record: dict) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with JSONL_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# brief コマンド本体
# ---------------------------------------------------------------------------

def cmd_brief(args) -> int:
    mode = "weekly" if args.weekly else "daily"
    cfg = MODES[mode]
    date_str = datetime.now().strftime("%Y-%m-%d")
    run_record = {
        "run_id": uuid.uuid4().hex[:12],
        "command": "brief",
        "mode": mode,
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "claude_calls": [],
        "outputs": {},
        "ok": False,
    }
    t0 = time.time()
    try:
        # Step 1: データ取得
        txt_path = collect_data(cfg["weekly_flag"], args.reuse_data, date_str)
        scraped_text = txt_path.read_text(encoding="utf-8")
        run_record["scraped_file"] = str(txt_path)

        # Step 2: 人間用 MD
        md_text, report_prompt = generate_report_md(mode, scraped_text, run_claude)
        run_record["claude_calls"].append(
            {"purpose": "report_md", "prompt": report_prompt, "response": md_text}
        )
        md_path = save_md_to_brain(md_text, mode, date_str)
        run_record["outputs"]["md_path"] = str(md_path)
        print(f"[intel] MD 保存: {md_path}")

        # Step 3: 機械用 JSON（リトライ → フォールバック内蔵）
        intel_obj, fallback_used, json_calls = generate_machine_json(md_text, run_claude)
        run_record["claude_calls"].extend(json_calls)
        run_record["json_fallback_used"] = fallback_used
        json_path = save_intel_json(intel_obj, mode, date_str)
        run_record["outputs"]["json_path"] = str(json_path)
        print(f"[intel] JSON 保存: {json_path}（fallback={'あり' if fallback_used else 'なし'}）")

        run_record["ok"] = True
        run_record["intel_json"] = intel_obj
        return 0
    except Exception as exc:  # noqa: BLE001
        run_record["error"] = f"{type(exc).__name__}: {exc}"
        print(f"[intel] [ERROR] {run_record['error']}", file=sys.stderr)
        return 1
    finally:
        run_record["duration_s"] = round(time.time() - t0, 1)
        run_record["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        append_run_log(run_record)
        print(f"[intel] 実行ログ追記: {JSONL_PATH}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="intel.py", description="ヘッドレス Bias 分析パイプライン"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    brief = sub.add_parser("brief", help="データ取得 → 分析 → 二重出力の一気通貫")
    mode_group = brief.add_mutually_exclusive_group(required=True)
    mode_group.add_argument("--daily", action="store_true", help="日次ブリーフ")
    mode_group.add_argument("--weekly", action="store_true", help="週次ブリーフ（COT 込み）")
    brief.add_argument(
        "--reuse-data", action="store_true",
        help="当日の scraped_data_*.txt が既にあれば再スクレイピングを省略",
    )
    brief.set_defaults(func=cmd_brief)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
