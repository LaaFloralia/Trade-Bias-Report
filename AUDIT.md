# AUDIT.md — fundamental-macro-analysis 現状監査

> **注（2026-08-11）**: 本監査は 2026-06-11 時点のスナップショットである。同日以降に
> 「4 本→2 本統合」（Deep 系プロンプト・コマンドの廃止、COT 常時取得化、PDF/Google Drive 発行、
> X-Search 停止）が実施されたため、Deep 系（master_prompt_deep*.md / deep-bias*.md）と
> --weekly の COT 差分に関する記述は旧構成のもの。現行の設計は `docs/UNIFIED_DESIGN.md` を参照。

監査日: 2026-06-11
対象: `/Users/laa/dev/fundamental-macro-analysis`（GitHub: `LaaQuantumFund/Trade-Bias-Report`）
方針: コードから確認できた事実のみを記載。変更・削除・リネームは一切実施していない。
判断できなかった点は § 7 UNKNOWN に列挙。

---

## 1. 全体フロー図

### エントリーポイント（5 経路）

| # | 起動方法 | 定義ファイル | 動作 |
|---|---|---|---|
| A | `/daily-bias`（Claude Code セッション内） | `.claude/commands/daily-bias.md` | `main.py` 実行 → `master_prompt.md` で分析 → Brain 保存。Mac / Routines 両対応 |
| B | `/weekly-bias` または `/daily-bias weekly` | `.claude/commands/weekly-bias.md` | `main.py --weekly`（COT 込み）→ `master_prompt_weekly.md` で分析 |
| C | `/deep-bias [pdf]` | `.claude/commands/deep-bias.md` | `main.py --weekly` → WebSearch 8〜12 + 自己検証 → `master_prompt_deep.md`。Mac ローカル専用 |
| D | `/deep-bias-weekly [pdf]` | `.claude/commands/deep-bias-weekly.md` | C の週次版（先週レビュー + 今週展望）。`master_prompt_deep_weekly.md` |
| E | `python main.py [--weekly]` 直接実行 | `main.py` | データ取得のみ（LLM 分析なし）。`output/scraped_data_*.{json,txt}` を生成して終了 |

LLM 分析は **Claude Code セッション内**（スラッシュコマンドを実行している Claude 自身）が行う。
Anthropic API の直叩きはメインフローには存在しない（コミット `99df123` で廃止済み。例外は § 6 参照）。

### 処理フロー

```
/daily-bias 等のスラッシュコマンド
  │
  ├─ Step 1: 環境判定（CLAUDE_CODE_REMOTE=true → Routines / それ以外 → Mac ローカル）
  │           Routines は find でリポジトリ clone 先を動的検出 + Playwright chromium 確認
  │
  ▼
main.py  collect_all_data()                       ※データ取得のみを担当
  │
  ├─ Twelve Data: /quote + /time_series 各 1 回（XAUUSD/USDJPY/BTCUSD 一括, 計 2 calls）
  ├─ Binance Futures: BTC Long/Short 3 種（Top Trader Position / Account / Global）
  ├─ Phase 1（並列）: MyFXBook sentiment（XAUUSD/USDJPY）+ CoinGlass（BTC）
  ├─ Phase 2（失敗銘柄のみ）: FXSSI → IG の順でフォールバック
  ├─ --weekly 時のみ: COT（CFTC Socrata API, Legacy Futures Only）
  ├─ Phase A（並列 8 系）: DXY / FRED(DGS10,DGS2,DTWEXBGS) / 経済指標カレンダー /
  │                        BTC ETF フロー / FedWatch / マクロ流動性 / 金利スプレッド / Crypto Funding
  ├─ Phase B（直列）: dxy_components → sleep 7 秒 → premarket → vix_structure
  │                   （Twelve Data 無料枠 8 calls/min の 429 回避 + Playwright 競合回避）
  └─ MyFXBook Open Orders（XAUUSD/USDJPY 並列）
  │
  ▼
normalize_scraper_results()（metadata_schema.py: 共通メタデータ補完, 非破壊）
format_scraped_data()（validation.py のバリデーション実行 → 異常値を「データ異常」表記に置換）
save_scraped()
  │
  ▼
output/scraped_data_YYYY-MM-DD.json（生データ, raw_text 除去済み）
output/scraped_data_YYYY-MM-DD.txt （LLM 向け整形テキスト）
  │
  ▼
Claude（セッション内）が Read:
  master_prompt*.md + scraped_data_*.txt
  Deep 系のみ追加: WebSearch 8〜12 クエリ（固定群 a〜h）+ WebFetch 3〜5 件クロスチェック
  │
  ▼
Markdown レポート生成（構造化: セクション固定 + テーブル + 信頼度スコアリング）
  Deep 系のみ: 自己検証（スコア再計算 / 空セル走査 / セクション間矛盾検出 → 1 回まで自動修正）
  │
  ├─ Brain 保存: ~/Brain/Calendar/{Daily-Bias|Weekly-Bias|Deep-Bias|Weekly-Deep-Bias}/
  ├─ Routines 環境のみ: Brain を master ブランチへ直接 commit + push、Slack #ceo へ通知
  └─ Deep + pdf 引数時のみ: scripts/render_report.py → HTML（中間生成→削除）→ PDF（output/ のみ）
```

