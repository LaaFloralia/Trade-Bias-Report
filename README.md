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

- Python 3.9+
- Claude Code CLI（サブスクログイン済み。LLM 分析はスラッシュコマンドまたは `claude -p` 経由、Anthropic API キーは不要）
- Node.js 18+（Playwright 用）
- Twelve Data API キー（価格取得用）
- **FRED API キー**（Treasury yields + Broad USD Index、無料、https://fred.stlouisfed.org/）— 1Password の `op://Agents/Fred/credential` に保管し、`./scripts/run-with-secrets.sh` 経由で注入する

---

## 2. ローカルセットアップ

### 2-1. 依存パッケージ

```bash
uv sync                          # 依存解決（Python 3.12、pyproject.toml が SSoT）
uv run playwright install chromium
```

uv が `.venv/` を `$PROJECT_DIR/.venv/bin/python3` に作るため、
`.claude/commands/*.md` の `PYTHON_BIN` デフォルトはそのまま動く。
旧 venv（Python 3.9）の最終状態は `docs/old_venv_freeze.txt` に凍結済み。

### 2-2. 環境変数

```bash
cp .env.example .env
# .env を編集して以下を設定:
# - TWELVEDATA_API_KEY
# - OBSIDIAN_VAULT_PATH（Brain へのローカルパス）
# ※ ANTHROPIC_API_KEY は不要（LLM 分析は Claude Code サブスク経由。
#    凍結中の scripts/archive/generate_xauusd_brief.py のみ要求するが未使用）

# API key は 1Password の Agents 保管庫が唯一の正本。ローカルに平文で置かない:
# 1. https://fred.stlouisfed.org/ で API key を取得（無料）
# 2. 1Password の Agents 保管庫に item「Fred」を作り、フィールド credential に値を入れる
#    （Twelve Data も同様に item「TwelveData」/ フィールド credential）
# 3. 参照は .env.tpl に記載済み。ランタイムへは op run が環境変数として注入する:
#      ./scripts/run-with-secrets.sh uv run python main.py
#    macOS Keychain / 平文 .env からの読み取りは廃止した。
```

### 2-3. 実行

```bash
uv run python main.py                # データ取得のみ（COT 含む。常時同一内容）
uv run python main.py --weekly       # 同上（ファイル名 prefix が scraped_data_weekly_ になるのみ）

# 分析込みの一気通貫（ヘッドレス、§ 8 参照）:
uv run python scripts/intel.py brief --daily    # 取得 → LLM 分析 → MD + JSON + PDF(Drive) 出力
uv run python scripts/intel.py brief --weekly
```

---

## 3. ファイル構成

```
fundamental-macro-analysis/  # 旧 ict-daily-bias、社長呼称「チャート外分析」
├── README.md                 # このファイル
├── MODERNIZATION_RESEARCH.md # 2026-05-09 調査: FMP MCP 等の置換計画（FRED のみ採用）
├── AUDIT.md                  # 2026-06-11 現状監査（フロー図 / 負債リスト / UNKNOWN）
├── pyproject.toml            # Python 依存 SSoT（uv 管理、playwright / dotenv / requests / markdown / pyyaml）
├── .env.example              # 環境変数テンプレート
├── config.yaml               # ★ 銘柄定義 SSoT（銘柄・シンボル・URL・ウェイトを一元管理）
├── config.py                 # config.yaml ローダー + 派生テーブル + FOMC 日程（2026/2027）
├── main.py                   # スクレイピングオーケストレーター（データ取得のみ）
├── master_prompt.md          # ★ 統合 Daily（オンデマンド「その瞬間の全体像」、2,400〜3,800 字）
├── master_prompt_weekly.md   # ★ 統合 Weekly（前回以降の振り返り + 来週の展望、4,500〜6,500 字）
├── archive/prompts/          # 旧 Deep 系プロンプト 2 本（2026-08-11 統合により凍結）
├── .claude/
│   └── commands/
│       ├── daily-bias.md     # 統合 Daily（Mac ローカル専用。XAU-TF 自動鮮度確認 → MD+PDF+push）
│       └── weekly-bias.md    # 統合 Weekly（同上 + 前回レポート/intel JSON を前回レビュー入力に）
├── scripts/
│   ├── intel.py              # ★ ヘッドレス分析パイプライン（§ 8。MD + JSON + PDF。INTEL_ENGINE で claude/codex 切替）
│   ├── render_report.py      # MD → PDF レンダラ（HTML は中間生成→削除）
│   ├── publish_report.py     # MD → PDF → Google Drive（マイドライブ/Trading/Bias-Reports）発行
│   └── archive/
│       └── generate_xauusd_brief.py # 凍結（API 直叩き方式。後続フェーズで再実装予定）
├── templates/                # PDF 用 A4 テンプレ + 印刷 CSS
├── docs/                     # 設計入力（FRED 系列候補 / OSS MCP 机上検証）
├── scrapers/
│   ├── twelvedata.py         # Twelve Data API（価格 + PDH/PDL + IPDA 20/40/60）
│   ├── dxy.py                # DXY（Investing.com → MarketWatch → EUR/USD 逆数推定）
│   ├── fred.py               # FRED 5 系列: DGS10/DGS2/DTWEXBGS/DFII10/T10YIE + 系列別 stale 判定
│   ├── myfxbook.py           # リテールセンチメント第 1 ソース
│   ├── fxssi.py / ig_sentiment.py # センチメントフォールバック 1 / 2
│   ├── coinglass.py / binance_btc_sentiment.py / crypto_funding.py # BTC センチメント・Funding 系
│   ├── cot.py                # CFTC COT（Legacy Futures Only、常時取得）
│   ├── economic_calendar.py / fedwatch.py / btc_etf.py # カレンダー / FedWatch / ETF フロー
│   ├── dxy_components.py / vix_structure.py / premarket.py # Deep 強化系
│   ├── macro_liquidity.py / rate_spreads.py / myfxbook_open_orders.py # Deep 強化系
│   ├── gold_etf.py           # 金ETFフロー: GLD 保有トン数（SPDR 公式 API、XAUUSD ファンダ大局用）
│   ├── gold_cb.py            # 中銀ゴールド購入: IMF IRFCL 報告国ベース月次集計（同上）
│   ├── report_anchor.py      # 前回レポート アンカー: Brain の直近 Weekly/Daily 結論を自動差し込み
│   ├── metadata_schema.py    # 共通メタデータ補完（非破壊・冪等）
│   └── validation.py         # 価格バリデーション + FRED 恒等式チェック（DGS10 ≒ DFII10 + T10YIE）
├── tests/                    # mock ベースのテスト群（実 API 疎通なし）
├── logs/                     # intel.py 実行ログ（JSONL、gitignore 対象）
└── output/                   # スクレイプ生データ + intel/（機械用 JSON）。gitignore 対象
```

