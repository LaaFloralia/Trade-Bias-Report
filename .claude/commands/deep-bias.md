---
description: Deep Bias Report（チャート外分析 強化版）を MD で生成（PDF はオプション）
allowed-tools: Bash, Read, Write, Edit, WebSearch, WebFetch
argument-hint: "[pdf]"
---

# Deep Bias Report 生成（強化版）

既存 Daily / Weekly Bias の **速報用** ルートとは独立して、10〜15 分かけて深層リサーチを行い、
資料として読める Markdown を生成する。ローカル（Mac）専用。

- 対象銘柄: DXY / XAUUSD / USDJPY / BTCUSD
- WebSearch クエリ 8〜12 を必ず実行（固定群 a〜h を最低 1 回ずつ）
- 自己検証ステップ（スコア再計算 / 欠損検出 / 矛盾検出）を必須実施
- **デフォルト出力は Markdown のみ**（Brain に master 直接 push）
- **PDF はオプション**: 引数（`$ARGUMENTS`）に `pdf` / `--pdf` / `--with-pdf` のいずれかが含まれる、または社長が会話内で「PDF も」「PDF 付き」等を明示要求した場合のみ追加生成

## 環境変数

| 変数 | デフォルト | 用途 |
|---|---|---|
| `PROJECT_DIR` | `/Users/laa/dev/fundamental-macro-analysis` | チャート外分析リポジトリのパス |
| `BRAIN_PATH` | `$HOME/Brain` | Brain リポジトリのパス |
| `PYTHON_BIN` | `$PROJECT_DIR/.venv/bin/python3` | Python 実行コマンド |

Routines 分岐は不要（ローカル専用）。

## 実行ルール

- 全ステップを順番に実行する。途中失敗時は原因を社長に報告して停止
- 推測で埋めない。取得不可は「取得不可」と明記、推測値は「（推定）」と注記
- 出力 Markdown 内に絵文字を使わない

## PDF オプションの判定（Step 1 開始前に 1 度だけ実施）

以下のいずれかに該当する場合は `WANT_PDF=1` として後続ステップで分岐する。該当しなければ `WANT_PDF=0`（MD のみで完遂）。

- `$ARGUMENTS` に `pdf` / `--pdf` / `--with-pdf` のいずれかが含まれる
- 直近の社長メッセージで「PDF も」「PDF 付き」「PDF 出して」等の明示要求がある

判定理由（`WANT_PDF` の値とその根拠）は Step 9 の最終応答に 1 行で記載する。

## Step 1: スクレイピング実行

`Bash` ツールで以下を実行する。

```bash
PROJECT_DIR="${PROJECT_DIR:-/Users/laa/dev/fundamental-macro-analysis}"
BRAIN_PATH="${BRAIN_PATH:-$HOME/Brain}"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_DIR/.venv/bin/python3}"
export PROJECT_DIR BRAIN_PATH PYTHON_BIN

if [ -z "$PROJECT_DIR" ] || [ ! -d "$PROJECT_DIR" ]; then
  echo "ERROR: PROJECT_DIR not found: '$PROJECT_DIR'" >&2
  exit 1
fi
if [ -z "$BRAIN_PATH" ] || [ ! -d "$BRAIN_PATH" ]; then
  echo "ERROR: BRAIN_PATH not found: '$BRAIN_PATH'" >&2
  exit 1
fi

cd "$PROJECT_DIR" && "$PYTHON_BIN" main.py --weekly
```

実行後、`$PROJECT_DIR/output/scraped_data_YYYY-MM-DD.json` と `.txt` が生成される
（`--weekly` で COT データを含む）。exit code が 0 でなければ以降中止し stderr を社長に報告する。

## Step 2: マスタープロンプトとデータの読み込み

`Read` ツールで以下を読み込む。

- `$PROJECT_DIR/master_prompt_deep.md`
- `$PROJECT_DIR/output/scraped_data_YYYY-MM-DD.txt`（JST 日付）

## Step 3: Deep Research（必須、WebSearch 8〜12 クエリ）

以下 a〜h の **固定群を最低 1 回ずつ** 実行する。状況に応じて追加クエリを最大 12 まで許容。
クエリ実行後、最重要 URL を **3〜5 件選んで WebFetch でクロスチェック** する。
最終的にクエリ群と URL 群を本文末尾「データソース脚注」セクションに列挙する。

