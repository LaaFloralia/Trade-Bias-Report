"""Daily XAUUSD Brief — 既存 Daily Bias Report から XAUUSD 簡易版を再構成する。

このモジュールは Daily Bias Report 本体の生成ロジックには一切手を加えない。
本体が出力した Markdown を入力として受け取り、XAUUSD に絞った簡易ブリーフを
別ファイルとして並行生成するだけの追加モジュールである。

設計原則:
- 新規データ取得・新規分析は行わない。本体出力の「再構成」のみ。
- LLM 呼び出しは 1 回だけ（本体生成とは完全に別呼び出し）。
- 簡易版生成が失敗しても例外を送出しない。logger にエラーを残して None を返す。
  → 呼び出し側（本体生成フロー）はこの戻り値が None でも処理を継続できる。
- 本体 Daily Report が存在しない / 空の場合は簡易版もスキップする
  （本体がなければ簡易版も無意味なため）。

使い方 (CLI):
    python scripts/generate_xauusd_brief.py <Daily_Bias_Report_YYYY-MM-DD.md>

    出力 `Daily_XAUUSD_Brief_YYYY-MM-DD.md` は入力 Daily Report と同じ
    ディレクトリに書き出される。

使い方 (import):
    from scripts.generate_xauusd_brief import generate_xauusd_brief
    path = generate_xauusd_brief("output/Daily_Bias_Report_2026-05-20.md")

環境変数:
    ANTHROPIC_API_KEY   必須。Anthropic API キー（.env またはシェル環境）。
    XAUUSD_BRIEF_MODEL  任意。使用モデル ID（既定: claude-sonnet-4-6）。
    GENERATE_XAUUSD_BRIEF  任意。"false"/"0"/"no" で出力を無効化（下記トグルを上書き）。
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:  # .env からのキー読み込み（無くても動く）
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv は requirements に含まれる
    pass


# ============================================================
# 出力 ON/OFF トグル
# ------------------------------------------------------------
# スクリプト先頭の定数で簡易版出力を切り替える。
# 環境変数 GENERATE_XAUUSD_BRIEF が設定されている場合はそちらを優先する
# （"false" / "0" / "no" / "off" は無効、それ以外は有効）。
# ============================================================
GENERATE_XAUUSD_BRIEF = True


logger = logging.getLogger("xauusd_brief")

# 使用モデル。Daily 本体は Claude Code セッション側で生成されるため、
# 簡易版は独立した 1 回の API 呼び出しで完結させる。
DEFAULT_MODEL = os.getenv("XAUUSD_BRIEF_MODEL", "claude-sonnet-4-6")

# 本文（テーブル除く）の上限字数。
MAX_BODY_CHARS = 1500

JST = timezone(timedelta(hours=9))


# ------------------------------------------------------------
# トグル判定
# ------------------------------------------------------------
def _toggle_enabled() -> bool:
    """簡易版を出力すべきかどうかを返す。

    環境変数 GENERATE_XAUUSD_BRIEF があればそれを優先し、無ければ
    モジュール定数 GENERATE_XAUUSD_BRIEF を使う。
    """
    env = os.getenv("GENERATE_XAUUSD_BRIEF")
    if env is not None:
        return env.strip().lower() not in {"false", "0", "no", "off", ""}
    return bool(GENERATE_XAUUSD_BRIEF)


# ------------------------------------------------------------
# 字数カウント（テーブル行を除外）
# ------------------------------------------------------------
def _count_body_chars(markdown_body: str) -> int:
    """本文の字数をカウントする。Markdown テーブル行は字数に含めない。

    「テーブル行」= strip 後に `|` で始まる行（ヘッダ行・区切り行・データ行）。
    空白・改行は字数に含めない（純粋な文字数を見るため）。
    """
    chars = 0
    for line in markdown_body.splitlines():
        if line.strip().startswith("|"):
            continue
        chars += len(re.sub(r"\s", "", line))
    return chars


# ------------------------------------------------------------
# データ取得時刻の抽出
# ------------------------------------------------------------
def _extract_acquired_time(report_text: str) -> str:
    """Daily Report 本文からデータ取得時刻 (HH:MM) を best-effort で抽出する。

    抽出できない場合は現在の JST 時刻を返す。タイトルの "(HH:MM JST取得)"
    に使う。
    """
    # 1. "データ取得日時: 2026-05-20T08:33:..." 形式
    m = re.search(r"データ取得日時[:：]\s*\d{4}-\d{2}-\d{2}[ T](\d{2}:\d{2})", report_text)
    if m:
        return m.group(1)
    # 2. 本文中の "HH:MM JST取得" / "HH:MM JST" 形式
    m = re.search(r"(\d{1,2}:\d{2})\s*JST", report_text)
    if m:
        hhmm = m.group(1)
        return hhmm if len(hhmm) == 5 else f"0{hhmm}"
    # 3. フォールバック: 現在の JST 時刻
    return datetime.now(JST).strftime("%H:%M")


def _resolve_report_date(report_text: str, explicit: str | None) -> str:
    """レポート日付 (YYYY-MM-DD) を決定する。

    優先順: 明示指定 > 本文中の最初の日付 > 本日(JST)。
    """
    if explicit:
        return explicit
    m = re.search(r"(20\d{2}-\d{2}-\d{2})", report_text)
    if m:
        return m.group(1)
    return datetime.now(JST).strftime("%Y-%m-%d")


# ------------------------------------------------------------
# プロンプト構築
# ------------------------------------------------------------
def _build_system_prompt() -> str:
    """XAUUSD Brief 生成用のシステムプロンプト（構造制約・生成ルール）を返す。"""
    return f"""あなたは ICT / SMC を専門とするトレードアシスタントです。