---

## 2. ディレクトリ・ファイル構成

※指示文のセクション 2〜4 の見出しが欠落していたため、残存断片（エントリーポイント / 判断ロジック / 出力形式）から「2. 構成」「3. データソース一覧」「4. 分析ロジック」と再構成して記載。

```
fundamental-macro-analysis/        # 旧 ict-daily-bias（2026-05-09 リネーム、社長呼称「チャート外分析」）
├── main.py                  (830 行)  スクレイピングオーケストレーター（データ取得のみ）
├── config.py                ( 58 行)  API キー env 読込 / INSTRUMENTS / FOMC 日程 / Playwright 設定
├── requirements.txt                   playwright, python-dotenv, requests, markdown, anthropic
├── .env                               TWELVEDATA_API_KEY 平文（gitignore 済・git 履歴混入なし）
├── .env.example                       環境変数テンプレート（FRED は Keychain 推奨と明記）
├── master_prompt.md         (304 行)  速報 Daily 用プロンプト（セクション 0〜7）
├── master_prompt_weekly.md  (393 行)  速報 Weekly 用（セクション 0〜8、COT 含む）
├── master_prompt_deep.md    (603 行)  Deep Daily 用（S0〜S14 + データソース脚注、11 項目スコア）
├── master_prompt_deep_weekly.md (474 行) Deep Weekly 用（W0〜W15、先週レビュー W1 含む）
├── README.md                (348 行)  運用ガイド（※現状と一部乖離、§ 6 参照）
├── MODERNIZATION_RESEARCH.md(427 行)  2026-05-09 調査。FMP MCP 等は見送り、FRED のみ採用の経緯
├── AUDIT.md                           本ファイル（2026-06-11 監査で新規追加）
├── .claude/
│   ├── settings.json                  Bash 許可リスト（python 実行系のみ）
│   └── commands/
│       ├── daily-bias.md    (234 行)  速報 Daily（Mac/Routines 両対応）
│       ├── weekly-bias.md   (160 行)  速報 Weekly（daily-bias の週次バリアント）
│       ├── deep-bias.md     (261 行)  Deep Daily（ローカル専用、9 Step）
│       └── deep-bias-weekly.md (277 行) Deep Weekly（ローカル専用、git stash 保護付き push）
├── scrapers/                (21 ファイル、詳細は § 3)
├── scripts/
│   ├── render_report.py     (256 行)  MD → HTML（中間）→ PDF（Playwright Chromium、A4）
│   └── generate_xauusd_brief.py (429 行)  Daily 本体から XAUUSD 簡易版を再構成
│                                      ※Anthropic API 直叩き・どこからも呼ばれていない（§ 6-3）
├── templates/
│   ├── report.html          ( 23 行)  PDF 用テンプレ（{{TITLE}}/{{SUMMARY_HTML}}/{{CONTENT}} 置換）
│   └── style.css            (449 行)  印刷用 CSS（信頼度バッジ cover-confidence-* クラス含む）
├── tests/                   (5 ファイル + fixtures、計 59 テスト関数)
│   ├── test_deep_bias.py              render_report の単体テスト
│   ├── test_new_scrapers.py           Deep 強化スクレイパー（mock ベース）
│   ├── test_metadata_schema.py        共通メタデータ補完
│   ├── test_dxy_fallback.py           DXY 失敗時 DTWEXBGS proxy 表示
│   └── test_xauusd_brief.py           XAUUSD Brief（mock client 注入）
├── docs/
│   ├── FRED_SERIES_CANDIDATES.md      追加系列候補（DFII10/T10YIE 等、未実装・机上比較）
│   └── OSS_MCP_DESK_VALIDATION.md     FMP Free / CoinGlass MCP / ETF Flow MCP 机上検証（未導入）
└── output/                            gitignore 対象。scraped_data 2 日分 + 旧 Weekly レポート MD/PDF
```