### 必須クエリ（a〜h）

a. Fed / ECB / BOJ 高官発言（24 時間、タカ派 / ハト派の傾き）
b. SPX / VIX / NQ の前日比 + 当日プリマーケット
c. US10Y / US2Y の前日比 + Yield Curve スロープ
d. WTI / Brent 価格と前日比
e. 中東 / 台湾 / ウクライナの地政学ヘッドライン
f. BTC 関連の SEC / ETF / 機関買い動向
g. 当日 NFP / CPI / FOMC 等の主要指標予想ブレ
h. リスクオン / オフ系のセンチメント指標（VIX / Fear & Greed / Put-Call 等）

各クエリは `current year` を含めて検索精度を上げる（例: `Fed FOMC member speech hawkish dovish 2026`）。

### 取得結果の本文への反映

- S4-2（中銀発言）/ S9（ニュース・地政学）/ S5（インターマーケット）/ S13（リスク要因）に集約
- 各セクションで参照 URL を本文中に括弧書きで示す
- 取得失敗ソースがあれば最終応答で社長に明示

## Step 4: 分析・レポート生成

`master_prompt_deep.md` の指示に厳密に従い、Markdown 本文を生成する。

メンタルモデル:

「以下のデータを使用して、本日の ICT Deep Bias Report を生成してください。

## 取得済みデータ（最優先で使用すること）

{scraped_data_YYYY-MM-DD.txt の全文}

## 追加リサーチ結果（WebSearch / WebFetch）

{Step 3 で取得した一次情報を URL 付きで整理}

## 指示

- 取得済みデータと追加リサーチ結果を最優先で使用すること
- データ取得不可の項目は『取得不可』と明記すること
- 推測値には必ず『（推定）』と注記すること
- master_prompt_deep.md のセクション順序、テーブル形式、出力ルール、ICT 用語規則、信頼度スコアリング基準に厳密に従うこと
- 字数目安は全体 5000〜8000 字（Stream timeout 対策の制限は適用しない）」

レポートを `Write` ツールで以下に保存する:

- `$PROJECT_DIR/output/Deep_Bias_Report_<JST 日付>.md`

## Step 5: Self-test（必須）

以下 a〜d を順に実施する。

### a. スコア再計算

S12-1 / S12-2 の信頼度スコアを項目別に再計算し、本文値と一致するか検算する。
一致しない場合は本文を修正する。

### b. 空テーブル / 未入力セル走査

本文を `Read` で読み直し、以下を機械的に検出する。

- 空セル（`| |` パターン）が大量に残っているテーブル
- 「取得不可」が連続している場合は、Step 3 のリサーチ結果で埋められないか再検討
- 「（推定）」の使用箇所一覧を本文の S14-2 に記載

### c. セクション間矛盾検出

以下のチェックを `Bash` の `grep` および `Read` で実施する。

- DXY バイアス（S1）と XAUUSD バイアス（S2）の方向が逆相関になっているか
- DXY バイアス（S1）と USDJPY バイアス（S2）の方向が順相関になっているか
- S11 の Draw on Liquidity 方向と S2 の BSL/SSL 注目帯が整合しているか
- S10 の Weekly PO3 と S12 のプラン PO3 フェーズが整合しているか
- S9 のニュース内容と S12 スコア項目 #9（ニュース整合）の判定が整合しているか
- スコアリング表で定義されていない項目が S12 に追加されていないか

### d. 矛盾検出時の再生成

矛盾が検出された場合、**本文を 1 回まで自動修正して再 Write** する。
再生成後も矛盾が残った場合は、本文末尾に以下を注記して完遂する:

```
矛盾検出: <内容>
当該銘柄の信頼度を Low に引き下げました。
```

## Step 6: Render（PDF、オプション）

**`WANT_PDF=1` の場合のみ実行**。`WANT_PDF=0` ならこの Step は丸ごとスキップする。

```bash
cd "$PROJECT_DIR" && "$PYTHON_BIN" scripts/render_report.py \
  "output/Deep_Bias_Report_$(date +%Y-%m-%d).md"
```

成功時、以下が生成される:

