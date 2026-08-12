---
description: ICT Weekly Bias Report を生成 (Mac ローカル専用、前回レビュー + PDF/Drive 発行付き)
allowed-tools: Bash, Read, Write
---

# ICT Weekly Bias Report 生成

週の開始前（日曜夜 or 月曜早朝 JST）に、過去一週間の値動き、COT、マクロ経済
カレンダー、中銀イベント等を体系的にまとめた週次レポートを生成し、Brain へ保存、
PDF を Google Drive へ発行する。

本コマンドは `/daily-bias` の週次バリアント（Mac ローカル専用）。構造は同一で、
差分は Step 1 の `--weekly`、`master_prompt_weekly.md` の使用、出力先 `Calendar/Weekly-Bias/`。

**銘柄引数は非対応**（Weekly は XAUUSD + DXY 文脈 + マクロ専用。個別銘柄は `/daily-bias <銘柄>` を使う）。

## 環境変数

`daily-bias.md` と同一（`PROJECT_DIR` / `BRAIN_PATH` / `PYTHON_BIN`、Mac ローカル固定）。

## 実行ルール

- 全ステップを順番に実行する。Step 0 と Step 5 (PDF) の失敗は警告して続行、
  それ以外のステップで失敗したら原因をユーザーに報告して中止する。
- レポート本文は推測で埋めず、データ取得不可の項目は `取得不可` と明記する。
- 推測値には必ず `（推定）` と注記する。
- Markdown 内に絵文字を使わない。

## Step 0: XAU-TF 鮮度確認（main.py より先に完了させること）

daily-bias.md Step 0 と同一。

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

## Step 1: スクレイピング実行 (--weekly)

```bash
SECRETS_WRAPPER="$PROJECT_DIR/scripts/run-with-secrets.sh"
if [ -x "$SECRETS_WRAPPER" ] && { [ -f "$HOME/.config/laa/op-service-token" ] || [ -x "$HOME/.config/laa/op-run-batch.sh" ]; }; then
  cd "$PROJECT_DIR" && "$SECRETS_WRAPPER" --batch "$PYTHON_BIN" main.py --weekly
else
  echo "WARN: 1Password 注入が使えないため素の実行 (TwelveData/FRED は取得不可になる)"
  cd "$PROJECT_DIR" && "$PYTHON_BIN" main.py --weekly
fi
```

実行後、`$PROJECT_DIR/output/scraped_data_weekly_YYYY-MM-DD.json` と `.txt` が
生成される（COT は Daily と同様に常時取得。`--weekly` はファイル名 prefix 用）。
exit code が 0 でなければ以降を中止し、stderr の内容をユーザーに報告する。

## Step 2: マスタープロンプトとデータの読み込み

`Read` ツールで以下を読み込む。

必須:
- `$PROJECT_DIR/master_prompt_weekly.md`
- `$PROJECT_DIR/output/scraped_data_weekly_YYYY-MM-DD.txt`（当日分）

**前回レビュー入力は scraped_data に自動注入済み**（`### 前回レビュー入力（前回想定との答え合わせ用）`
ブロック。main.py --weekly が `scrapers/weekly_review.py` で前回 Weekly / 直近 Daily の抜粋と
intel JSON 群を組み立てる。interactive / headless 両フローで同一入力）。

補助（scraped_data のブロックが欠損・不足している場合のみ）:
- `$BRAIN_PATH/Calendar/Weekly-Bias/` の最新 `Weekly_Bias_Report_*.md` を直接 Read してよい

## Step 3: 分析・レポート生成

`master_prompt_weekly.md` のセクション構成・テーブル形式・出力ルールに厳密に従って
Markdown レポートを生成する。

- scraped_data 内の `### 前回レビュー入力` を前回レビュー系セクションの材料にする
- 前回レビュー入力が無い場合は、その旨をレポート内に明記して初回として生成する

レポートは Markdown 形式、テーブル積極使用、絵文字禁止、時刻はすべて JST。
字数は master_prompt_weekly.md の出力ルールに従う（目安: 全体 3500-5000 字）。

## Step 4: レポートを Brain に保存

```bash
mkdir -p "$BRAIN_PATH/Calendar/Weekly-Bias"
```

`Write` ツールで `$BRAIN_PATH/Calendar/Weekly-Bias/Weekly_Bias_Report_YYYY-MM-DD.md` に保存する。

## Step 4.5: Bias-Review-Log への振り返り記録

生成したレポートのセクション1（前回レビュー）の内容から振り返りエントリを組み立て、
`$BRAIN_PATH/Atlas/Bias-Review-Log.md` に追記する。形式・手順は `scrapers/bias_review.py` の
docstring に定義された標準形式に従い、`bias_review.append_entry()` を呼ぶか同形式で直接 Edit する。
セクション1 が「照合不能」のみの場合はスキップする。

## Step 5: PDF 発行（Google Drive）

```bash
cd "$PROJECT_DIR" && "$PROJECT_DIR/.venv/bin/python" scripts/publish_report.py \
  "$BRAIN_PATH/Calendar/Weekly-Bias/Weekly_Bias_Report_$(date +%Y-%m-%d).md"
```

stdout の `PDF:` / `Drive:` 行を控えて Step 7 で提示する。WARN（Drive 未マウント等）
は警告として続行する。

## Step 6: Brain リポジトリへのコミットと push

daily-bias.md Step 6 と同じルール（**master 直接 push、新 claude ブランチ禁止**、
コミット対象は生成したレポートファイルのみ）。

```bash
TODAY=$(date +%Y-%m-%d)

cd "$BRAIN_PATH"
git checkout master 2>/dev/null || git checkout main
git pull --rebase origin master 2>/dev/null || git pull --rebase origin main
git add "Calendar/Weekly-Bias/Weekly_Bias_Report_${TODAY}.md"
# Step 0 で XAU-TF を再生成した場合はそれも同時にコミット（他端末の Obsidian 可視化のため）
git add "Calendar/XAU-TF/XAU_Technical_Report_${TODAY}.md" 2>/dev/null || true
# Step 4.5 の振り返りログ（存在すれば）
git add "Atlas/Bias-Review-Log.md" 2>/dev/null || true
git commit -m "ICT Weekly Bias ${TODAY}"
git push origin HEAD:master 2>/dev/null || git push origin HEAD:main
```

## Step 7: ユーザーへの最終応答

1. レポートのセクション0 (エグゼクティブサマリー) の要点 5 行
2. 保存先 MD のフルパス
3. 生成した PDF のパスと Google Drive コピー先のパス（スキップ時は理由）
4. データ取得失敗があった場合は警告として明示

長い本文の再掲示は不要。詳細は Brain 上のファイルで確認する前提。