既に生成済みの「ICT Daily Bias Report 本体」を入力として受け取り、
そこから XAUUSD に絞った簡易ブリーフ（本文のみ）を再構成して出力します。

# 最重要原則
- 入力 Daily 本体に書かれていない判断・数値・シナリオを新たに追加しない。
  新規分析・推測・創作は一切禁止。本体内容の「抜粋と再構成」のみを行う。
- XAUUSD 以外の銘柄情報は原則含めない。例外は DXY 動向のみ（後述・第5項）。
- 出力は日本語。絵文字は使わない。時刻はすべて JST。

# 出力する内容
セクション 1〜6 のみを、下記の順序・項目・見出しで出力する。
ドキュメントタイトル（H1 `# ...`）は出力しない（呼び出し側で付与する）。
セクションの追加・削除・順序変更は禁止。

## 1. 本日のゲート判定
- **トレード可否**: [可 / 条件付き可 / 見送り推奨] のいずれか1つ
- **取れる方向**: [Long のみ / Short のみ / 両方 / なし] のいずれか1つ
- **見送り条件**: 「○○の場合は本日無効」を1〜2行で明記

## 2. 21-24時に効く要素(最大3つ)
箇条書き、各1行。価格レベルと意味を併記する。3つを超えない。

## 3. 注目ゾーン
- **戻り売り / 押し目買い候補帯**: 価格レンジで提示
- **チャート確認指示**: H1/M15 のどこで何を見るかを1〜2行

## 4. 今夜のリスクイベント
Killzone 重複ハイインパクト指標のみ抜粋。なければ「なし」と書く。
ある場合は 時刻(JST)・指標名・想定影響 を1行で。テーブル可。

## 5. 信頼度
- スコア: X / 5
- プラス要因: 1行
- マイナス要因: 1行（必ず書く。なければ「特になし」）

## 6. 本日のNGアクション
- 「○○が起きた時点で本日終了」を1〜2行で具体的に書く
- tilt 防止のための事前コミットメントとして機能させる

# 生成ルール（厳守）
1. Daily 本体に書かれていない判断は追加しない。新規分析・推測の追加は禁止。
   本体内容の再構成のみ。
2. 「cautious」「やや」「軽微」等の曖昧語の連発を禁止。判定は二値寄りにする。
   「条件付き可」を使う場合は、その条件を必ず明記する。
3. XAUUSD 以外の銘柄情報は原則含めない。ただし DXY 動向が XAUUSD の判断材料
   として本体で言及されている場合に限り、第5項のプラス/マイナス要因に1行で
   含めてよい。
