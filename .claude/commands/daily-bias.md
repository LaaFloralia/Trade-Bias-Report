---
description: ICT Daily Bias Report を生成 (Mac ローカル専用、PDF + Google Drive 発行付き)
allowed-tools: Bash, Read, Write
---

# ICT Daily Bias Report 生成

トレード分析の直前に、チャート外情報 (リテールセンチメント、COT、経済指標、FedWatch、
ETF フロー等) を体系的にまとめたレポートを生成し、Brain (Obsidian Vault) へ保存、
PDF を Google Drive へ発行する。

このコマンドは Mac ローカル専用。週次レポートは `/weekly-bias` を使う（引数分岐はない）。

## 環境変数

| 変数 | デフォルト | 用途 |
|---|---|---|
| `PROJECT_DIR` | `/Users/laa/dev/fundamental-macro-analysis` | チャート外分析リポジトリのパス |
| `BRAIN_PATH` | `$HOME/Brain` | Brain リポジトリのパス |
| `PYTHON_BIN` | `$PROJECT_DIR/.venv/bin/python3` | Python 実行コマンド |

## 実行ルール

- 全ステップを順番に実行する。Step 0 と Step 5 (PDF) の失敗は警告して続行、
  それ以外のステップで失敗したら原因をユーザーに報告して中止する。
- レポート本文は推測で埋めず、データ取得不可の項目は `取得不可` と明記する。
- 推測値には必ず `（推定）` と注記する。
- 出力する Markdown 内に絵文字を使わない。

## Step 0: XAU-TF 鮮度確認（main.py より先に完了させること）

`report_anchor` が main.py 実行時に `$BRAIN_PATH/Calendar/XAU-TF/` の最新レポートを
読み込むため、**Step 1 の前に**鮮度を確認し、古ければ再生成する。

```bash
PROJECT_DIR="${PROJECT_DIR:-/Users/laa/dev/fundamental-macro-analysis}"
BRAIN_PATH="${BRAIN_PATH:-$HOME/Brain}"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_DIR/.venv/bin/python3}"
export PROJECT_DIR BRAIN_PATH PYTHON_BIN

TODAY=$(date +%Y-%m-%d)
YESTERDAY=$(date -v-1d +%Y-%m-%d)
XAU_TF_DIR="$BRAIN_PATH/Calendar/XAU-TF"

if [ ! -f "$XAU_TF_DIR/XAU_Technical_Report_${TODAY}.md" ] && \
   [ ! -f "$XAU_TF_DIR/XAU_Technical_Report_${YESTERDAY}.md" ]; then
  echo "XAU-TF レポートが当日/前日分とも無いため再生成する (約 1 分)..."
  (cd /Users/laa/dev/xauusd-smc-quant && .venv/bin/python run_report.py --fetch) || \
    echo "WARN: XAU-TF 生成に失敗。report_anchor は直近ファイル (stale) または無しで続行する"
else
  echo "XAU-TF レポートは新鮮 (当日または前日分あり)"
fi
```

## Step 1: スクレイピング実行

TWELVEDATA_API_KEY / FRED_API_KEY は 1Password 経由（`run-with-secrets.sh`）でのみ注入される。

```bash
SECRETS_WRAPPER="$PROJECT_DIR/scripts/run-with-secrets.sh"
if [ -x "$SECRETS_WRAPPER" ] && [ -f "$HOME/.config/laa/op-service-token" ]; then
  cd "$PROJECT_DIR" && "$SECRETS_WRAPPER" --batch "$PYTHON_BIN" main.py
else
  echo "WARN: 1Password 注入が使えないため素の実行 (TwelveData/FRED は取得不可になる)"
  cd "$PROJECT_DIR" && "$PYTHON_BIN" main.py
fi
```

COT は常時取得化済みのため `--weekly` フラグは付けない。
実行後、`$PROJECT_DIR/output/scraped_data_YYYY-MM-DD.json` と `.txt` が生成される。
exit code が 0 でなければ以降を中止し、stderr の内容をユーザーに報告する。

## Step 2: マスタープロンプトとデータの読み込み

`Read` ツールで以下の 2 ファイルを読み込む。

