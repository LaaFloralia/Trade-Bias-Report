# ICT Daily / Weekly Bias Report — 自動生成パイプライン

> **2026-05-09 リネーム**: フォルダ名を `ict-daily-bias` → `fundamental-macro-analysis` に変更。
> 社長は本プロジェクトを「**チャート外分析**」と呼ぶ（リテールセンチメント / 経済指標 / FedWatch / ETFフロー / COT 等、チャート上の値動き以外の情報を体系化するため）。
> Output report 名（"ICT Daily Bias Report" / "ICT Weekly Bias Report"）、GitHub repo 名（`Trade-Bias-Report`）、Sovereign Stack skill / Routine 識別子（`ict-daily-bias` / `ict-weekly-bias`）は **変更なし**（識別子として継続使用）。

Playwright（ヘッドレスブラウザ）でリテールセンチメントデータと経済指標を取得し、
Claude API にマスタープロンプトとデータを渡してレポートを生成する。
出力は Markdown として Obsidian Vault（Brain）に保存される。

> **次世代化メモ**: 2026-05-05 リリースの Anthropic 公式 financial-services プラグイン（Financial Modeling Prep MCP 含む）で、Twelve Data + DXY/US10Y/economic_calendar スクレイピングを大幅に置き換え可能な見込み。詳細は `MODERNIZATION_RESEARCH.md` を参照。

本リポジトリは **Sovereign Stack の Flow D（ad-hoc 起動）から呼び出される** ことを前提に設計されている。
**Full 展開時**: 自宅母艦に SSH → `~/HQ/bin/run-skill ict-daily-bias` で起動（skill 識別子は継続）。
**Bootstrap 期**: Anthropic Claude Code Routines のクラウド環境で Run now 起動。
ローカル実行（MacBook 直叩き）も可能。

---

## 1. 前提条件

- Python 3.11+
- Anthropic API キー（https://console.anthropic.com/）
- Node.js 18+（Playwright 用）
- Twelve Data API キー（価格取得用）
- **FRED API キー**（Treasury yields + Broad USD Index、無料、https://fred.stlouisfed.org/）— **macOS Keychain 推奨**（service: `FRED_API_KEY`、account: `$USER`、`-A` silent access）

---

## 2. ローカルセットアップ

### 2-1. 依存パッケージ

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 2-2. 環境変数

```bash
cp .env.example .env
# .env を編集して以下を設定:
# - ANTHROPIC_API_KEY
# - TWELVEDATA_API_KEY
# - OBSIDIAN_VAULT_PATH（Brain へのローカルパス）

# FRED API key は macOS Keychain (-A silent access) 推奨:
# 1. https://fred.stlouisfed.org/ で API key を取得（無料）
# 2. 1Password vault に「FRED API」item として保管（正本）
# 3. ワンタイム bootstrap で Keychain へ転記（API key 値は echo しない）:
#    op signin
#    KEY=$(op item get "FRED API" --field credential --reveal 2>/dev/null \
#          || op item get "FRED API" --field password   --reveal 2>/dev/null \
#          || op item get "FRED API" --field "api key"  --reveal 2>/dev/null)
#    [ -n "$KEY" ] && security add-generic-password -a "$USER" -s "FRED_API_KEY" -w "$KEY" -A -U && unset KEY
# 4. ランタイムは Python が Keychain から自動取得（解決順: env → Keychain → .env）
```

### 2-3. 実行

```bash
python main.py                # Daily Bias
python main.py --weekly       # Weekly Bias（実装に応じて）
```

---

## 3. ファイル構成

