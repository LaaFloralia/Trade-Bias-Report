"""Intel pipeline — ヘッドレス Bias 分析

データ取得 (main.py) → LLM ヘッドレス分析 (既存 master_prompt 使用) →
二重出力 (人間用 Markdown を Brain へ / 機械用 JSON を output/intel/ へ) →
PDF 発行 (scripts/publish_report.py 経由で Google Drive へ、非致命) を
一気通貫で実行する。LLM 呼び出しは既定で Claude Code CLI のヘッドレスモード
(`claude -p`)。環境変数 INTEL_ENGINE=codex で Codex CLI (`codex exec`) に
切り替えられる（エンジンシーム）。Anthropic API 直叩きは行わない（サブスク運用方針）。

使い方:
    python scripts/intel.py brief --daily            # 日次ブリーフ一式（COT 常時取得）
    python scripts/intel.py brief --weekly           # 週次（scraped_data_weekly_ prefix）
    python scripts/intel.py brief --daily --reuse-data
        # 当日の scraped_data_*.txt が既にあれば再スクレイピングを省略
    python scripts/intel.py brief --daily --quick
        # 新規取得を完全にスキップし、直近（日付不問）の scraped_data_*.txt で
        # 分析のみ再実行する軽量モード。--reuse-data より優先される

出力:
    1. 人間用 MD : $BRAIN_PATH/Calendar/{Daily-Bias|Weekly-Bias}/
                   {Daily|Weekly}_Bias_Report_YYYY-MM-DD.md（既存形式を維持）
    2. 機械用 JSON: output/intel/intel_{daily|weekly}_YYYY-MM-DD.json
       schema: { bias: -1.0〜+1.0, no_trade: bool, no_trade_reason: str|null,
                 risk_events_next_24h: [str], positioning_summary: str,
                 confidence: 0.0〜1.0, data_as_of: YYYY-MM-DD,
                 generated_at: ISO8601 }
    3. PDF + Drive: output/*.pdf と config.yaml output.gdrive_pdf_dir へのコピー
       （publish_report.py に委譲。失敗しても run 全体は成功のまま）
    4. 実行ログ : logs/intel_runs.jsonl（全実行の入出力を JSONL で追記）

JSON パース/スキーマ検証に失敗した場合はリトライ 1 回 → なお失敗なら
no_trade=true の安全側 JSON にフォールバックする（exit 0 のまま）。

環境変数:
    BRAIN_PATH           Brain リポジトリのルート（既定: ~/Brain）
    INTEL_ENGINE         LLM エンジン: claude | codex（既定: claude）
    INTEL_CLAUDE_BIN     claude CLI のパス（既定: claude）
    INTEL_CLAUDE_MODEL   生成モデル（既定: opus = Opus 5。空文字でセッション既定を継承）
    INTEL_CLAUDE_EFFORT  推論強度（既定: high。空文字でセッション既定を継承）
    INTEL_CODEX_BIN      codex CLI のパス（既定: codex、INTEL_ENGINE=codex 時のみ使用）
    INTEL_CLAUDE_TIMEOUT LLM 1 呼び出しのタイムアウト秒（既定: 900、両エンジン共通）
    INTEL_SKIP_XAU_TF    1 で XAU-TF エンジンの自動再生成をスキップ
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# `python scripts/intel.py` 直接実行時は sys.path にプロジェクトルートが入らず
# `from scrapers import ...` が ModuleNotFoundError になるため明示的に追加する
# （pytest 経由では rootdir が入るため検出されない）。
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
OUTPUT_DIR = PROJECT_ROOT / "output"
INTEL_DIR = OUTPUT_DIR / "intel"
LOGS_DIR = PROJECT_ROOT / "logs"
JSONL_PATH = LOGS_DIR / "intel_runs.jsonl"

CLAUDE_BIN = os.environ.get("INTEL_CLAUDE_BIN", "claude")
CLAUDE_TIMEOUT = int(os.environ.get("INTEL_CLAUDE_TIMEOUT", "900"))
# 定時配信のモデル・推論強度は明示ピンする（2026-08-13）。
# 理由: 未指定だと ~/.claude/settings.json のセッション既定を継承し、社長が /model を
# 切り替えると定時配信の生成モデルまで意図せず変わる。加えて Fable 5 は Max の週次
# プールを共有して消費が速いため、毎営業日の自動生成には Opus 5 を既定にする
# （本レポートは 2,400〜3,800 字の構造化文書で、Fable の長文一貫性の優位が効かない領域）。
# 手動の /daily-bias はセッションのモデルをそのまま使う（この定数の影響を受けない）。
CLAUDE_MODEL = os.environ.get("INTEL_CLAUDE_MODEL", "opus")
CLAUDE_EFFORT = os.environ.get("INTEL_CLAUDE_EFFORT", "high")
# レポート本文生成時のみ許可するツール（カンマ区切り。空文字で完全無効化）。
# 金は地政学プレミアムと要人発言で動くため、ニュースセクションを埋めるには検索が要る。
CLAUDE_ALLOWED_TOOLS = os.environ.get("INTEL_CLAUDE_ALLOWED_TOOLS", "WebSearch,WebFetch")

MODES = {
    "daily": {
        "master_prompt": "master_prompt.md",
        "report_kind": "ICT Daily Bias Report",
        "brain_subdir": "Calendar/Daily-Bias",
        "md_prefix": "Daily_Bias_Report",
        "weekly_flag": False,
        "scraped_prefix": "scraped_data_",
        "length_hint": "全体 2400-3800 字を目安",
    },
    "weekly": {
        "master_prompt": "master_prompt_weekly.md",
        "report_kind": "ICT Weekly Bias Report",
        "brain_subdir": "Calendar/Weekly-Bias",
        "md_prefix": "Weekly_Bias_Report",
        "weekly_flag": True,
        "scraped_prefix": "scraped_data_weekly_",
        "length_hint": "全体 4500-6500 字を目安",
    },
}

# デフォルト銘柄: scraped_data_(weekly_)DATE.txt / 個別銘柄: scraped_data_{SYM}_DATE.txt
# いずれも日付は group(1) で取れる。日付直結を強制することで、個別銘柄ファイルが
# デフォルト銘柄の glob (--quick / --reuse-data) を汚染しないようにする。
SCRAPED_RE = re.compile(r"scraped_data(?:_weekly|_[A-Z0-9]{3,12})?_(\d{4}-\d{2}-\d{2})\.txt$")
_SCRAPED_DEFAULT_DAILY_RE = re.compile(r"scraped_data_\d{4}-\d{2}-\d{2}\.txt$")
_SCRAPED_WEEKLY_RE = re.compile(r"scraped_data_weekly_\d{4}-\d{2}-\d{2}\.txt$")
STALE_DATA_BANNER = """> [!warning] STALE DATA
> 本レポートは **{data_as_of} 時点の取得データ**から再生成された（実行日: {run_date}、--quick/--reuse-data モード）。
> 価格・イベント情報が古い可能性があるため、執行判断には当日データでの通常実行を使うこと。