git 状態（2026-06-11 時点）:
- 最終コミット: `cc9bb13`（2026-05-14）「Deep Bias Report 強化版: 新規スクレイパー追加とデータ取得経路の改善」
- **未コミット変更が約 4 週間分残存**: modified 8 件（main.py, scrapers/dxy.py, scrapers/premarket.py, scripts/render_report.py, README.md, requirements.txt, deep-bias.md, deep-bias-weekly.md）+ untracked 6 件（docs/, scrapers/binance_btc_sentiment.py, scrapers/metadata_schema.py, scripts/generate_xauusd_brief.py, tests/test_metadata_schema.py, tests/test_xauusd_brief.py）

---

## 3. データソース／スクレイパー一覧

| モジュール | 取得対象 | ソースと優先順 | 方式 | 備考 |
|---|---|---|---|---|
| `twelvedata.py` | XAUUSD/USDJPY/BTCUSD の現在値・PDH/PDL・PWH/PWL・PMH/PML・IPDA 20/40/60 日 | Twelve Data API | REST（要 API キー） | /quote + /time_series 各 1 call。429 時 5 秒待ち 1 回リトライ |
| `dxy.py` | DXY 現在値 + HTF レベル | ① Investing.com（内部 API → HTML）② MarketWatch ③ EUR/USD 逆数推定（Twelve Data） | Playwright + REST | ヒストリカルは Investing.com 内部 API（instrument_id=942611）→ Stooq フォールバック |
| `fred.py` | DGS10（US10Y）/ DGS2（US2Y）/ DTWEXBGS（Broad USD Index ≠ DXY） | FRED API | REST（要 API キー） | キー解決順: env → macOS Keychain → .env。失敗時は過去 7 日の output JSON から stale 復元 |
| `myfxbook.py` | リテール Long/Short % + 平均エントリー価格（XAUUSD/USDJPY） | MyFXBook outlook ページ | Playwright + 正規表現 | リテールセンチメントの第 1 ソース |
| `fxssi.py` | Buy/Sell %（XAUUSD/USDJPY/EURUSD/GBPUSD） | FXSSI Current Ratio | Playwright + 正規表現 | MyFXBook 失敗時のフォールバック 1 |
| `ig_sentiment.py` | Long/Short %（XAUUSD/USDJPY のみ URL あり） | IG | Playwright + 正規表現 | フォールバック 2。long+short=100 補完あり |
| `coinglass.py` | BTC L/S Ratio + OI 加重 Funding Rate | CoinGlass（2 ページ） | Playwright + 正規表現 | BTC センチメントの第 1 ソース |
| `binance_btc_sentiment.py` | BTC Top Trader（Position/Account）+ Global L/S | Binance Futures 公開 API | REST（認証不要） | MyFXBook BTC 非対応の代替（2026-05-16 追加）。Global−TopTrader 乖離も main.py 側で算出 |
| `cot.py` | COT 4 銘柄（GC/6J/DX/BTC）Large Spec・Commercials・Small Spec・OI | CFTC Socrata API（Legacy Futures Only） | REST（認証不要） | --weekly 時のみ。最新 2 週分で前週比算出 |
| `economic_calendar.py` | ★★★ 経済指標（今週 + 来週）: 日付/JST 時刻/国/指標/前回/予想 | Investing.com | Playwright（timezone_id=Asia/Tokyo） | 米国主要指標の JST 時刻サニティチェック内蔵（06-08 時帯は変換エラー疑い） |
| `fedwatch.py` | 次回 FOMC・据置/利下げ/利上げ確率 | ① Investing.com Fed Rate Monitor（stealth）② CME 公式（ほぼ Cloudflare timeout） | Playwright | 常時取得（旧 FOMC 週限定を撤廃）。CME paid API は非採用 |
| `btc_etf.py` | BTC ETF 日次フロー（IBIT/FBTC/GBTC + 合計、直近 5 営業日） | ① Farside（Playwright stealth）② Farside（requests、ほぼ 403）③ SoSoValue ④ CoinGlass | Playwright + 正規表現 | |
| `dxy_components.py` | DXY 構成 6 通貨の前日比 → ウェイト按分寄与・主要ドライバー | Twelve Data /quote バッチ | REST | ICE 公式ウェイト直書き（EUR 57.6% 等）。Deep 用 |
| `vix_structure.py` | VIX9D/VIX/VIX3M/VIX6M + ターム構造 + レジーム | ① FRED（VIXCLS/VXVCLS）② CBOE Dashboard ③ Twelve Data ④ Yahoo Finance | REST + Playwright 直列 | 妥当域 5〜100 でフィルタ。Deep 用 |
| `premarket.py` | SPX/NDX/DJI 現在値・前日比・OHL + risk regime | ① Twelve Data ② Yahoo Chart API ③ Yahoo HTML | REST + Playwright | OHL 欠損時は Chart API で補完。Deep 用 |
| `macro_liquidity.py` | WALCL − RRPONTSYD − WTREGEN ≒ Net Liquidity + regime | FRED | REST | 単位正規化（M→B USD）。Deep 用 |
| `rate_spreads.py` | US10Y − DE/JP/UK/CA 10Y スプレッド + 前回比 | FRED（DGS10 + OECD 月次系列） | REST | 海外系列は月次（stale 注意）。Deep 用 |
| `crypto_funding.py` | BTCUSDT Funding Rate 3 取引所平均 + 乖離 + regime | Binance / Bybit / OKX 公開 API | REST（認証不要） | Deep 用。CoinGlass 不通時の独立ソース |
| `myfxbook_open_orders.py` | Order Book の Bid/Ask 集中帯 → BSL/SSL 候補クラスタ top-3（XAUUSD/USDJPY） | MyFXBook outlook ページ | Playwright + テキストパース | 前半/後半二分割で Bids/Asks を推定、0.5% 幅でクラスタリング。Deep 用 |
| `validation.py` | （スクレイパーではない）価格データ異常検出 | — | — | § 4-3 参照 |
| `metadata_schema.py` | （スクレイパーではない）共通メタデータ補完 | — | — | source/symbol/timestamp/as_of_date/stale/fallback_used/error/note を非破壊補完 |