```
fundamental-macro-analysis/  # 旧 ict-daily-bias、社長呼称「チャート外分析」
├── README.md                 # このファイル
├── MODERNIZATION_RESEARCH.md # 2026-05-09 調査: Anthropic 公式 financial-services プラグイン / FMP MCP による置換計画
├── requirements.txt          # Python 依存パッケージ
├── .env.example              # 環境変数テンプレート
├── main.py                   # メインオーケストレーター
├── config.py                 # 設定読み込み
├── master_prompt.md          # ICT Daily Bias マスタープロンプト（速報用）
├── master_prompt_weekly.md   # ICT Weekly Bias マスタープロンプト（速報用）
├── master_prompt_deep.md     # ICT Deep Bias マスタープロンプト（強化版、S0〜S14 / 信頼度 11 項目）
├── .claude/
│   └── commands/
│       ├── daily-bias.md     # Routine から参照されるスラッシュコマンド（速報用、Mac/Routines 両対応）
│       ├── weekly-bias.md    # 週次速報用（Mac/Routines 両対応）
│       └── deep-bias.md      # Deep Bias 強化版（ローカル専用、Deep Research + 自己検証 + 3 形式出力）
├── scripts/
│   └── render_report.py      # MD → PDF レンダラ（markdown + Playwright、HTML は中間生成→削除）
├── templates/
│   ├── report.html           # PDF 用 A4 テンプレ（表紙 + 本文 + フッタ）
│   └── style.css             # 印刷用 CSS（システムフォント、テーブル、バッジ、@page）
├── scrapers/
│   ├── __init__.py
│   ├── myfxbook.py           # MyFXBook センチメント取得
│   ├── fxssi.py              # FXSSI Current Ratio 取得
│   ├── ig_sentiment.py       # IG Client Sentiment 取得
│   ├── coinglass.py          # CoinGlass L/S Ratio + Funding Rate
│   ├── economic_calendar.py  # 経済指標カレンダー（Investing.com）
│   ├── dxy.py                # DXY スクレイピング（Investing.com / MarketWatch / Stooq）
│   ├── twelvedata.py         # Twelve Data API（XAUUSD/USDJPY/BTCUSD OHLCV）
│   ├── cot.py                # CFTC COT 公式 API
│   ├── btc_etf.py            # BTC ETF フロー
│   ├── fedwatch.py           # CME FedWatch（FOMC週のみ）
│   ├── fred.py               # FRED: DGS10 (US10Y) / DGS2 (US2Y) / DTWEXBGS (Broad USD Index, ≠ DXY)
│   ├── binance_btc_sentiment.py  # Binance Futures BTC Long/Short（MyFXBook 非対応の代替、2026-05-16 追加）
│   └── validation.py         # データバリデーション
└── output/                   # ローカル出力（Brain にも commit）
```

---

## 4. Sovereign Stack での運用

本リポジトリは Sovereign Stack の **Flow D（ad-hoc 起動）** から呼び出される。共通の AI Runtime 設定・launchd / webhook テンプレート・トラブル一覧は `~/HQ/infrastructure/runtime-setup.md` を参照。**本 README はこのリポジトリ固有の設定のみを扱う。**

- **Full（自宅母艦取得後）**: iPhone Termius / MacBook ターミナルから母艦 SSH → `~/HQ/bin/run-skill ict-daily-bias`（または Slack `/laa daily-bias` slash command）
- **Bootstrap 期（自宅母艦未取得）**: `claude.ai/code/routines` で Routine を 2 本作成し Run now で起動（API trigger の `text` は非パースのため、Daily / Weekly は **Routine を 2 つに分割**する）

### 4-1. Bootstrap 期の Routine 設定（`claude.ai/code/routines`）

| 項目 | ict-daily-bias | ict-weekly-bias |
|---|---|---|
| Trigger | API（Run now 起動用） | API（Run now 起動用） |
| Repositories | `LaaQuantumFund/Trade-Bias-Report` + `LaaQuantumFund/Brain` | 同左 |
| Allow unrestricted branch pushes | **両 repo とも ON** | 同左 |
| Connectors | **Slack のみ**（Linear / GitHub は外す） | 同左 |
| Model | **Sonnet 4.6**（Opus 4.7 は Stream timeout 多発） | 同左 |
| Network access | **Full**（外部サイトのスクレイピング必要） | 同左 |