"""

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


def attach_pipeline_metadata(obj: dict, data_as_of: str, generated_at: str) -> dict:
    """LLM 由来ではないパイプライン管理メタデータを付加する。"""
    enriched = dict(obj)
    enriched["data_as_of"] = data_as_of
    enriched["generated_at"] = generated_at
    return enriched


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
# LLM ヘッドレス実行（エンジンシーム: claude -p / codex exec）
# ---------------------------------------------------------------------------

def run_claude(prompt: str, timeout: int = None, allow_tools: bool = False) -> str:
    """claude CLI をヘッドレス (-p) で実行し、stdout を返す。失敗は RuntimeError。

    モデルと推論強度は明示ピンする（CLAUDE_MODEL / CLAUDE_EFFORT の定義コメント参照）。
    空文字を渡すとフラグ自体を省略し、セッション既定の継承に戻せる。

    allow_tools=True のときのみ WebSearch / WebFetch を許可する（レポート本文生成のみ）。
    これを渡さないと headless では検索系ツールが使えず、ニュース・地政学セクションが
    常時「検索不可環境」で空になる。JSON 変換・振り返り生成は入力テキストの変換に
    過ぎないためツールを与えない（余計な外部アクセスと実行時間を避ける）。
    """
    timeout = timeout or CLAUDE_TIMEOUT
    cmd = [CLAUDE_BIN, "-p", "--output-format", "text"]
    if CLAUDE_MODEL:
        cmd += ["--model", CLAUDE_MODEL]
    if CLAUDE_EFFORT:
        cmd += ["--effort", CLAUDE_EFFORT]
    if allow_tools and CLAUDE_ALLOWED_TOOLS:
        cmd += ["--allowedTools", *CLAUDE_ALLOWED_TOOLS.split(",")]
    try:
        proc = subprocess.run(
            cmd,
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


def run_codex(prompt: str, timeout: int = None) -> str:
    """codex CLI を非対話 (`codex exec`) で実行し、最終メッセージを返す。失敗は RuntimeError。

    プロンプトの受け渡し（`codex exec --help` で確認済み、2026-08-11）:
      - 位置引数 [PROMPT]、または `-` 指定で stdin から読む。長文のため stdin を使う。
      - 最終応答は `--output-last-message <FILE>` で回収する
        （stdout はイベントログ混在のため信頼しない）。
      - レポート生成にツール実行は不要なので `--sandbox read-only` で実行する。
    """
    codex_bin = os.environ.get("INTEL_CODEX_BIN", "codex")
    timeout = timeout or CLAUDE_TIMEOUT
    with tempfile.TemporaryDirectory(prefix="intel_codex_") as td:
        out_file = Path(td) / "last_message.txt"
        cmd = [
            codex_bin, "exec",
            "--sandbox", "read-only",
            "--output-last-message", str(out_file),
            "-",
        ]
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(PROJECT_ROOT),
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"codex CLI が見つかりません: {codex_bin}。"
                "INTEL_ENGINE=claude（または unset INTEL_ENGINE）で Claude エンジンに"
                "切り替えるか、INTEL_CODEX_BIN で codex のパスを指定してください"
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"codex exec がタイムアウト ({timeout}s)")

        if proc.returncode != 0:
            stderr_tail = (proc.stderr or "")[-500:]
            raise RuntimeError(f"codex exec exit {proc.returncode}: {stderr_tail}")
        out = ""
        if out_file.exists():
            out = out_file.read_text(encoding="utf-8").strip()
        if not out:
            # --output-last-message が書かれない異常系のみ stdout にフォールバック
            out = (proc.stdout or "").strip()
        if not out:
            raise RuntimeError("codex exec の出力が空")
        return out


def run_llm(prompt: str, timeout: int = None, allow_tools: bool = False) -> str:
    """エンジンシーム: 環境変数 INTEL_ENGINE (claude|codex、既定 claude) で切り替える。"""
    engine = os.environ.get("INTEL_ENGINE", "claude").strip().lower()
    if engine == "codex":
        # codex exec は read-only sandbox 固定のため allow_tools は無視される
        return run_codex(prompt, timeout=timeout)
    if engine != "claude":
        print(f"[intel] [WARN] 未知の INTEL_ENGINE={engine!r} → claude を使用")
    return run_claude(prompt, timeout=timeout, allow_tools=allow_tools)


def run_llm_with_tools(prompt: str, timeout: int = None) -> str:
    """レポート本文生成用: 検索ツールを許可した runner。"""
    return run_llm(prompt, timeout=timeout, allow_tools=True)


# ---------------------------------------------------------------------------
# Step 1: データ取得
# ---------------------------------------------------------------------------

def secrets_wrapper_prefix() -> Optional[List[str]]:
    """1Password 注入ラッパー (scripts/run-with-secrets.sh --batch) のコマンド接頭辞を返す。

    2026-08-10 のシークレット 1Password 移行以降、TWELVEDATA_API_KEY / FRED_API_KEY は
    環境変数のみで解決されるため、main.py の subprocess 実行時に op run で注入する。
    使えない環境（wrapper なし / service token なし / INTEL_SECRETS_WRAPPER=0）では
    None を返し、従来どおり素の実行にフォールバックする（該当スクレイパーは取得不可扱い）。
    """
    if os.environ.get("INTEL_SECRETS_WRAPPER", "1") == "0":
        return None
    wrapper = PROJECT_ROOT / "scripts" / "run-with-secrets.sh"
    token = Path.home() / ".config" / "laa" / "op-service-token"
    # 2026-08-11 の移行後はサービストークンが Keychain 管理になり、
    # 共通バッチラッパー (op-run-batch.sh) 経由で注入される
    # (run-with-secrets.sh --batch がフォールバックする)。どちらかがあれば使える。
    batch_wrapper = Path.home() / ".config" / "laa" / "op-run-batch.sh"
    if wrapper.exists() and (token.exists() or batch_wrapper.exists()):
        return [str(wrapper), "--batch"]
    return None


def ensure_xau_tf_fresh(timeout: int = 300) -> None:
    """XAU-TF エンジンレポート (Brain/Calendar/XAU-TF) の鮮度を確保する。

    slash command (/daily-bias) の Step 0 相当を headless でも実行する。
    今日/昨日分の MD が無ければ xauusd-smc-quant の run_report.py --fetch で再生成する。
    これは同時に live-h1.csv も更新する（retail_analytics のスイープ検証が参照）。
    失敗は非致命（WARN して続行。アンカー側が STALE 扱いにする）。
    INTEL_SKIP_XAU_TF=1 でスキップできる。
    """
    if os.environ.get("INTEL_SKIP_XAU_TF") == "1":
        return
    try:
        import config as _config
        repo = _config.XAU_TF_REPO_DIR
    except Exception as exc:  # noqa: BLE001
        print(f"[intel] [WARN] config から xau_tf_engine を読めない → XAU-TF 再生成スキップ: {exc}")
        return
    if not repo or not Path(repo).is_dir():
        print("[intel] [WARN] xau_tf_engine.repo_dir が未設定/不存在 → XAU-TF 再生成スキップ")
        return

    brain = brain_path()
    today = datetime.now().date()
    for d in (today, today - timedelta(days=1)):
        if (brain / "Calendar" / "XAU-TF" / f"XAU_Technical_Report_{d.isoformat()}.md").exists():
            return

    py = Path(repo) / ".venv" / "bin" / "python"
    if not py.exists():
        print(f"[intel] [WARN] XAU-TF venv が見つからない ({py}) → 再生成スキップ")
        return
    print("[intel] Step 0: XAU-TF レポートが古いため再生成 (run_report.py --fetch)")
    try:
        proc = subprocess.run(
            [str(py), "run_report.py", "--fetch"],
            cwd=str(repo), timeout=timeout, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            tail = (proc.stderr or "")[-300:]
            print(f"[intel] [WARN] XAU-TF 再生成 exit {proc.returncode}（非致命・続行）: {tail}")
        else:
            print("[intel] Step 0: XAU-TF 再生成完了")
    except Exception as exc:  # noqa: BLE001
        print(f"[intel] [WARN] XAU-TF 再生成失敗（非致命・続行）: {exc}")


def scraped_txt_path(mode: str, date_str: str, symbol: Optional[str] = None) -> Path:
    if symbol:
        return OUTPUT_DIR / f"scraped_data_{symbol}_{date_str}.txt"
    return OUTPUT_DIR / f"{MODES[mode]['scraped_prefix']}{date_str}.txt"


def find_latest_scraped(mode: str = "daily", symbol: Optional[str] = None) -> Optional[Path]:
    """output/ にある最新日付のモード別 scraped_data *.txt を返す（なければ None）。

    ファイル名の日付部分は接頭辞直結を強制する（正規表現で検証）。
    個別銘柄ファイル (scraped_data_USDJPY_*.txt) がデフォルト銘柄の
    候補に混入しないためのガード。名前順 = 日付順。
    """
    if symbol:
        pattern = re.compile(rf"scraped_data_{re.escape(symbol)}_\d{{4}}-\d{{2}}-\d{{2}}\.txt$")
        candidates = [p for p in OUTPUT_DIR.glob(f"scraped_data_{symbol}_*.txt")
                      if pattern.match(p.name)]
    elif mode == "daily":
        candidates = [p for p in OUTPUT_DIR.glob("scraped_data_*.txt")
                      if _SCRAPED_DEFAULT_DAILY_RE.match(p.name)]
    else:
        candidates = [p for p in OUTPUT_DIR.glob("scraped_data_weekly_*.txt")
                      if _SCRAPED_WEEKLY_RE.match(p.name)]
    candidates = sorted(candidates)
    return candidates[-1] if candidates else None


def collect_data(weekly: bool, reuse: bool, date_str: str, quick: bool = False,
                 symbol: Optional[str] = None) -> Path:
    """main.py を実行してモード別 scraped_data_<date>.txt を生成し、そのパスを返す。

    quick=True の場合は新規取得を完全にスキップし、直近（日付不問）の
    モード一致 scraped_data_*.txt をそのまま使う。1 件もなければ RuntimeError。
    symbol 指定時（個別銘柄デイリー）は scraped_data_{SYM}_ 系列を使う。
    """
    mode = "weekly" if weekly else "daily"
    if quick:
        latest = find_latest_scraped(mode, symbol=symbol)
        if latest is None:
            raise RuntimeError(
                f"--quick: output/ に {mode} 用 scraped_data_*.txt が 1 件もない（先に通常実行が必要）"
            )
        print(f"[intel] --quick: 新規取得をスキップし {latest.name} を使用")
        return latest

    txt_path = scraped_txt_path(mode, date_str, symbol=symbol)
    if reuse and txt_path.exists():
        print(f"[intel] --reuse-data: 既存の {txt_path.name} を使用")
        return txt_path

    # Step 0 相当: 新規スクレイプ前に XAU-TF エンジンの鮮度を確保
    # (report_anchor の [XAU テクニカル] と retail_analytics の H1 が依存。
    #  個別銘柄レポートは XAU-TF 非依存のためスキップ)
    if symbol is None:
        ensure_xau_tf_fresh()

    base_cmd = [sys.executable, str(PROJECT_ROOT / "main.py")]
    if weekly:
        base_cmd.append("--weekly")
    if symbol:
        base_cmd.extend(["--symbol", symbol])
    wrap = secrets_wrapper_prefix()
    cmd = (wrap + base_cmd) if wrap else base_cmd
    if wrap:
        print("[intel] Step 1: データ取得を実行（1Password 注入: run-with-secrets.sh --batch）")
    else:
        print(f"[intel] Step 1: データ取得を実行 ({' '.join(cmd)})")
    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), timeout=1200)
    if proc.returncode != 0 and wrap:
        # op 側の障害（トークン失効等）でデータ取得ごと死なないよう素の実行で 1 回再試行
        print(f"[intel] [WARN] op 注入付き実行が exit {proc.returncode} → 注入なしで再試行")
        proc = subprocess.run(base_cmd, cwd=str(PROJECT_ROOT), timeout=1200)
    if proc.returncode != 0:
        raise RuntimeError(f"main.py がexit {proc.returncode} で失敗")
    if not txt_path.exists():
        raise RuntimeError(f"データ取得後も {txt_path} が存在しない")
    return txt_path


def extract_data_date(path: Path) -> str:
    """scraped_data ファイル名からデータ基準日を抽出する。"""
    match = SCRAPED_RE.match(path.name)
    if match:
        return match.group(1)
    fallback = datetime.now().strftime("%Y-%m-%d")
    print(f"[WARN] scraped_data ファイル名から日付を抽出できないため実行日を使用: {path}")
    return fallback


# ---------------------------------------------------------------------------
# Step 2: 人間用 Markdown レポート生成
# ---------------------------------------------------------------------------

def build_report_prompt(
    mode: str,
    master_prompt_text: str,
    scraped_text: str,
    data_as_of: str,
    run_date: str,
    extra_block: Optional[str] = None,
) -> str:
    """slash command (/daily-bias) Step 3 のメンタルモデルをヘッドレスで再現する。"""
    cfg = MODES[mode]
    extra = f"\n\n{extra_block}" if extra_block else ""
    return f"""あなたはヘッドレスパイプラインから起動された分析エージェントである。