リテールセンチメントのフォールバック連鎖: **MyFXBook → FXSSI → IG**（main.py:139-211 の Phase 1/2）。
全スクレイパー共通: Playwright `new_context()` に `ignore_https_errors=True`（Routines のセキュリティプロキシ対応、README § 5-1）。

---

## 4. 分析ロジック

### 4-1. 分析の実行主体とプロンプト

LLM 分析は Claude Code セッション（スラッシュコマンド実行中の Claude）が行う。モデル ID のコード直書きはメインフローにない（Routines 側のモデルは README:129 で「Sonnet 4.6」指定、例外は § 6-3 の `generate_xauusd_brief.py`）。

| プロンプト | 用途 | セクション構成 | 字数目安 |
|---|---|---|---|
| `master_prompt.md` | 速報 Daily | 0 サマリー / 1 DXY / 2 銘柄別 / 3 カレンダー / 4 FedWatch・中銀 / 5 相関 / 6 Intraday PO3 / 7 統合（Liquidity Map + スコアリング） | 2000〜3500 字（Routines は 1500〜2000 字） |
| `master_prompt_weekly.md` | 速報 Weekly | 0〜8（COT 分析 / 季節性・Quarterly Shift / IPDA / トレードプラン Top 2 / PO3 マルチ TF が追加） | 3000〜5000 字 |
| `master_prompt_deep.md` | Deep Daily | S0〜S14 + データソース脚注。S2-X（DXY 分解 / Funding 横断 / Open Orders 実数）、S5-X ボラ環境、S6-X マクロ流動性 + スプレッド、S14 自己検証 | 5000〜8000 字 |
| `master_prompt_deep_weekly.md` | Deep Weekly | W0〜W15。W1 先週レビュー（Bias 結果 vs 実際）、W0 は 11〜12 行厳守 | 6000〜10000 字 |

### 4-2. 判断ロジック（コード側に実装された閾値）