銘柄の追加・変更は `config.yaml` の編集だけで全スクレイパーに反映される
（直書きの復活は `tests/test_config_ssot.py` が検出する）。

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
| `TWELVEDATA_API_KEY` | `op://Agents/TwelveData/credential`（`.env.tpl` 経由で `op run` が注入）| Twelve Data（価格 API）|
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

- **マスタープロンプト変更**: `master_prompt.md` / `master_prompt_weekly.md` を編集（統合の設計判断は `docs/UNIFIED_DESIGN.md` を先に参照）
- **銘柄追加・変更**: `config.py` の `INSTRUMENTS` を編集
- **スクレイピング対象サイト変更**: 各 `scrapers/*.py` を編集

---

## 7.5. 統合 2 本体制（2026-08-11〜）

2026-08-11 に旧 4 本（速報 Daily / Deep Daily / 速報 Weekly / Deep Weekly）を **2 本に統合**した。
深さ軸（速報 / Deep）は廃止し、時間軸のみ残す。設計判断の全記録は `docs/UNIFIED_DESIGN.md`。
旧 Deep 系プロンプトは `archive/prompts/` に凍結（Brain の `Calendar/Deep-Bias/` / `Weekly-Deep-Bias/` は過去レポート置き場として残存）。

| 項目 | **統合 Daily** | **統合 Weekly** |
|---|---|---|
| 位置づけ | オンデマンドで「その瞬間の全体像」（トレード直前） | 「前回以降の振り返り + 来週の展望」（週末） |
| 実行コマンド | `/daily-bias` | `/weekly-bias` |
| 所要時間 | 約 4〜5 分（XAU-TF 自動更新込み） | 約 8〜12 分 |
| 字数目安 | 2,400〜3,800 字 | 4,500〜6,500 字 |
| WebSearch | 基本 2 + 条件付き最大 4（ツール利用可能環境のみ） | 4〜8 クエリ（マクロ環境・ニュースに集中） |
| 固有セクション | 今夜の執行プラン / ファンダ大局バイアス（アンカー継承） | 前回レビュー（実データ照合）/ COT 分析 / 来週カレンダー |
| 信頼度スコア | **統一 8 項目・閾値共通**（High ≥7 / Med 5-6 / Med-cautious 3-4 / Low ≤2 = 様子見） | 同一 |
| 自己検証 | 軽量 3 チェック（1 パス・注記のみ） | 同一 |
| 出力 | MD（Brain push）+ **PDF（Google Drive: マイドライブ/Trading/Bias-Reports）** | 同一 |
| 実行環境 | Mac ローカル専用（Routines / Slack 経路は撤去） | 同一 |