ファイル操作・コマンド実行系のツール（Read / Write / Edit / Bash）は使わない。
**WebSearch / WebFetch は利用可能**で、マスタープロンプトのニュース・地政学セクションの
検索ポリシー（クエリ数上限を含む）に従ってのみ使うこと。それ以外の事実は
このプロンプト内の取得済みデータだけで完結させる。
出力は Markdown レポート本文のみとし、前置き・後書き・コードフェンスで全体を包むことを禁止する。

以下のマスタープロンプトの指示（セクション構成、テーブル形式、出力ルール、ICT 用語規則）に厳密に従い、
本日の {cfg['report_kind']} を生成してください。

=============== マスタープロンプト ここから ===============
{master_prompt_text}
=============== マスタープロンプト ここまで ===============

## 取得済みデータ (最優先で使用すること)
データ基準日: {data_as_of}（パイプライン実行日: {run_date}）

{scraped_text}{extra}

## 指示

- 取得済みデータを最優先で使用すること
- データ取得不可の項目は『取得不可』と明記すること
- 推測値には必ず『（推定）』と注記すること
- マスタープロンプトのセクション順序、テーブル形式、出力ルールに厳密に従うこと
- Markdown 形式、テーブル積極使用、絵文字禁止、時刻はすべて JST、{cfg['length_hint']}
"""


def generate_report_md(mode: str, scraped_text: str,
                       runner: Callable[[str], str],
                       data_as_of: str,
                       run_date: str,
                       extra_block: Optional[str] = None,
                       symbol: Optional[str] = None) -> Tuple[str, str]:
    """MD レポートを生成して (md_text, prompt) を返す。

    symbol 指定時は個別銘柄用スリムプロンプト (master_prompt_symbol.md) を使い、
    {{SYMBOL}} プレースホルダを置換する。
    """
    cfg = MODES[mode]
    mp_file = "master_prompt_symbol.md" if symbol else cfg["master_prompt"]
    master_prompt_text = (PROJECT_ROOT / mp_file).read_text(encoding="utf-8")
    if symbol:
        master_prompt_text = master_prompt_text.replace("{{SYMBOL}}", symbol)
    prompt = build_report_prompt(
        mode, master_prompt_text, scraped_text, data_as_of, run_date, extra_block=extra_block
    )
    print(f"[intel] Step 2: claude -p で {cfg['report_kind']}"
          f"{f' ({symbol})' if symbol else ''} を生成中...")
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
# Step 3.5: Bias-Review-Log エントリ生成（振り返りナレッジベース）
# ---------------------------------------------------------------------------

def build_review_prompt(md_text: str, mode: str, date_str: str,
                        feedback: Optional[str] = None) -> str:
    mode_label = mode.capitalize()
    section_ref = "セクション8-1（前回照合）" if mode == "daily" else "セクション1（前回レビュー）"
    feedback_block = f"\n## 前回出力の問題点（必ず修正すること）\n\n{feedback}\n" if feedback else ""
    return f"""あなたはヘッドレスパイプラインの振り返り記録エージェントである。