| 判定 | ロジック | 実装箇所 |
|---|---|---|
| FOMC 週 | 次回 FOMC 開催日を含む週の月〜金なら `is_fomc_week=true` | main.py:43-85（日程は config.py:41-50 の 2026 年 8 回直書き） |
| Binance 乖離 | Global − TopTrader Long% が +5pp 超 → 「リテール強気・プロ弱気」、−5pp 未満 → 反転シグナル候補 | main.py:516-530 |
| VIX ターム構造 | VIX > VIX3M×1.02 → backwardation（リスクオフ）/ VIX < ×0.98 → contango / 間 → flat。VIX9D > VIX×1.05 → 短期イベント警戒。レベル区分 <15 calm / <20 normal / <30 elevated / ≥30 panic | vix_structure.py:168-202 |
| リスクレジーム | SPX/NDX/DJI 前日比が全銘柄 +0.1% 超 → risk-on / 全銘柄 −0.1% 未満 → risk-off / 混在 → mixed | premarket.py:309-321 |
| 流動性レジーム | Net Liquidity 前回比 +20B 超 → expansion / −20B 未満 → contraction | macro_liquidity.py:95-114 |
| Funding レジーム | 3 取引所平均 > +0.01% → long crowded / < −0.005% → short crowded | crypto_funding.py:153-160 |
| BSL/SSL クラスタ | 現在価格の上=BSL 候補 / 下=SSL 候補。価格差 0.5% 以内を同一クラスタ化、volume 上位 3 件 | myfxbook_open_orders.py:124-172 |
| FedWatch 互換値 | 最大確率レンジを hold_pct とみなし、上下レンジを bp 差で cut/hike に振り分け（近似） | fedwatch.py:118-145 |

### 4-3. データバリデーション（validation.py）

- B-1 最小レンジ: PDH/PDL 等の差が閾値未満 → 異常（XAUUSD 5.0 / USDJPY 0.05 / BTCUSD 200 / DXY 0.1）
- B-2 前日比異常: XAUUSD ±10% / USDJPY ±3% / BTCUSD ±15% / DXY ±3% 超 → 異常
- B-3 ゼロ・NULL・負数 / B-4 High < Low 逆転
- 異常検出時は整形テキスト内の該当行を「データ異常: [理由]」に置換（apply_validation）
- **注意**: Twelve Data 3 銘柄分のバリデーションは `_raw_quote_*` キー前提だが main.py がそのキーを保存しないため**実質 DXY のみ機能**（§ 6-2）

### 4-4. 弱気/強気の判断基準（プロンプト側）

- リテール 60% 以上の偏り → 反対側に Liquidity Pool（BSL/SSL）が存在すると判断（逆張りスコア +2、55〜60% は +1、バイアスと同方向 55% 以上は −1）
- DXY バイアス整合 +2（推定値なら +1）、XAUUSD は DXY 逆相関 / USDJPY は順相関 / BTC は ETF フロー・NQ 相関優先
- ETF フロー: 純流入=機関買い圧力、3 日以上連続でトレンド注記。取得不可は −1
- COT: Large Spec / Commercials がバイアス支持 +1、極端な逆偏り −1（Weekly/Deep のみ）
- 信頼度合計の判定: 速報 Daily「7+ 高確度 / 5-6 標準 / 4 慎重 / 3 以下 様子見（プラン非提示）」、速報 Weekly「8+ High / 6-7 Med / 5 Med-cautious / 4 以下 Low」、Deep（11 項目に ニュース整合・地政学・季節性 を追加）「10+ High / 7-9 Med / 5-6 Med-cautious / 4 以下 Low」
- スコアリング制約: 定義済み項目以外の追加加点を禁止（プロンプト内に明記）

### 4-5. 出力形式

自由文ではなく**構造化 Markdown**。セクション順序・テーブル列・スコアリング表をプロンプトで固定。絵文字禁止・時刻は全 JST。Deep 系は S0/W0 の 1 行目に「信頼度バッジ: High/Med/Med-cautious/Low」を必須化し、`render_report.py:32-77` が正規表現で抽出して PDF 表紙バッジ化する。

---

## 5. 出力とストレージ

| 種別 | パス | 形式 | 残存状況（2026-06-11 確認） |
|---|---|---|---|
| スクレイプ生データ | `output/scraped_data_YYYY-MM-DD.json` | JSON（raw_text 除去済み） | 2026-05-16 / 05-20 の 2 日分 |
| LLM 向け整形テキスト | `output/scraped_data_YYYY-MM-DD.txt` | プレーンテキスト | 同上 2 日分 |
| 速報 Daily | `~/Brain/Calendar/Daily-Bias/Daily_Bias_Report_*.md` | Markdown | 2 件（2026-04-19, 2026-05-20） |
| 速報 Weekly | `~/Brain/Calendar/Weekly-Bias/Weekly_Bias_Report_*.md` | Markdown | 3 件（うち 1 件は旧名 `ICT Weekly Bias Report.md`） |
| Deep Daily | `~/Brain/Calendar/Deep-Bias/Deep_Bias_Report_*.md` | Markdown | 2 件（2026-05-13, 05-14） |
| Deep Weekly | `~/Brain/Calendar/Weekly-Deep-Bias/Weekly_Deep_Bias_Report_*.md` | Markdown | 1 件（2026-05-14） |
| PDF（Deep + pdf 引数時のみ） | `output/Deep_Bias_Report_*.pdf` 等 | PDF A4（5〜20 ページ目安） | 旧 Weekly の PDF 1 件（2026-05-16）。Brain には置かない |
| XAUUSD Brief | `output/Daily_XAUUSD_Brief_*.md`（設計上） | Markdown | **0 件（未統合のため生成実績なし）** |