4. 第5項の信頼度スコアは本体のスコアリングをそのまま反映する。再計算しない。
   - 本体が「高確度」/「7点以上」相当 → 5
   - 本体が「標準確度」/「5-6点」相当 → 4
   - 本体が「標準確度（慎重）」/「4点」相当 → 3
   - 本体が「様子見推奨」/「3点以下」相当 → 2
   本体に明示の数値スコアがあれば、この対応で 5 点スケールに転記する。
   新たな加点・減点計算はしない。
5. 第6項「NGアクション」が本体に明示されていない場合は、本体内の
   「見送り条件」「無効化レベル」「破棄条件」から1つ抽出して具体化する。
   創作はしない。

# 字数制約（厳守）
- 本文（セクション 1〜6 のテーブルを除いた地の文）の合計は
  {MAX_BODY_CHARS} 字以内に厳守する。
- テーブル（`|` で始まる行）は字数制約の対象外。
- 制約を満たすため、各セクションは要点のみを簡潔に書く。
"""


def _build_user_prompt(report_text: str) -> str:
    """LLM へ渡すユーザーメッセージ（Daily 本体の全文）を返す。"""
    return (
        "以下は本日生成済みの ICT Daily Bias Report 本体です。\n"
        "この内容のみを根拠に、XAUUSD Brief のセクション 1〜6 を出力してください。\n\n"
        "===== Daily Bias Report 本体 ここから =====\n"
        f"{report_text}\n"
        "===== Daily Bias Report 本体 ここまで =====\n"
    )


# ------------------------------------------------------------
# LLM 呼び出し
# ------------------------------------------------------------
def _call_llm(client, model: str, system_prompt: str, user_prompt: str) -> str:
    """Anthropic API を 1 回呼び出して本文（セクション 1〜6）を得る。"""
    resp = client.messages.create(
        model=model,
        max_tokens=2500,
        temperature=0.2,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    parts = [
        block.text
        for block in resp.content
        if getattr(block, "type", None) == "text"
    ]
    return "".join(parts).strip()


def _strip_h1(body: str) -> str:
    """LLM が誤って H1 を付けた場合に備え、先頭の H1 行を除去する。"""
    lines = body.splitlines()
    while lines and (lines[0].startswith("# ") or lines[0].strip() == ""):
        if lines[0].startswith("# "):
            lines.pop(0)
        elif lines[0].strip() == "":
            lines.pop(0)
        else:
            break
    return "\n".join(lines).strip()


# ------------------------------------------------------------
# メイン関数
# ------------------------------------------------------------
def generate_xauusd_brief(
    daily_report,
    output_dir=None,
    report_date: str | None = None,
    client=None,
    model: str = DEFAULT_MODEL,
):
    """既存 Daily Bias Report から XAUUSD 簡易ブリーフを生成する。

    Args:
        daily_report: Daily Bias Report の Markdown ファイルパス（str / Path）
            または Markdown 本文そのもの（str）。
        output_dir: 出力先ディレクトリ。None かつ daily_report がパスの場合は
            そのパスと同じディレクトリに出力する。
        report_date: レポート日付 (YYYY-MM-DD)。None なら本文/本日から決定。
        client: Anthropic クライアント（テスト用に注入可）。None なら自動生成。
        model: 使用モデル ID。

    Returns:
        成功時: 書き出した Daily_XAUUSD_Brief_*.md の Path。
        スキップ時 / 失敗時: None（例外は送出しない。logger にエラーを残す）。
    """
    try:
        # --- トグル判定 ---
        if not _toggle_enabled():
            logger.info("XAUUSD Brief: GENERATE_XAUUSD_BRIEF が無効のためスキップ")
            return None

        # --- 入力解決（パス or 本文）---
        report_path: Path | None = None
        report_text: str

        if isinstance(daily_report, Path) or (
            isinstance(daily_report, str)
            and "\n" not in daily_report
            and daily_report.strip().endswith(".md")
        ):
            report_path = Path(daily_report).expanduser()
            if not report_path.exists():
                logger.error(
                    "XAUUSD Brief: Daily 本体が見つからないためスキップ: %s",
                    report_path,
                )
                return None
            report_text = report_path.read_text(encoding="utf-8")
        else:
            report_text = str(daily_report)

        # --- 本体が空ならスキップ（本体がなければ簡易版も無意味）---
        if not report_text or not report_text.strip():
            logger.error("XAUUSD Brief: Daily 本体が空のためスキップ")
            return None

        # --- 出力先の決定 ---
        if output_dir is not None:
            out_dir = Path(output_dir).expanduser()
        elif report_path is not None:
            out_dir = report_path.parent
        else:
            logger.error(
                "XAUUSD Brief: output_dir を特定できません"
                "（本文渡しの場合は output_dir を明示してください）"
            )
            return None

        # --- 日付・取得時刻・タイトル ---
        date_str = _resolve_report_date(report_text, report_date)
        acquired = _extract_acquired_time(report_text)
        h1 = f"# XAUUSD Brief {date_str} ({acquired} JST取得)"

        # --- LLM クライアント（注入が無ければ生成）---
        if client is None:
            try:
                import anthropic
            except ImportError:
                logger.error(
                    "XAUUSD Brief: anthropic パッケージが未インストールのためスキップ"
                )
                return None
            if not os.getenv("ANTHROPIC_API_KEY"):
                logger.error(
                    "XAUUSD Brief: ANTHROPIC_API_KEY が未設定のためスキップ"
                )
                return None
            client = anthropic.Anthropic()

        system_prompt = _build_system_prompt()
        user_prompt = _build_user_prompt(report_text)

        # --- LLM 呼び出し（1 回）---
        body = _strip_h1(_call_llm(client, model, system_prompt, user_prompt))

        # --- 字数チェック（超過時は 1 回だけ短縮リトライ）---
        body_chars = _count_body_chars(body)
        if body_chars > MAX_BODY_CHARS:
            logger.warning(
                "XAUUSD Brief: 本文 %d 字 > 上限 %d 字。短縮リトライを実行",
                body_chars,
                MAX_BODY_CHARS,
            )
            retry_prompt = (
                user_prompt
                + f"\n\n【再指示】直前の出力は本文が {body_chars} 字あり、"
                f"上限 {MAX_BODY_CHARS} 字（テーブル除く）を超過しました。"
                "構造・項目はそのままに、地の文をさらに削って "
                f"{MAX_BODY_CHARS} 字以内に収めて再出力してください。"
            )
            body = _strip_h1(_call_llm(client, model, system_prompt, retry_prompt))
            body_chars = _count_body_chars(body)
            if body_chars > MAX_BODY_CHARS:
                logger.warning(
                    "XAUUSD Brief: 短縮後も %d 字。上限超過のまま出力します",
                    body_chars,
                )

        # --- ファイル書き出し ---
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"Daily_XAUUSD_Brief_{date_str}.md"
        out_path.write_text(f"{h1}\n\n{body}\n", encoding="utf-8")

        logger.info(
            "XAUUSD Brief: 生成完了 %s（本文 %d 字）", out_path, body_chars
        )
        return out_path

    except Exception as e:  # 簡易版の失敗で本体フローを落とさない
        logger.error("XAUUSD Brief: 生成に失敗しました: %s", e, exc_info=True)
        return None


# ------------------------------------------------------------
# CLI
# ------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="既存 Daily Bias Report から XAUUSD 簡易ブリーフを生成する"
    )
    parser.add_argument(
        "daily_report",
        help="Daily Bias Report の Markdown ファイルパス",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="出力先ディレクトリ（既定: 入力ファイルと同じディレクトリ）",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"使用モデル ID（既定: {DEFAULT_MODEL}）",
    )
    args = parser.parse_args(argv)

    result = generate_xauusd_brief(
        args.daily_report,
        output_dir=args.output_dir,
        model=args.model,
    )
    if result is None:
        # スキップ / 失敗。本体フローを落とさない設計に合わせ、
        # CLI 単体実行でも 0 を返す（失敗はログに残っている）。
        print("XAUUSD Brief: 生成されませんでした（ログを確認してください）")
        return 0
    print(f"XAUUSD Brief: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