ツールは一切使わず、出力はエントリ本文のみ（`## ` 見出し行から始める）。前置き・コードフェンス禁止。
{feedback_block}
以下の Bias Report (Markdown) の{section_ref}を読み、Bias-Review-Log のエントリを次の形式で生成せよ:

## {date_str} {mode_label}
- 判定: 当たり | 外れ | 未決着 | 照合不能
- 前回想定: （前回レポートのバイアス・信頼度・注目ゾーンを1行）
- 実際: （実際の値動きとスイープ実績を1行。リテール分析の sweep 検証があれば必ず含める）
- 外し要因: （外れ・未決着時のみ具体的に。それ以外は「-」）
- 学び: （次回の分析に継承すべき視点を1〜2行。「どの視点が抜けていたか」を優先して書く）
<!-- review-json: {{"date": "{date_str}", "mode": "{mode}", "verdict": "hit|miss|open|n/a のいずれか"}} -->

ルール:
- 事実はレポート本文のみに依拠し、推測で補わない
- 照合不能（前回レポート未提供）の場合は verdict を "n/a" とする

## 対象レポート

{md_text}
"""


def generate_review_entry(md_text: str, mode: str, date_str: str,
                          runner: Callable[[str], str]) -> Tuple[Optional[str], List[dict]]:
    """振り返りエントリを生成する。(entry_md | None, call_records) を返す。

    形式検証に失敗したら 1 回だけリトライし、なお失敗なら None（非致命スキップ）。
    verdict が n/a（照合不能）の場合も None を返す（蓄積価値がないため）。
    """
    from scrapers import bias_review

    calls = []
    feedback = None
    for attempt in (1, 2):
        prompt = build_review_prompt(md_text, mode, date_str, feedback=feedback)
        print(f"[intel] Step 3.5: Bias-Review-Log エントリ生成 (attempt {attempt}/2)...")
        record = {"purpose": f"review_attempt_{attempt}", "prompt": prompt}
        try:
            raw = runner(prompt).strip()
        except RuntimeError as exc:
            record["error"] = str(exc)
            calls.append(record)
            feedback = f"実行エラー: {exc}"
            continue
        record["response"] = raw
        errors = bias_review.validate_entry(raw, date_str, mode)
        if errors:
            record["error"] = f"形式違反: {errors}"
            calls.append(record)
            feedback = "形式違反: " + " / ".join(errors)
            continue
        calls.append(record)
        if bias_review.extract_verdict(raw) == "n/a":
            print("[intel] Step 3.5: 照合不能 (n/a) のため記録をスキップ")
            return None, calls
        return raw, calls

    print("[intel] [WARN] Bias-Review-Log エントリ生成に失敗（非致命・スキップ）")
    return None, calls


# ---------------------------------------------------------------------------
# Brain git 同期（headless 実行時。他端末は git pull 経由で閲覧するため必須）
# ---------------------------------------------------------------------------

def commit_brain_outputs(rel_paths: List[str], message: str) -> bool:
    """Brain リポジトリで生成物のみを add → commit → master 直接 push する。

    コマンド版 (daily-bias.md Step 6) と同じルール:
    - 生成したファイル以外を git add しない（社長の個人メモを巻き込まない）
    - claude/... ブランチを作らず master (fallback: main) へ直接 push
    失敗は False を返すのみ（非致命。呼び出し側が WARN 表示）。
    """
    brain = brain_path()
    if not (brain / ".git").exists():
        print(f"[intel] [WARN] Brain が git リポジトリではない ({brain}) → コミットスキップ")
        return False

    def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(brain), *args],
            capture_output=True, text=True, timeout=120,
        )

    try:
        if _git("checkout", "master").returncode != 0:
            _git("checkout", "main")
        _git("pull", "--rebase", "origin", "master")
        added = False
        for rel in rel_paths:
            if (brain / rel).exists() and _git("add", rel).returncode == 0:
                added = True
        if not added:
            return False
        commit = _git("commit", "-m", message)
        if commit.returncode != 0:
            # 変更なし（同一内容の再生成）は正常扱い
            if "nothing to commit" in (commit.stdout + commit.stderr):
                return True
            print(f"[intel] [WARN] Brain commit 失敗: {(commit.stderr or '')[-200:]}")
            return False
        push = _git("push", "origin", "HEAD:master")
        if push.returncode != 0:
            push = _git("push", "origin", "HEAD:main")
        if push.returncode != 0:
            print(f"[intel] [WARN] Brain push 失敗（コミットはローカルに存在）: {(push.stderr or '')[-200:]}")
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[intel] [WARN] Brain git 同期失敗（非致命）: {exc}")
        return False


# ---------------------------------------------------------------------------
# 出力・ログ
# ---------------------------------------------------------------------------

def brain_path() -> Path:
    return Path(os.environ.get("BRAIN_PATH", str(Path.home() / "Brain")))


def save_md_to_brain(md_text: str, mode: str, date_str: str,
                     symbol: Optional[str] = None) -> Path:
    """MD を Brain に保存する。

    個別銘柄は Calendar/Daily-Bias/{SYM}/ サブディレクトリに置く。
    フラット配置だと report_anchor の非再帰 glob (`*_YYYY-MM-DD.md`) が
    銘柄別ファイルをデフォルト銘柄の前回 Daily と誤認するため、構造で分離する。
    """
    cfg = MODES[mode]
    target_dir = brain_path() / cfg["brain_subdir"]
    name = f"{cfg['md_prefix']}_{date_str}.md"
    if symbol:
        target_dir = target_dir / symbol
        name = f"{cfg['md_prefix']}_{symbol}_{date_str}.md"
    target_dir.mkdir(parents=True, exist_ok=True)
    md_path = target_dir / name
    md_path.write_text(md_text, encoding="utf-8")
    return md_path


def publish_report_pdf(md_path: Path, timeout: int = 300) -> dict:
    """publish_report.py を subprocess で呼び、PDF 発行と Drive コピーを行う。

    stdout の `PDF:` / `Drive:` 行をパースして {"pdf_path": ..., "drive_path": ...}
    を返す（出なかったキーは含めない）。publish_report.py はソフト障害を exit 0 で
    飲み込む設計だが、それ以外の失敗もここでは RuntimeError にして呼び出し側の
    try/except（非致命）に委ねる。
    """
    proc = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "publish_report.py"), str(md_path)],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(PROJECT_ROOT),
    )
    result: dict = {}
    for line in (proc.stdout or "").splitlines():
        if line.startswith("PDF:"):
            result["pdf_path"] = line.split(":", 1)[1].strip()
        elif line.startswith("Drive:"):
            result["drive_path"] = line.split(":", 1)[1].strip()
    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "")[-300:]
        raise RuntimeError(f"publish_report.py exit {proc.returncode}: {stderr_tail}")
    return result


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
    # 個別銘柄指定: デフォルト銘柄の明示指定は通常フロー扱い (symbol=None) に正規化
    symbol = getattr(args, "symbol", None)
    if symbol:
        import config as _config
        if symbol == _config.DEFAULT_SYMBOL:
            symbol = None
    run_dt = datetime.now().astimezone()
    date_str = run_dt.strftime("%Y-%m-%d")
    generated_at = run_dt.isoformat(timespec="seconds")
    run_record = {
        "run_id": uuid.uuid4().hex[:12],
        "command": "brief",
        "mode": mode,
        "symbol": symbol,
        "started_at": generated_at,
        "claude_calls": [],
        "outputs": {},
        "ok": False,
    }
    t0 = time.time()
    try:
        # Step 1: データ取得
        txt_path = collect_data(
            cfg["weekly_flag"], args.reuse_data, date_str, quick=args.quick, symbol=symbol
        )
        scraped_text = txt_path.read_text(encoding="utf-8")
        data_as_of = extract_data_date(txt_path)
        run_record["scraped_file"] = str(txt_path)
        run_record["quick"] = bool(args.quick)
        run_record["data_as_of"] = data_as_of

        # X-Search 追加ブロックはデフォルト銘柄 (XAUUSD/マクロ) 専用
        extra_block = None
        if symbol is None:
            from scrapers import xsearch_ingest

            xsearch_data = xsearch_ingest.load_xsearch(date_str)
            extra_block = xsearch_ingest.format_xsearch_block(xsearch_data) if xsearch_data else None
            run_record["xsearch_used"] = xsearch_data is not None
            run_record["xsearch_file"] = xsearch_ingest.LAST_SOURCE_FILE
            if xsearch_data is None:
                run_record["xsearch_skip_reason"] = xsearch_ingest.LAST_SKIP_REASON

        # Step 2: 人間用 MD（本文生成のみ検索ツールを許可）
        md_text, report_prompt = generate_report_md(
            mode, scraped_text, run_llm_with_tools, data_as_of, date_str,
            extra_block=extra_block, symbol=symbol,
        )
        if data_as_of != date_str:
            md_text = STALE_DATA_BANNER.format(
                data_as_of=data_as_of, run_date=date_str
            ) + md_text
        run_record["claude_calls"].append(
            {"purpose": "report_md", "prompt": report_prompt, "response": md_text}
        )
        md_path = save_md_to_brain(md_text, mode, date_str, symbol=symbol)
        run_record["outputs"]["md_path"] = str(md_path)
        print(f"[intel] MD 保存: {md_path}")

        # Step 2.5: PDF 発行 + Google Drive コピー（非致命: 失敗しても run は続行）
        try:
            pub = publish_report_pdf(md_path)
            if pub.get("pdf_path"):
                run_record["outputs"]["pdf_path"] = pub["pdf_path"]
                print(f"[intel] PDF 発行: {pub['pdf_path']}")
            if pub.get("drive_path"):
                run_record["outputs"]["drive_path"] = pub["drive_path"]
                print(f"[intel] Drive コピー: {pub['drive_path']}")
            if not pub:
                print("[intel] [WARN] PDF 発行はスキップされた（publish_report.py の WARN を参照）")
        except Exception as pub_exc:  # noqa: BLE001 — PDF はレポート本体を止めない
            run_record["outputs"]["pdf_error"] = f"{type(pub_exc).__name__}: {pub_exc}"
            print(f"[intel] [WARN] PDF 発行に失敗（非致命・続行）: {pub_exc}")

        # Step 3 前半: Bias-Review-Log エントリ生成（デフォルト銘柄のみ、非致命）
        review_meta = None
        if symbol is None:
            try:
                review_entry, review_calls = generate_review_entry(
                    md_text, mode, date_str, run_llm
                )
                run_record["claude_calls"].extend(review_calls)
                if review_entry:
                    from scrapers import bias_review

                    log_path = bias_review.append_entry(review_entry, date_str, mode)
                    review_meta = {"verdict": bias_review.extract_verdict(review_entry)}
                    run_record["outputs"]["review_log"] = str(log_path)
                    print(f"[intel] Bias-Review-Log 追記: {log_path}")
            except Exception as rev_exc:  # noqa: BLE001 — 振り返りはレポート本体を止めない
                print(f"[intel] [WARN] Bias-Review-Log 記録に失敗（非致命・続行）: {rev_exc}")

        # Step 3: 機械用 JSON（リトライ → フォールバック内蔵）
        # 個別銘柄実行では出力しない: logos-engine gates.py が
        # output/intel/intel_{mode}_*.json を日付 glob で消費しており、
        # intel_daily_{SYM}_*.json が混ざると日付マップを汚染するため。
        if symbol is None:
            intel_obj, fallback_used, json_calls = generate_machine_json(md_text, run_llm)
            intel_obj = attach_pipeline_metadata(intel_obj, data_as_of, generated_at)
            if review_meta:
                intel_obj["review"] = review_meta  # additive（既存 6 キー契約は不変）
            run_record["claude_calls"].extend(json_calls)
            run_record["json_fallback_used"] = fallback_used
            json_path = save_intel_json(intel_obj, mode, date_str)
            run_record["outputs"]["json_path"] = str(json_path)
            print(f"[intel] JSON 保存: {json_path}（fallback={'あり' if fallback_used else 'なし'}）")
            run_record["intel_json"] = intel_obj
        else:
            print(f"[intel] 個別銘柄 ({symbol}) のため機械用 JSON はスキップ（MD + PDF のみ）")

        # Step 4: Brain git 同期（他端末は git pull 経由で閲覧するため headless でも必須）
        try:
            rel_paths = [str(md_path.relative_to(brain_path()))]
            if symbol is None:
                rel_paths.append("Atlas/Bias-Review-Log.md")
                rel_paths.append(f"Calendar/XAU-TF/XAU_Technical_Report_{date_str}.md")
            msg = f"ICT {mode.capitalize()} Bias{f' ({symbol})' if symbol else ''} {date_str}"
            synced = commit_brain_outputs(rel_paths, msg)
            run_record["outputs"]["brain_synced"] = synced
            print(f"[intel] Brain git 同期: {'OK' if synced else 'スキップ/失敗（非致命）'}")
        except Exception as git_exc:  # noqa: BLE001
            run_record["outputs"]["brain_synced"] = False
            print(f"[intel] [WARN] Brain git 同期に失敗（非致命・続行）: {git_exc}")

        run_record["ok"] = True
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
    brief.add_argument(
        "--quick", action="store_true",
        help="新規取得を完全にスキップし、直近（日付不問）の scraped_data_*.txt で"
             "分析のみ再実行する軽量モード（--reuse-data より優先）",
    )
    brief.add_argument(
        "--symbol", default=None,
        help="個別銘柄デイリー (例: USDJPY)。config report.on_demand_symbols のみ有効。"
             "デイリー専用・機械用 JSON なし (MD + PDF のみ)",
    )
    brief.set_defaults(func=cmd_brief)

    args = parser.parse_args(argv)

    # --symbol の検証 (brief のみ)
    if getattr(args, "symbol", None):
        import config as _config
        if args.weekly:
            parser.error("--symbol はデイリー専用（--weekly と併用不可）")
        valid = set(_config.ON_DEMAND_SYMBOLS) | {_config.DEFAULT_SYMBOL}
        if args.symbol not in valid:
            parser.error(f"--symbol {args.symbol} は無効（有効: {sorted(valid)}）")

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