- `output/` は gitignore 対象（ローカルのみ）。README:109 の「Brain にも commit」はレポート MD のみを指す。
- Routines 経路では Brain リポジトリの **master ブランチへ直接 commit + push**（`claude/...` ブランチ禁止）+ Slack `#ceo` へ通知（セッション URL 実値埋め込み）。ローカル経路では deep 系コマンドのみ git push を実施、速報系は Write 保存のみ。
- **ログファイルは存在しない**。実行ログは stdout の print のみで、永続化はされない（過去実行の成否は output ファイルの有無からしか推定できない）。
- FRED の stale フォールバックが `output/scraped_data_*.json` の直近 7 ファイルをキャッシュとして再利用する（fred.py:124-142）— output/ はただの成果物置き場ではなく**準キャッシュ層**を兼ねる。

---

## 6. ハードコード・技術的負債リスト

### 6-1. APIキー・シークレット

| 箇所 | 内容 | 危険度 |
|---|---|---|
| `.env:1` | `TWELVEDATA_API_KEY` の実キーが平文で存在 | **中**。`.gitignore` 済みで git 履歴への混入なし（`git log -S` で確認済み）。コミットはされていないが、ローカル平文保管は社長の Keychain 一元化方針（FRED は Keychain 化済み）と非対称 |
| `.gitignore:1,6` | `.env` が重複記載 | 低（実害なし） |
| `scripts/generate_xauusd_brief.py:336` | `ANTHROPIC_API_KEY` を env から要求 | 中（§ 6-3 参照） |

**平文コミットされた API キーは存在しない**（警告対象なし）。

### 6-2. 機能していない/呼ばれていないコード

| 箇所 | 内容 |
|---|---|
| `validation.py:219-229` | Twelve Data 3 銘柄のバリデーションは `scraped_data["_raw_quote_XAUUSD"]` 等のキー前提だが、main.py はこのキーを一切セットしない（コメントにも「main.py側でquote/seriesを保持する必要あり」と残置）。**価格バリデーションは実質 DXY しか動いていない** |
| `fred.py:49-53, 250` | age ベースの stale 判定は `SERIES_CONFIG`（DGS10/DGS2/DTWEXBGS のみ）に登録された系列しか動かない。`rate_spreads.py`（OECD 月次系列）・`macro_liquidity.py`（WALCL 等）・`vix_structure.py`（VIXCLS 等）が取得する系列は**鮮度超過しても stale=false のまま**。rate_spreads.py:16 の「stale 上限を 35 日まで許容」はコードに実装されていない |
| `scripts/generate_xauusd_brief.py` 全体 | **どのスラッシュコマンド・main.py からも呼ばれていない**（呼び出し元はテストと CLI のみ）。生成実績ファイルも 0 件 |
| `config.py:8-13` | `OBSIDIAN_DAILY_PATH` / `OBSIDIAN_WEEKLY_PATH` はどこからも import されていない（コマンド側は `BRAIN_PATH` を使用）。死に設定 |
| `fxssi.py:85` | `asyncio.run(scrape_fxssi()).items().__iter__` — `__main__` ブロック内の無意味な残骸行（実行すると 2 回スクレイプする） |
| `myfxbook_open_orders.py:272-304` | `_summarize_buckets` は旧スキーマ互換でテスト専用と明記（残置） |
| `btc_etf.py:28-47` | `_scrape_farside`（requests 版）は「殆ど 403」と明記しつつ後方互換で残置 |

### 6-3. 方針との不整合

