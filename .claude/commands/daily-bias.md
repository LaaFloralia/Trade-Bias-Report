---
description: ICT Daily/Weekly Bias Report を生成 (Mac/Routines 両対応)
argument-hint: [weekly]
allowed-tools: Bash, Read, Write
---

# ICT Daily Bias Report 生成

トレード分析の直前に、チャート外情報 (リテールセンチメント、経済指標、FedWatch、ETFフロー、COT 等)
を体系的にまとめたレポートを生成し、Brain (Obsidian Vault) に保存する。

引数:
- 引数なし: 日次レポート (`master_prompt.md` を使用)
- `weekly`: 週次レポート (`master_prompt_weekly.md` を使用、COT データも含む)

引数: $ARGUMENTS

## 環境変数

このコマンドは以下の環境変数で挙動を切り替える。未定義時のデフォルト値は Mac ローカル前提。

| 変数 | デフォルト | 用途 |
|---|---|---|
| `PROJECT_DIR` | `/Users/laa/dev/fundamental-macro-analysis` | チャート外分析リポジトリ（旧 ict-daily-bias）のパス |
| `BRAIN_PATH` | `$HOME/Brain` | Brain リポジトリのパス |
| `PYTHON_BIN` | `$PROJECT_DIR/.venv/bin/python3` | Python 実行コマンド |
| `SLACK_NOTIFY_CHANNEL` | `#ceo` | Slack 通知先チャンネル |
| `CLAUDE_CODE_REMOTE` | (未定義) | `true` なら Routines 環境と判定 |

Routines 環境 (`CLAUDE_CODE_REMOTE=true`) では、Setup script が依存パッケージのみ
インストールし、リポジトリの clone 先パスはこのコマンド側で動的に検出する
(`find` を使う)。Setup script では `$CLAUDE_ENV_FILE` が未定義のため、
環境変数の引き継ぎは Setup script ではなく当コマンド側の判定で行う。

## 実行ルール

- 全ステップを順番に実行する。途中で失敗したら、原因をユーザーに報告して中止する。
- レポート本文は推測で埋めず、データ取得不可の項目は `取得不可` と明記する。
- 推測値には必ず `（推定）` と注記する。
- 出力する Markdown 内に絵文字を使わない。

## Step 1: スクレイピング実行

`Bash` ツールで以下を実行する。`$ARGUMENTS` が `weekly` の場合は末尾に ` --weekly` を付ける。

環境判定とパスの解決を冒頭で行い、Routines 環境では `find` で動的にパスを検出する。

```bash
# 一般的な clone 先パスを順に試して見つかったものを採用するヘルパー関数
find_repo() {
  local name="$1"
  for base in /home/user /root /workspace /tmp; do
    if [ -d "$base/$name" ]; then
      echo "$base/$name"
      return
    fi
  done
  # フォールバック: /home /root /workspace を限定深度で検索 (高速)
  find /home /root /workspace -maxdepth 4 -type d -name "$name" 2>/dev/null | head -1
}

if [ "$CLAUDE_CODE_REMOTE" = "true" ]; then
  # Routines (クラウド) 環境: clone 先パスを動的検出
  PROJECT_DIR="${PROJECT_DIR:-$(find_repo Trade-Bias-Report)}"
  BRAIN_PATH="${BRAIN_PATH:-$(find_repo Brain)}"
  PYTHON_BIN="${PYTHON_BIN:-python3}"
else
  # ローカル (Mac) 環境: 固定パス
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

# Routines 環境のみ: Playwright Chromium が存在するか確認し、無ければインストール
# Setup script で入れていても、PLAYWRIGHT_BROWSERS_PATH の不一致等で
# 見つからないことがあるため、念のため再インストール (idempotent)
if [ "$CLAUDE_CODE_REMOTE" = "true" ]; then
  if ! "$PYTHON_BIN" -c "from playwright.sync_api import sync_playwright; sync_playwright().__enter__().chromium.launch(headless=True).close()" >/dev/null 2>&1; then
    echo "Playwright chromium not usable, installing..."
    "$PYTHON_BIN" -m pip install playwright >/dev/null 2>&1 || true
    "$PYTHON_BIN" -m playwright install chromium 2>&1 | tail -5
  else
    echo "Playwright chromium OK"
  fi
fi

cd "$PROJECT_DIR" && "$PYTHON_BIN" main.py
```

実行後、`$PROJECT_DIR/output/scraped_data_YYYY-MM-DD.json` と
`$PROJECT_DIR/output/scraped_data_YYYY-MM-DD.txt` が生成される。
exit code が 0 でなければ以降を中止し、stderr の内容をユーザーに報告する。

後続の Step 4 (Brain への保存) と Step 5 (git push) でも同じ `$BRAIN_PATH` /
`$PROJECT_DIR` を再利用するため、上の export を活かしたまま進めること。

## Step 2: マスタープロンプトとデータの読み込み

引数に応じて読み込むファイルを切り替える。`Read` ツールで両方を読み込む。

- 引数なし: `$PROJECT_DIR/master_prompt.md` + `$PROJECT_DIR/output/scraped_data_YYYY-MM-DD.txt`
- `weekly`: `$PROJECT_DIR/master_prompt_weekly.md` + `$PROJECT_DIR/output/scraped_data_weekly_YYYY-MM-DD.txt`

## Step 3: 分析・レポート生成

マスタープロンプトの指示 (セクション0〜7、出力ルール) に厳密に従って Markdown レポートを生成する。
具体的には、以下のメンタルモデルで分析を進める。

「以下のデータを使用して、本日の ICT {Daily|Weekly} Bias Report を生成してください。

## 取得済みデータ (最優先で使用すること)