**オンデマンド運用の自己完結化（前回レポート アンカー）**: Daily 生成時に `scrapers/report_anchor.py` が
Brain/Calendar の最新 Weekly（結論 = セクション0）と前回 Daily（ファンダ大局 + 結論）を自動で
scraped_data に差し込む。許容鮮度（Weekly 9 日 / Daily 7 日 / XAU-TF 1 日）超過は `[STALE]` が付き、
プロンプト側は参考扱いに落とし統一スコア #5 を 0 固定にする。

**XAUUSD レベルの SSoT（XAU-TF アンカー）**: XAUUSD の価格レベルは `xauusd-smc-quant` の
XAU Technical Report（Dukascopy H1・検出定義がコードで検証済み）を正とする。`/daily-bias` /
`/weekly-bias` は Step 0 で XAU-TF レポートが前日以前の場合に自動再生成する（失敗時は STALE 続行）。
乖離時は「乖離: TD ○○ / XAU-TF ○○（採用）」形式でレポートに明記。

**推論エンジン**: 既定は Claude サブスク枠（`claude -p`）。`INTEL_ENGINE=codex` で Codex CLI
（`codex exec`）に切替可能（実験的。CLI 呼び出し規約は検証済み、生成品質は未検証）。

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

---

## 8. ヘッドレスパイプライン（scripts/intel.py）

スラッシュコマンド（対話セッション）を介さずに、データ取得 → LLM 分析 → 保存を
一気通貫で実行する経路。cron / launchd / 外部システム（trading-bot 等）からの
呼び出しを想定する。LLM 分析は `claude -p`（Claude Code CLI ヘッドレスモード）で
実行し、Anthropic API 直叩きは行わない（サブスク運用方針）。

### 8-1. 実行

```bash
uv run python scripts/intel.py brief --daily            # 日次（master_prompt.md 使用）
uv run python scripts/intel.py brief --weekly           # 週次（master_prompt_weekly.md 使用）
uv run python scripts/intel.py brief --daily --reuse-data  # 当日データがあれば再取得を省略
uv run python scripts/intel.py brief --daily --quick    # 新規取得をスキップし直近データで分析のみ再実行
```

前提: `claude` CLI がログイン済み（サブスク認証）であること。API キーは不要。
`INTEL_ENGINE=codex` で Codex CLI（`codex exec`）に切替可能（実験的）。

### 8-2. 出力（三重）

| 出力 | パス | 用途 |
|---|---|---|
| 人間用 Markdown | `$BRAIN_PATH/Calendar/{Daily-Bias\|Weekly-Bias}/{Daily\|Weekly}_Bias_Report_YYYY-MM-DD.md` | 既存スラッシュコマンドと同じ保存先・形式 |
| 機械用 JSON | `output/intel/intel_{daily\|weekly}_YYYY-MM-DD.json` | trading-bot / EA 等の機械判断入力 |
| PDF | `output/*.pdf` + Google Drive `マイドライブ/Trading/Bias-Reports/` | スマホ閲覧用（publish_report.py。失敗しても run は成功のまま） |

機械用 JSON スキーマ:

```json
{
  "bias": -1.0,                       // XAUUSD 日次バイアス（-1.0 強Bearish 〜 +1.0 強Bullish）
  "no_trade": false,                  // 様子見 / プラン非提示 / 信頼度不足なら true
  "no_trade_reason": null,            // no_trade=true の理由（false なら null）
  "risk_events_next_24h": ["21:30 JST 米 CPI"],  // 24h 以内の高重要度イベント
  "positioning_summary": "...",       // リテール / 機関ポジショニング要約
  "confidence": 0.7                   // レポート信頼度の 0.0〜1.0 正規化
}
```

JSON のパース / スキーマ検証に失敗した場合は**リトライ 1 回**（違反内容をフィードバック）、
それでも失敗したら **`no_trade: true` の安全側 JSON にフォールバック**して exit 0 で完了する
（トレードを止める方向にしか倒れない設計）。

### 8-3. 実行ログ

全実行の入出力（プロンプト全文・応答全文・所要時間・出力パス・フォールバック有無）を
`logs/intel_runs.jsonl` に 1 行 1 実行で追記する。gitignore 対象。

## シークレット管理（1Password）

シークレットの値はリポジトリに置かない。1Password の `Agents` 保管庫に登録し、
`.env.tpl` の `op://` 参照経由で実行時にのみ注入する。

```bash
# 初回セットアップ（pre-commit の gitleaks フックを有効化）
git config core.hooksPath scripts/hooks

# 実行（op run でシークレットを注入）
./scripts/run-with-secrets.sh <command>

# launchd / cron からの非対話実行（サービスアカウントトークンを使う）
./scripts/run-with-secrets.sh --batch <command>
```

前提: `op`（1Password CLI）と `gitleaks` がインストール済みであること。
`--batch` は `~/.config/laa/op-service-token` を読む。