| 箇所 | 内容 |
|---|---|
| `scripts/generate_xauusd_brief.py:63, 227-231` | モデル ID `claude-sonnet-4-6` を直書きし、`anthropic.Anthropic()` で **API を直叩き**する設計。コミット `99df123`「Anthropic API 直叩きを廃止し、Claude Code スラッシュコマンド経由に変更」の方針、および「Claude Code はサブスク版のみ・API キー設定禁止」の運用方針と逆行する。未統合のまま放置されている |
| `master_prompt_deep.md:283` | 「CFTC **Disaggregated** Report の最新を使用」とあるが、`cot.py:3-12` の実装は **Legacy Futures Only** API（`6dca-aqww`）。プロンプトと実装でレポート種別が食い違う |
| `README.md:74-110` | ファイル構成リストが現状と乖離: Deep 強化スクレイパー 8 本（dxy_components / vix_structure / premarket / macro_liquidity / rate_spreads / crypto_funding / myfxbook_open_orders / metadata_schema）、`deep-bias-weekly.md`、`master_prompt_deep_weekly.md`、`scripts/generate_xauusd_brief.py`、`docs/` が未記載 |
| git 作業ツリー | 2026-05-14 の最終コミット以降、modified 8 + untracked 6 が**約 4 週間未コミット**。「検証通過後に commit → push まで自動実行」の dev 運用ルールから逸脱した状態 |

### 6-4. 銘柄・閾値・日程・URL の直書き（主要箇所）

| 分類 | 箇所 | 値 |
|---|---|---|
| 銘柄定義 | `config.py:16-38` | INSTRUMENTS（DXY/XAUUSD/USDJPY/BTCUSD + slug） |
| 銘柄（重複定義） | `twelvedata.py:24-28` / `dxy_components.py:33-40` / `premarket.py:31-49` / `vix_structure.py:39-66` / `cot.py:15-20` / `btc_etf.py:25` / `fxssi.py:43` / `ig_sentiment.py:14-17` / `main.py:301` | 各モジュールが独自にシンボル表を持つ。config.INSTRUMENTS と連動しない（銘柄追加時に最大 9 ファイル修正が必要） |
| FOMC 日程 | `config.py:41-50` | 2026 年 8 回のみ。**2026-12-15 以降は「未定（2026年日程終了）」になり FOMC 週判定が事実上無効化**（main.py:66-71） |
| DXY 推定係数 | `dxy.py:27` | `EURUSD_DXY_FACTOR = 50.14348112` |
| Investing.com 内部 ID | `dxy.py:130` | `instrument_id = "942611"`（DXY ヒストリカル API） |
| DXY 構成ウェイト | `dxy_components.py:33-40` | EUR 57.6% / JPY 13.6% / GBP 11.9% / CAD 9.1% / SEK 4.2% / CHF 3.6% |
| バリデーション閾値 | `validation.py:17-30` | 最小レンジ / 前日比閾値（master_prompt.md:110-115, 300-304 にも同値が重複記載 — 二重管理） |
| レート制限対策 | `main.py:280`（sleep 7 秒）/ `twelvedata.py:50`（5 秒リトライ）/ `dxy_components.py:54,61`（8 秒）/ `premarket.py:77,136`（2 秒×n, 8 秒） | Twelve Data 無料枠 8 calls/min・Yahoo 429 前提の数値 |
| 判定閾値 | `vix_structure.py:176-196`（1.02/0.98/1.05/15/20/30）/ `premarket.py:312-313`（±0.1%）/ `macro_liquidity.py:107-110`（±20B）/ `crypto_funding.py:155-157`（+0.01%/−0.005%）/ `main.py:526-529`（±5pp）/ `myfxbook_open_orders.py:128`（cluster 0.5%） | § 4-2 の判断ロジック本体 |
| 固定パス | `daily-bias.md:24,69` / `weekly-bias.md:56` / `deep-bias.md:22,48` / `deep-bias-weekly.md:23,47` | `/Users/laa/dev/fundamental-macro-analysis`（PROJECT_DIR デフォルト）、`$HOME/Brain` |
| 対象 URL | 各スクレイパー冒頭 docstring + コード内（myfxbook.py:39 / fxssi.py:28 / coinglass.py:31,61 / fedwatch.py:47,196 / btc_etf.py:34,134,170,230 / economic_calendar.py:182 / dxy.py:129,242,314,393 / cot.py:12 / fred.py:43 / crypto_funding.py:36-86 / binance_btc_sentiment.py:21 / vix_structure.py:52-66 / premarket.py:38-49,64） | 全 URL 直書き（スクレイパーの性質上不可避だが、一覧管理はされていない） |
| User-Agent | `config.py:54-58` | Chrome/131 固定（陳腐化するとブロックリスク。btc_etf.py:147 の Sec-Ch-Ua も同様） |
| モデル指定 | `scripts/generate_xauusd_brief.py:63` | `claude-sonnet-4-6`（env で上書き可）。README.md:129 は Routines モデルとして「Sonnet 4.6」を記載 |
| Slack チャンネル | `.env.example:31` / `daily-bias.md:27` | `#ceo` デフォルト |
| Killzone 定義 | `master_prompt.md:126` 等 | London 16:00-19:00 JST / NY 21:00-01:00 JST（プロンプト直書き） |
| 週/月の換算 | `twelvedata.py:155-156` | PWH/PWL=直近 5 本、PMH/PML=直近 22 本の近似（dxy.py:73-99 はカレンダー基準で算出しており、**同名レベルの定義が銘柄間で不一致**） |