### 4-2. Environment variables

| 変数 | 値 | 出典 |
|---|---|---|
| `TWELVEDATA_API_KEY` | **Full** = Keychain `twelvedata-api-key` から `.zshrc` で export / **Bootstrap** = Routine ごとに UI で個別登録 | Twelve Data（価格 API）|
| `ANTHROPIC_API_KEY` | **Full** = `claude` CLI が自動解決 / **Bootstrap** = Routines が自動注入 | — |

### 4-3. Setup script（Bootstrap 期のみ）

`~/HQ/infrastructure/runtime-setup.md § 9-3` の標準テンプレートを使い、`<REPO_NAME>` を `Trade-Bias-Report` に置換する。Setup script 内で `$CLAUDE_ENV_FILE` は**使わない**（罠参照）。Full 展開時は不要（母艦のローカル環境で完結）。

### 4-4. プロンプト

Routine / `run-skill` から渡すプロンプトはシンプルに以下のみ記載し、本体ロジックは `.claude/commands/*.md` で版管理する:

```
リポジトリ内の .claude/commands/daily-bias.md を Read で読み込み、その指示に従って実行せよ。

重要な遵守事項:
- Brain への commit は master ブランチに直接 commit し push すること
- Slack に通知する際のセッション URL は実値を埋め込むこと（${CLAUDE_CODE_REMOTE_SESSION_ID} のまま出力しない）
- 出力レポートは 1500〜2000 字以内に簡潔化（Stream timeout 対策）
```

Weekly も同じ構造で `weekly-bias.md` を参照する。

### 4-5. 起動の具体例

- **Full（推奨）**: iPhone Termius で母艦 SSH → `~/HQ/bin/run-skill ict-daily-bias` / Slack `/laa daily-bias` slash command（母艦 webhook daemon 経由、Flow C）
- **Bootstrap**: iPhone Safari で `claude.ai/code/routines` → 対象 Routine の **Run now** / MacBook で CLI `/schedule run ict-daily-bias`

---

## 5. スクレイパー実装の鉄則

### 5-1. Playwright の SSL 対策（Routines 環境で必須）

Anthropic セキュリティプロキシの自己署名証明書を信頼するため、
`new_context()` には必ず `ignore_https_errors=True` を指定する。
これがないと Routines 環境で SSL 検証エラーで全スクレイパーが失敗する。

```python
context = await browser.new_context(
    user_agent=USER_AGENT,
    ignore_https_errors=True,  # Routines 環境で必須
)
```

### 5-2. User-Agent

- 各 `scrapers/*.py` で同一の User-Agent 文字列を使う
- サイト側のブロックを受けた場合は、User-Agent を実ブラウザ相当に更新

### 5-3. セレクタ保守

スクレイピング対象サイトは UI 変更が入ることがある。セレクタが壊れたら:

```bash
claude "scrapers/myfxbook.py のセレクタが壊れている。サイトを確認して修正して"
```

（Claude Code のローカル実行で修正 → PR → マージ）

---

## 6. スラッシュコマンド（`.claude/commands/*.md`）の設計鉄則

Routine から参照される `.claude/commands/daily-bias.md` および `weekly-bias.md` は、以下の鉄則に従う:

### 6-1. 環境判定

```bash
if [ "$CLAUDE_CODE_REMOTE" = "true" ]; then
  # Routines 環境
else
  # ローカル環境
fi
```

### 6-2. プロジェクトパス解決

- ローカル: 固定値（例: `~/dev/fundamental-macro-analysis`）
- Routines: for ループ + find で clone 先を検出（`~/HQ/infrastructure/runtime-setup.md` § 8 の Setup script と同じロジック）

### 6-3. Brain への push

```bash
git checkout master
git pull --rebase origin master   # 競合回避
git push origin HEAD:master        # 新ブランチを作らない
```

### 6-4. Slack 通知時のセッション URL

