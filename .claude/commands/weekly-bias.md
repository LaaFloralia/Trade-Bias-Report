---
description: ICT Weekly Bias Report を生成 (Mac/Routines 両対応)
allowed-tools: Bash, Read, Write
---

# ICT Weekly Bias Report 生成

週の開始前（日曜夜 or 月曜早朝 JST）に、過去一週間の値動き、COT レポート、
マクロ経済カレンダー、中銀イベント等を体系的にまとめたレポートを生成し、
Brain (Obsidian Vault) に保存する。

本コマンドは `daily-bias.md` の週次バリアント。実行ロジックのほぼ全てを共有し、
以下の差分だけを持つ:

- `main.py` を `--weekly` フラグ付きで実行（COT データを含む）
- `master_prompt_weekly.md` を使う（セクション構成が週次向け）
- 出力先が `Calendar/Weekly-Bias/Weekly_Bias_Report_YYYY-MM-DD.md`
- Slack 通知のタイトルが `ICT Weekly Bias Report - YYYY-MM-DD`

## 環境変数

`daily-bias.md` と同一。`PROJECT_DIR` / `BRAIN_PATH` / `PYTHON_BIN` /
`SLACK_NOTIFY_CHANNEL` / `CLAUDE_CODE_REMOTE` の扱いは変更なし。

## 実行ルール

- 全ステップを順番に実行。失敗時は原因を報告して中止
- レポート本文は推測で埋めず、データ取得不可の項目は `取得不可` と明記
- 推測値には必ず `（推定）` と注記
- Markdown 内に絵文字を使わない

## Step 1: スクレイピング実行 (--weekly)

`daily-bias.md` Step 1 と同じパス解決ロジックを使い、末尾の実行コマンドを
`--weekly` 付きで呼ぶ。

```bash
# 共通ヘルパーとパス解決は daily-bias.md Step 1 と同一。差分のみ示す。

find_repo() {
  local name="$1"
  for base in /home/user /root /workspace /tmp; do
    if [ -d "$base/$name" ]; then
      echo "$base/$name"
      return
    fi
  done
  find /home /root /workspace -maxdepth 4 -type d -name "$name" 2>/dev/null | head -1
}

if [ "$CLAUDE_CODE_REMOTE" = "true" ]; then
  PROJECT_DIR="${PROJECT_DIR:-$(find_repo Trade-Bias-Report)}"
  BRAIN_PATH="${BRAIN_PATH:-$(find_repo Brain)}"
  PYTHON_BIN="${PYTHON_BIN:-python3}"
else
  PROJECT_DIR="${PROJECT_DIR:-/Users/laa/dev/fundamental-macro-analysis}"
  BRAIN_PATH="${BRAIN_PATH:-$HOME/Brain}"
  PYTHON_BIN="${PYTHON_BIN:-$PROJECT_DIR/.venv/bin/python3}"
fi
export PROJECT_DIR BRAIN_PATH PYTHON_BIN

if [ -z "$PROJECT_DIR" ] || [ ! -d "$PROJECT_DIR" ]; then
  echo "ERROR: PROJECT_DIR not found: '$PROJECT_DIR'" >&2
  exit 1
fi
if [ -z "$BRAIN_PATH" ] || [ ! -d "$BRAIN_PATH" ]; then
  echo "ERROR: BRAIN_PATH not found: '$BRAIN_PATH'" >&2
  exit 1
fi

# Playwright chromium check (Routines 環境のみ)
if [ "$CLAUDE_CODE_REMOTE" = "true" ]; then
  if ! "$PYTHON_BIN" -c "from playwright.sync_api import sync_playwright; sync_playwright().__enter__().chromium.launch(headless=True).close()" >/dev/null 2>&1; then
    echo "Playwright chromium not usable, installing..."
    "$PYTHON_BIN" -m pip install playwright >/dev/null 2>&1 || true
    "$PYTHON_BIN" -m playwright install chromium 2>&1 | tail -5
  fi
fi

cd "$PROJECT_DIR" && "$PYTHON_BIN" main.py --weekly
```

実行後、`$PROJECT_DIR/output/scraped_data_weekly_YYYY-MM-DD.json` と `.txt` が生成される
（週次は COT セクションが含まれる）。

## Step 2: マスタープロンプトとデータの読み込み

- `$PROJECT_DIR/master_prompt_weekly.md` を Read
- `$PROJECT_DIR/output/scraped_data_weekly_YYYY-MM-DD.txt` を Read

## Step 3: 分析・レポート生成

`master_prompt_weekly.md` のセクション構成（マクロサマリー / COT / 週間カレンダー /
銘柄別週次バイアス / 週次 Draw on Liquidity / 週次注目イベント等）に厳密に従う。

Routines 環境では Stream idle timeout 回避のため以下を厳守:

- 全体 1500-2000 字以内
- 各セクション 200-300 字以内
- テーブルは重要項目のみ
- 詳細な根拠説明は省略し、結論・数値・方向性のみ記載
- 該当事項なしのセクションは「該当なし」の 1 行で省略
- 注目イベントセクションのみ最大 400 字まで詳細可

詳細分析はローカル (Mac) 経路で行う想定。

## Step 4: レポートを Brain に保存

```bash
mkdir -p "$BRAIN_PATH/Calendar/Weekly-Bias"
```

Write ツールで以下に保存:
`$BRAIN_PATH/Calendar/Weekly-Bias/Weekly_Bias_Report_YYYY-MM-DD.md`

## Step 5: Routines 環境のみ追加処理

`$CLAUDE_CODE_REMOTE=true` のみ実行。

### 5-1. Brain へ commit + push

**master ブランチに直接** commit / push（新 claude ブランチ禁止）。

```bash
TODAY=$(date +%Y-%m-%d)

cd "$BRAIN_PATH"
git checkout master 2>/dev/null || git checkout main
git pull --rebase origin master 2>/dev/null || git pull --rebase origin main
git add "Calendar/Weekly-Bias/Weekly_Bias_Report_${TODAY}.md"
git commit -m "ICT Weekly Bias ${TODAY}"
git push origin HEAD:master 2>/dev/null || git push origin HEAD:main
```

### 5-2. Slack 通知

Slack MCP で `${SLACK_NOTIFY_CHANNEL:-#ceo}` に投稿:

```bash
echo "https://claude.ai/code/${CLAUDE_CODE_REMOTE_SESSION_ID}"
```

上 bash の出力をセッション URL として本文に埋め込む（プレースホルダー文字列のまま
投稿しない）。

投稿フォーマット:
- ヘッダー: `ICT Weekly Bias Report - YYYY-MM-DD`
- 本文:
  - マクロサマリー（先頭 5 行）
  - 保存先ファイル名（`Calendar/Weekly-Bias/Weekly_Bias_Report_YYYY-MM-DD.md`）
  - セッション URL（上 bash の実値）
  - データ取得失敗があれば警告

## Step 6: ユーザーへの最終応答

- マクロサマリー 5 行
- 保存先フルパス
- データ取得失敗があれば警告

長い本文の再掲示は不要。詳細は Brain 上ファイルで確認する前提。