### 6-5. その他の負債

- `main.py:229` と `main.py:576` で `_get_fomc_metadata()` を 2 回呼ぶ（日跨ぎ実行時に取得時と整形時で判定がズレ得る。軽微）
- `.pytest_cache/v/cache/lastfailed` に `test_deep_bias.py::test_render_produces_pdf` の失敗記録が残存（最終ローカル実行時に PDF レンダーテストが失敗していた）
- `economic_calendar.py:85` の★判定が `html.count("opacity-60") >= 5` という CSS クラス名依存（Investing.com の class 変更で全滅する系の脆さ。正規表現スクレイパー全般に同種のリスク）
- `master_prompt_deep_weekly.md` W1-3「先週の Bias 結果 vs 実際」を埋めるには先週レポートの参照が必要だが、`deep-bias-weekly.md` Step 2 の Read 対象はプロンプトと当日 scraped_data のみ（先週レポートを読む手順が未定義 → 実行時の Claude の裁量任せ）
- テスト 59 件はすべてパーサ・計算ロジックの mock テスト。実サイトへの疎通を検証する統合テストは存在しない

---

## 7. UNKNOWN（コードからは判断できなかった点）

1. **各スクレイパーが現時点で動くか**: 最終実行は 2026-05-20（output の日付）。以降 3 週間、対象サイトの DOM 変更・ブロック有無は実行しないと不明。
2. **Routines（クラウド）側の Routine 2 本（ict-daily-bias / ict-weekly-bias）が現在も登録・有効か**: claude.ai/code/routines 側の状態はリポジトリから確認不可。
3. **FRED_API_KEY が Keychain に実際に登録されているか**: コードは解決順のみ定義。`security find-generic-password` を実行しないと不明（監査では実行していない）。
4. **TWELVEDATA_API_KEY（.env の値）が現在も有効か**、無料枠か有料枠か。
5. **Slack MCP / `#ceo` チャンネルの接続状態**（Routines 経路でのみ使用）。
6. **`.venv` の Python バージョンと実際にインストール済みの依存**（requirements.txt との一致は未検証）。
7. **`~/Brain/Calendar/Daily-Bias/Daily_Bias_Report_2026-05-20.md` の mtime が 2026-06-11 22:24 である理由**（本日何かが触った形跡。Obsidian sync / 手動編集 / 別セッションのいずれかは特定不可）。
8. **未コミット変更（8 modified）の差分意図**: 動作中の改善か、検証途中で放置されたものか。`git diff` の精査は本監査のスコープ外（変更禁止のため stash 等も不可）。
9. **速報 Weekly の運用実態**: Brain の Weekly-Bias は 3 件のみ（最新 2026-05-16）。週次運用が継続中か停止中かは記録がなく不明。
10. **PDF レンダーの現在の成否**: lastfailed に PDF テスト失敗が残るが、原因（Playwright 環境か コードか）は実行しないと不明。
11. 指示文のセクション 2〜4 見出しが欠落していたため § 2〜4 は断片から再構成した。意図と異なる場合は追補する。

---

## 8. 所感（アップデート時の最重要注意点）

1. **銘柄・閾値の定義が 9 ファイルに分散重複**しており（config.INSTRUMENTS が SSoT になっていない）、銘柄追加・変更を伴うアップデートでは修正漏れがそのまま「サイレントなデータ欠落」になる。まず定義の一元化から着手すべき。
2. **バリデーションと stale 判定が「動いているつもりで動いていない」**（Twelve Data 銘柄のバリデーション不発、OECD/WALCL 系列の stale 未判定）。アップデートで信頼度スコアを強化する前に、この基盤の穴を塞がないとスコアの数字だけが立派になる。
3. 約 4 週間分の未コミット変更 + 未統合の `generate_xauusd_brief.py`（API 直叩き）が混在しており、**現在の HEAD はリポジトリの実態を表していない**。アップデート計画の基線を切る前に、まず現状の commit 整理（または破棄判断）が必要。
