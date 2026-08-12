---
description: ICT Daily Bias Report を生成 (Mac ローカル専用、PDF + Google Drive 発行付き)
argument-hint: "[銘柄 (省略 = XAUUSD。USDJPY / BTCUSD はスリム版)]"
allowed-tools: Bash, Read, Write
---

# ICT Daily Bias Report 生成

トレード分析の直前に、チャート外情報 (リテールセンチメント、オーダーブック由来の
リクイディティプール、COT、経済指標、FedWatch、ETF フロー等) を体系的にまとめた
レポートを生成し、Brain (Obsidian Vault) へ保存、PDF を Google Drive へ発行する。

このコマンドは Mac ローカル専用。週次レポートは `/weekly-bias` を使う。

## 引数（$ARGUMENTS）

- **空（引数なし）**: デフォルト = **XAUUSD 特化デイリー**（DXY 上流文脈込みのフル版）。定時実行と同一
- **`USDJPY` / `BTCUSD`**: 個別銘柄スリム版デイリー。XAU-TF・金フロー・リテール分析は含まれない
- 上記以外の引数はエラーとしてユーザーに確認する（`XAUUSD` 指定はデフォルトと同じ扱い）

以下、引数ありの場合の分岐を各 Step に `【銘柄指定時】` として記す。

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

`report_anchor` が main.py 実行時に `$BRAIN_PATH/Calendar/XAU-TF/` の最新レポートを読み込み、
`retail_analytics` が xauusd-smc-quant の `data/live-h1.csv` をスイープ検証に使うため、
**Step 1 の前に**鮮度を確認し、古ければ再生成する。

**【銘柄指定時】** XAU-TF は XAUUSD 専用のため、このステップをスキップして Step 1 へ進む。

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

TWELVEDATA_API_KEY / FRED_API_KEY は 1Password 経由（`run-with-secrets.sh --batch`。
サービストークンは Keychain 管理の `~/.config/laa/op-run-batch.sh` にフォールバックする）でのみ注入される。

```bash
SECRETS_WRAPPER="$PROJECT_DIR/scripts/run-with-secrets.sh"
if [ -x "$SECRETS_WRAPPER" ] && { [ -f "$HOME/.config/laa/op-service-token" ] || [ -x "$HOME/.config/laa/op-run-batch.sh" ]; }; then
  cd "$PROJECT_DIR" && "$SECRETS_WRAPPER" --batch "$PYTHON_BIN" main.py
else
  echo "WARN: 1Password 注入が使えないため素の実行 (TwelveData/FRED は取得不可になる)"
  cd "$PROJECT_DIR" && "$PYTHON_BIN" main.py
fi
```

**【銘柄指定時】** `main.py` に `--symbol <銘柄>` を付ける（例: `main.py --symbol USDJPY`）。

COT は常時取得化済みのため `--weekly` フラグは付けない。
実行後、`$PROJECT_DIR/output/scraped_data_YYYY-MM-DD.json` と `.txt`
（**【銘柄指定時】** `scraped_data_<銘柄>_YYYY-MM-DD.*`）が生成される。
exit code が 0 でなければ以降を中止し、stderr の内容をユーザーに報告する。

## Step 2: マスタープロンプトとデータの読み込み

`Read` ツールで以下の 2 ファイルを読み込む。

- `$PROJECT_DIR/master_prompt.md`
- `$PROJECT_DIR/output/scraped_data_YYYY-MM-DD.txt`（当日分）

**【銘柄指定時】** 代わりに以下を読む。

- `$PROJECT_DIR/master_prompt_symbol.md`（`{{SYMBOL}}` を指定銘柄に読み替える）
- `$PROJECT_DIR/output/scraped_data_<銘柄>_YYYY-MM-DD.txt`

## Step 3: 分析・レポート生成

マスタープロンプトの指示 (セクション構成、テーブル形式、出力ルール、ICT 用語規則) に
厳密に従って Markdown レポートを生成する。以下のメンタルモデルで分析を進める。

「以下のデータを使用して、本日の ICT Daily Bias Report を生成してください。

## 取得済みデータ (最優先で使用すること)

{scraped_data の全文}

## 指示

- 取得済みデータを最優先で使用すること
- データ取得不可の項目は『取得不可』と明記すること
- 推測値には必ず『（推定）』と注記すること
- マスタープロンプトのセクション順序、テーブル形式、出力ルール、ICT用語規則に厳密に従うこと」

レポートは Markdown 形式、テーブル積極使用、絵文字禁止、時刻はすべて JST。
字数はマスタープロンプトの出力ルールに従う（デフォルト版 2,400-3,800 字 / スリム版 1,200-2,000 字）。

## Step 4: レポートを Brain に保存

ファイル名は `Daily_Bias_Report_YYYY-MM-DD.md`（実行時の JST 日付）。

```bash
mkdir -p "$BRAIN_PATH/Calendar/Daily-Bias"
```

`Write` ツールで `$BRAIN_PATH/Calendar/Daily-Bias/Daily_Bias_Report_YYYY-MM-DD.md` に保存する。

**【銘柄指定時】** 保存先は **`$BRAIN_PATH/Calendar/Daily-Bias/<銘柄>/Daily_Bias_Report_<銘柄>_YYYY-MM-DD.md`**
（サブディレクトリ必須。フラット配置すると report_anchor の glob が XAUUSD の前回 Daily と誤認するため）。

## Step 4.5: Bias-Review-Log への振り返り記録（デフォルト版のみ）

**【銘柄指定時】はスキップ。** デフォルト版では、生成したレポートのセクション8-1（前回照合）の内容から
振り返りエントリを組み立て、`$BRAIN_PATH/Atlas/Bias-Review-Log.md` に追記する。
形式・手順はプロジェクトの `scrapers/bias_review.py` の docstring に定義された標準形式に従い、
`python -c` 等で `bias_review.append_entry()` を呼ぶか、同形式で直接 Edit する。
前回照合が「前回Daily未提供」の場合はこのステップをスキップする。

## Step 5: PDF 発行（Google Drive）

```bash
cd "$PROJECT_DIR" && "$PROJECT_DIR/.venv/bin/python" scripts/publish_report.py \
  "$BRAIN_PATH/Calendar/Daily-Bias/Daily_Bias_Report_$(date +%Y-%m-%d).md"
```

**【銘柄指定時】** 引数をサブディレクトリのファイルパスに読み替える。

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
# Step 4.5 の振り返りログ（存在すれば）
git add "Atlas/Bias-Review-Log.md" 2>/dev/null || true
git commit -m "ICT Daily Bias ${TODAY}"

# 必ず master/main に直接 push (claude/... ブランチを作らない)
git push origin HEAD:master 2>/dev/null || git push origin HEAD:main
```

**【銘柄指定時】** `git add` の対象を `Calendar/Daily-Bias/<銘柄>/Daily_Bias_Report_<銘柄>_${TODAY}.md` に
読み替え、コミットメッセージは `ICT Daily Bias (<銘柄>) ${TODAY}` とする。XAU-TF / Bias-Review-Log は対象外。

push が認証エラーで失敗した場合は、その旨をユーザーに報告する
（レポート生成自体は完了扱いで良い）。

## Step 7: ユーザーへの最終応答

以下を簡潔に提示する。

1. レポートのセクション0 (エグゼクティブサマリー) の要点 5 行
2. 保存先 MD のフルパス
3. 生成した PDF のパスと Google Drive コピー先のパス（スキップされた場合はその理由）
4. データ取得失敗があった場合は警告として明示

長い本文の再掲示は不要。詳細は Brain 上のファイルで確認する前提。