- `$PROJECT_DIR/output/Deep_Bias_Report_<JST 日付>.pdf`

HTML は PDF 生成の中間ファイルとして一時生成され、デフォルトで削除される。
デバッグ目的で HTML を残したい場合は `--keep-html` フラグを付ける。
Browser test / プレビュー PNG 生成は廃止された（容量削減のため、2026-05-16）。

## Step 7: Sanity check（ファイルサイズ + PDF ページ数）

MD の範囲チェックは常時実施。PDF 関連の範囲チェックは `WANT_PDF=1` の場合のみ実施する。

```bash
cd "$PROJECT_DIR/output"
TODAY=$(date +%Y-%m-%d)
MD="Deep_Bias_Report_${TODAY}.md"

MD_SIZE=$(stat -f%z "$MD" 2>/dev/null || stat -c%s "$MD")

echo "MD: $MD_SIZE bytes (range 10240-51200)"

WARNINGS=""
[ "$MD_SIZE" -lt 10240 ] || [ "$MD_SIZE" -gt 51200 ] && WARNINGS+="MD size out of range; "

# 以下は WANT_PDF=1 のときのみ実施
if [ "${WANT_PDF:-0}" = "1" ]; then
  PDF="Deep_Bias_Report_${TODAY}.pdf"
  PDF_SIZE=$(stat -f%z "$PDF" 2>/dev/null || stat -c%s "$PDF")

  # PDF ページ数（macOS の mdls を使用、なければ N/A）
  if command -v mdls >/dev/null 2>&1; then
    PDF_PAGES=$(mdls -name kMDItemNumberOfPages -raw "$PDF")
  else
    PDF_PAGES="N/A"
  fi

  echo "PDF: $PDF_SIZE bytes (range 102400-2097152), pages=$PDF_PAGES (range 5-20)"

  [ "$PDF_SIZE" -lt 102400 ] || [ "$PDF_SIZE" -gt 2097152 ] && WARNINGS+="PDF size out of range; "
  if [ "$PDF_PAGES" != "N/A" ] && [ -n "$PDF_PAGES" ]; then
    [ "$PDF_PAGES" -lt 5 ] || [ "$PDF_PAGES" -gt 20 ] && WARNINGS+="PDF pages out of range; "
  fi
fi

if [ -n "$WARNINGS" ]; then
  echo "WARN: $WARNINGS"
fi
```

範囲外の警告は最終応答で社長に明示する。

## Step 8: Brain への push（MD のみ）

HTML / PDF は `output/` のみに留め Brain には置かない。MD のみを Brain に commit。

```bash
TODAY=$(date +%Y-%m-%d)
BRAIN_PATH="${BRAIN_PATH:-$HOME/Brain}"
PROJECT_DIR="${PROJECT_DIR:-/Users/laa/dev/fundamental-macro-analysis}"

mkdir -p "$BRAIN_PATH/Calendar/Deep-Bias"

cp "$PROJECT_DIR/output/Deep_Bias_Report_${TODAY}.md" \
   "$BRAIN_PATH/Calendar/Deep-Bias/Deep_Bias_Report_${TODAY}.md"

cd "$BRAIN_PATH"
git checkout master 2>/dev/null || git checkout main
git pull --rebase origin master 2>/dev/null || git pull --rebase origin main
git add "Calendar/Deep-Bias/Deep_Bias_Report_${TODAY}.md"
git commit -m "Deep Bias $TODAY"
COMMIT_HASH=$(git rev-parse --short HEAD)
git push origin HEAD:master 2>/dev/null || git push origin HEAD:main
echo "commit: $COMMIT_HASH"
```

## Step 9: 社長への最終応答

以下を簡潔に提示する。

1. **エグゼクティブサマリー（S0）の 5 行**
2. **出力ファイルのフルパス**: MD は常に / PDF は `WANT_PDF=1` の場合のみ追記
3. **PDF 判定結果**: `WANT_PDF=0`（MD のみ）or `WANT_PDF=1`（PDF も生成）と、その根拠を 1 行
4. **commit hash**
5. **取得失敗ソース一覧**（あれば）
6. **Sanity check 警告**（Step 7 で範囲外があれば）

長い本文の再掲示は不要。詳細は MD（PDF を生成した場合は PDF も）で確認する前提。