```bash
SESSION_URL="https://claude.ai/code/${CLAUDE_CODE_REMOTE_SESSION_ID}"
# SESSION_URL を Slack 投稿本文に埋め込む（変数名のまま投稿しない）
```

---

## 7. カスタマイズ

- **マスタープロンプト変更**: `master_prompt.md` / `master_prompt_weekly.md` / `master_prompt_deep.md` を編集
- **銘柄追加・変更**: `config.py` の `INSTRUMENTS` を編集
- **スクレイピング対象サイト変更**: 各 `scrapers/*.py` を編集

---

## 7.5. Deep Bias（強化版）

`master_prompt.md` / `master_prompt_weekly.md` は **速報用**（1500〜3500 字、Routines / Mac 両対応）。
これとは別に、10〜15 分かけて深層リサーチを行う **強化版** を並走させている。
速報用ファイル群は変更せず温存し、Deep Bias は独立ファイルとして並走する。

### 7.5-1. 既存 Daily / Weekly との違い

| 項目 | Daily / Weekly（速報） | **Deep Bias（強化版）** |
|---|---|---|
| 実行コマンド | `/daily-bias` / `/weekly-bias` | **`/deep-bias`** |
| 所要時間 | 2〜3 分 | **10〜15 分** |
| 字数目安 | 1500〜3500 字 | **5000〜8000 字** |
| 出力形式 | MD のみ | **MD のみ**（PDF は引数 `pdf` または明示要求時のみ追加生成、HTML は中間ファイル） |
| WebSearch | なし | **8〜12 クエリ必須**（固定群 a〜h を最低 1 回ずつ） |
| 自己検証 | なし | **スコア再計算 / 欠損検出 / 矛盾検出 を必須実施** |
| 実行環境 | Mac / Routines 両対応 | **Mac ローカル専用** |
| 信頼度スコア項目 | 6〜7 項目 | **11 項目（ニュース / 地政学 / 季節性を追加）** |
| Brain への push | 速報 MD | **MD のみ**（PDF は `output/` のみ保持） |
| プロンプトファイル | `master_prompt.md` / `master_prompt_weekly.md` | `master_prompt_deep.md` |

### 7.5-2. 実行

```bash
# Claude Code セッション内で:
/deep-bias
```

または以下を直接実行（プロンプト経由）。

```
リポジトリ内の .claude/commands/deep-bias.md を Read で読み込み、その指示に従って実行せよ。
```

### 7.5-3. 出力の置き場所

デフォルトは Markdown のみを生成し Brain に push する。PDF はオプションで、
`/deep-bias pdf`（または `/deep-bias-weekly pdf`）のように引数を明示するか、
社長が会話で「PDF も」「PDF 付き」等を要求した場合のみ追加生成される。

HTML は PDF レンダリング時の中間ファイルとして一時生成し、PDF 生成後に削除する
（`--keep-html` フラグで保持可能）。Browser test / プレビュー PNG の永続出力は廃止済み（2026-05-16）。

| 形式 | パス | 備考 |
|---|---|---|
| Markdown | `output/Deep_Bias_Report_YYYY-MM-DD.md` | **常時生成**。Brain に master 直接 push |
| PDF | `output/Deep_Bias_Report_YYYY-MM-DD.pdf` | **オプション**: 明示要求時のみ生成 / A4 / 5〜20 ページ目安 / Brain には置かない |
| Brain 側 | `~/Brain/Calendar/Deep-Bias/Deep_Bias_Report_YYYY-MM-DD.md` | MD のみ commit + push |

### 7.5-4. ネットリサーチ 8〜12 クエリの内訳

必須群（a〜h、各 1 回以上、合計 8 クエリ。状況に応じて最大 12 まで追加可）:

| # | カテゴリ | 例 |
|---|---|---|
| a | Fed / ECB / BOJ 高官発言（24h、タカ派 / ハト派） | `Fed FOMC member speech hawkish dovish 2026` |
| b | SPX / VIX / NQ 前日比 + 当日プリマーケット | `SPX VIX NQ premarket today 2026` |
| c | US10Y / US2Y 前日比 + Yield Curve | `US10Y US2Y yield curve slope 2026` |
| d | WTI / Brent 価格と前日比 | `WTI Brent crude oil price today 2026` |
| e | 中東 / 台湾 / ウクライナの地政学 | `Middle East Taiwan Ukraine geopolitical news 2026` |
| f | BTC / SEC / ETF / 機関買い | `Bitcoin ETF inflow SEC institutional 2026` |
| g | NFP / CPI / FOMC 等主要指標予想ブレ | `NFP CPI forecast revision today 2026` |
| h | リスクオン / オフセンチメント指標 | `VIX Fear Greed Put-Call ratio today 2026` |

各クエリで最重要 URL を 3〜5 件選び **WebFetch でクロスチェック**。
最終的に「データソース脚注」セクションに URL を列挙する。

### 7.5-5. 自己検証ステップ

レポート生成後、AI 自身が以下を実施し本文に結果を記載する:

1. **スコア再計算**: S12 の信頼度スコアを項目別に再計算し、本文値と一致するか検算
2. **空テーブル / 未入力セル走査**: 「取得不可」と空セルの残存を一覧化
3. **セクション間矛盾検出**: DXY-XAUUSD 逆相関 / DXY-USDJPY 順相関 / Draw on Liquidity 整合 / PO3 整合 / ニュース整合 を機械的にチェック
4. **矛盾検出時の自動修正**: 本文を **1 回まで** 自動再生成。残存矛盾があれば信頼度を Low に下げて完遂

---

---

## 8. トラブルシューティング（ICT Bias 固有）

Routines 共通のトラブルは `~/HQ/infrastructure/runtime-setup.md` § 17 を参照。以下は本リポジトリ固有の症状。

| 症状 | 原因 | 解決策 |
|---|---|---|
| スクレイピング全失敗（SSL エラー） | Anthropic セキュリティプロキシの自己署名証明書 | `ignore_https_errors=True` を全 `new_context()` に追加（§ 5-1） |
| 特定サイトのみスクレイピング失敗 | サイト構造変更（セレクタ崩れ） | Claude Code に `scrapers/<site>.py` の修正を依頼（§ 5-3） |
| Playwright がブロックされる | User-Agent が bot と判定された | User-Agent を実ブラウザ相当に更新 |
| API キーエラー（Twelve Data） | Routine Environment に `TWELVEDATA_API_KEY` 未設定 | § 4-2 を確認し、Daily / Weekly 両方の Routine に登録 |
| API キーエラー（Anthropic） | ローカル実行時の `.env` 未設定 | Routines 側は自動注入のため原因は .env 側。`cp .env.example .env` + 編集 |
| Brain への push が `claude/...` ブランチへ | プロンプトに master 明示がない | Routine prompt に「master 直接 push」を明記（§ 4-4） |
| Slack 投稿の URL が `${CLAUDE_CODE_REMOTE_SESSION_ID}` のまま | 変数展開されず文字列で投稿 | `.claude/commands/*.md` で bash `echo` を経由（§ 6-4） |
| Daily / Weekly で違う動作が欲しいのに同じ結果になる | 1 つの Routine で `text` 引数切替を試みている | **Routine を Daily / Weekly で分ける**（§ 4-1） |

---

## 9. 関連ドキュメント

- `~/HQ/infrastructure/runtime-setup.md` — Routines 構築の共通ガイド（Setup script テンプレート、トラブル表、モデル選択等）
- `~/HQ/infrastructure/slack-operations.md` — Slack × Routines の運用方針 SSoT
- Anthropic 公式: https://code.claude.com/docs/en/routines
- Anthropic 公式: https://code.claude.com/docs/en/claude-code-on-the-web

### GitHub リポジトリ

- `LaaQuantumFund/Trade-Bias-Report` — 本リポジトリ（スクレイパー + プロンプト）
- `LaaQuantumFund/Brain` — レポート出力先（master 直接 push）