- `$PROJECT_DIR/master_prompt.md`
- `$PROJECT_DIR/output/scraped_data_YYYY-MM-DD.txt`（当日分）

## Step 3: 分析・レポート生成

マスタープロンプトの指示 (セクション構成、テーブル形式、出力ルール、ICT 用語規則) に
厳密に従って Markdown レポートを生成する。以下のメンタルモデルで分析を進める。

「以下のデータを使用して、本日の ICT Daily Bias Report を生成してください。

## 取得済みデータ (最優先で使用すること)

{scraped_data_YYYY-MM-DD.txt の全文}

## 指示

- 取得済みデータを最優先で使用すること
- データ取得不可の項目は『取得不可』と明記すること
- 推測値には必ず『（推定）』と注記すること
- マスタープロンプトのセクション順序、テーブル形式、出力ルール、ICT用語規則に厳密に従うこと」

レポートは Markdown 形式、テーブル積極使用、絵文字禁止、時刻はすべて JST。
字数は master_prompt.md の出力ルールに従う（目安: 全体 2400-3800 字）。

## Step 4: レポートを Brain に保存

ファイル名は `Daily_Bias_Report_YYYY-MM-DD.md`（実行時の JST 日付）。

```bash
mkdir -p "$BRAIN_PATH/Calendar/Daily-Bias"
```

`Write` ツールで `$BRAIN_PATH/Calendar/Daily-Bias/Daily_Bias_Report_YYYY-MM-DD.md` に保存する。

## Step 5: PDF 発行（Google Drive）

```bash
cd "$PROJECT_DIR" && "$PROJECT_DIR/.venv/bin/python" scripts/publish_report.py \
  "$BRAIN_PATH/Calendar/Daily-Bias/Daily_Bias_Report_$(date +%Y-%m-%d).md"
```

- stdout の `PDF:` / `Drive:` 行を控えて Step 7 の最終応答に含める。
- publish_report.py はソフト障害 (Drive 未マウント等) を WARN + exit 0 で飲み込む設計。
  WARN が出た場合はその旨を最終応答に含めて続行する。

## Step 6: Brain リポジトリへのコミットと push

Brain の同期は Git が正（他端末は launchd の auto-pull / Working Copy で git 経由取得）
のため、コミットしないレポートは他端末の Obsidian に表示されない。コミット対象は
生成したレポートファイルのみとし、vault 内の他の未コミットファイル（社長の個人メモ等）
を `git add` に含めないこと。

**重要**: Brain リポジトリへの push は必ず `master` ブランチに直接行うこと。
新しい `claude/...` ブランチを作って push してはならない。社長の Mac は
master ブランチを `git pull` するだけで取得できる運用のため。

```bash
TODAY=$(date +%Y-%m-%d)

cd "$BRAIN_PATH"

# master ブランチを明示的にチェックアウト (新ブランチを作らない)
git checkout master 2>/dev/null || git checkout main

# リモートの最新を取り込んでから commit (競合回避)
git pull --rebase origin master 2>/dev/null || git pull --rebase origin main

git add "Calendar/Daily-Bias/Daily_Bias_Report_${TODAY}.md"
# Step 0 で XAU-TF を再生成した場合はそれも同時にコミット（他端末の Obsidian 可視化のため）
git add "Calendar/XAU-TF/XAU_Technical_Report_${TODAY}.md" 2>/dev/null || true
git commit -m "ICT Daily Bias ${TODAY}"

# 必ず master/main に直接 push (claude/... ブランチを作らない)
git push origin HEAD:master 2>/dev/null || git push origin HEAD:main
```

push が認証エラーで失敗した場合は、その旨をユーザーに報告する
（レポート生成自体は完了扱いで良い）。

## Step 7: ユーザーへの最終応答

以下を簡潔に提示する。

1. レポートのセクション0 (エグゼクティブサマリー) の要点 5 行
2. 保存先 MD のフルパス
3. 生成した PDF のパスと Google Drive コピー先のパス（スキップされた場合はその理由）
4. データ取得失敗があった場合は警告として明示

長い本文の再掲示は不要。詳細は Brain 上のファイルで確認する前提。