{scraped_data_YYYY-MM-DD.txt の全文}

## 指示

- 取得済みデータを最優先で使用すること
- データ取得不可の項目は『取得不可』と明記すること
- 推測値には必ず『（推定）』と注記すること
- マスタープロンプトのセクション順序、テーブル形式、出力ルール、ICT用語規則に厳密に従うこと」

レポートは Markdown 形式、テーブル積極使用、絵文字禁止、時刻はすべて JST、
全体 2200-3800 字を目安に、master_prompt の出力ルールに従って生成する。

**重要 (Routines 環境 = `$CLAUDE_CODE_REMOTE` が `true` の場合のみ):**
Anthropic API の stream idle timeout を回避するため、Routines 環境では
以下の制約を **厳守** すること (master_prompt の字数目安より優先する):

- 全体 1500-2000 字以内 (Mac 経路の 2000-3500 字目安より短く)
- 各セクション 200-300 字以内
- テーブルは最低限の重要項目のみ
- 詳細な根拠説明は省略し、結論・数値・方向性のみ記載
- セクション 1.5 (ファンダ大局バイアス) はテーブルのみ (補足行なし)。省略はしない
- セクション 4 (FedWatch・中銀)、5 (インターマーケット)、6 (PO3) は
  本日該当事項なしなら「該当なし」の 1 行で省略
- セクション 7 (注目ポイント) のみ詳細に書いて良い (最大 400 字)

簡潔化の目的は品質低下ではなく、Write tool の引数構築時間を短縮し
タイムアウトを防ぐこと。詳細分析はローカル (Mac) 経路で行う想定。

## Step 4: レポートを Brain に保存

ファイル名は `Daily_Bias_Report_YYYY-MM-DD.md` (または `Weekly_Bias_Report_YYYY-MM-DD.md`)。
日付は実行時の JST 日付を使う。

出力先:
- 日次: `$BRAIN_PATH/Calendar/Daily-Bias/Daily_Bias_Report_YYYY-MM-DD.md`
- 週次: `$BRAIN_PATH/Calendar/Weekly-Bias/Weekly_Bias_Report_YYYY-MM-DD.md`

ディレクトリが存在しなければ以下を実行してから、`Write` ツールでレポートを保存する。

```bash
BRAIN_PATH="${BRAIN_PATH:-$HOME/Brain}"
mkdir -p "$BRAIN_PATH/Calendar/Daily-Bias" "$BRAIN_PATH/Calendar/Weekly-Bias"
```

## Step 5: Routines 環境のみ追加処理

環境変数 `CLAUDE_CODE_REMOTE` が `true` の場合のみ、以下を追加で実施する。
ローカル (Mac) 実行ではこの Step を完全にスキップする。

### 5-1. Brain リポジトリへのコミットと push

**重要**: Brain リポジトリへの push は必ず `master` ブランチに直接行うこと。
新しい `claude/...` ブランチを作って push してはならない。社長の Mac は
master ブランチを `git pull` するだけで取得できる運用のため。

```bash
BRAIN_PATH="${BRAIN_PATH:-$HOME/Brain}"
SUBDIR="Daily-Bias"  # weekly なら "Weekly-Bias"
PREFIX="Daily_Bias_Report"  # weekly なら "Weekly_Bias_Report"
TITLE="ICT Daily Bias"  # weekly なら "ICT Weekly Bias"
TODAY=$(date +%Y-%m-%d)

cd "$BRAIN_PATH"

# master ブランチを明示的にチェックアウト (新ブランチを作らない)
git checkout master 2>/dev/null || git checkout main

# リモートの最新を取り込んでから commit (競合回避)
git pull --rebase origin master 2>/dev/null || git pull --rebase origin main

git add "Calendar/$SUBDIR/${PREFIX}_${TODAY}.md"
git commit -m "$TITLE $TODAY"

# 必ず master/main に直接 push (claude/... ブランチを作らない)
git push origin HEAD:master 2>/dev/null || git push origin HEAD:main
```

git push が認証エラーで失敗した場合、Routine の Repositories 設定で
`Allow unrestricted branch pushes` が Brain リポジトリで有効化されているか
確認する旨をユーザーに報告する (本来は ON のはず)。

### 5-2. Slack へ通知

Slack MCP 経由で `#ceo` チャンネルに以下を投稿する
(環境変数 `SLACK_NOTIFY_CHANNEL` が設定されていれば、その値を優先)。

**重要**: 投稿本文に含めるセッション URL は、必ず以下の bash で取得した実際の値を使用すること。
プレースホルダー `${CLAUDE_CODE_REMOTE_SESSION_ID}` の文字列をそのまま投稿してはならない。

```bash
echo "https://claude.ai/code/${CLAUDE_CODE_REMOTE_SESSION_ID}"
```

→ 出力された URL (例: `https://claude.ai/code/session_01HJK...`) を本文に埋め込む。

投稿フォーマット:

- ヘッダー: `ICT {Daily|Weekly} Bias Report - YYYY-MM-DD`
- 本文:
  - レポートのセクション0 (エグゼクティブサマリー) を抜粋 (5行)
  - 保存先ファイル名 (例: `Calendar/Daily-Bias/Daily_Bias_Report_2026-04-19.md`)
  - セッション URL (上記 bash の結果を文字列として埋め込み)
  - データ取得失敗があった場合は警告として明示

## Step 6: ユーザーへの最終応答

以下の3点を簡潔に提示する。

1. レポートのセクション0 (エグゼクティブサマリー) の5行
2. 保存先のフルパス
3. データ取得失敗があった場合は警告として明示

長い本文の再掲示は不要。詳細は Brain 上のファイルで確認する前提。
