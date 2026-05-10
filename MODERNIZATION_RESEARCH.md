# Modernization Research — Claude Code 新機能でチャート外分析を簡素化できるか

調査日: 2026-05-09
調査者: @agent-researcher（Opus 4.7, 1M context）
対象期間: 2025-11 〜 2026-05 の Anthropic 公式アップデート
信頼度: **高**（公式 docs / Anthropic blog / 公式 GitHub を一次ソースとして確認済み）

---

## 1. エグゼクティブサマリー

3〜5 行で結論：

1. **2026-05-05 リリースの「Claude for Financial Services 2.0」で、Anthropic 公式のパートナー連携として Financial Modeling Prep (FMP) を含む 8 社の MCP コネクタが正式追加された**。FMP は equities/ETFs/crypto/forex/commodities を全てカバーし、本プロジェクトの XAUUSD/USDJPY/BTCUSD/DXY 取得を MCP 経由で完全に代替可能（一次情報: https://www.anthropic.com/news/finance-agents ）。
2. **Claude Code 標準ツールでの「ライブデータ取得能力強化」は限定的**。`web_fetch_20260209` でダイナミックフィルタリングが追加されたが、**JS レンダリングは依然として非対応**と公式ドキュメントに明記（Investing.com・MyFXBook 等の SPA スクレイピングは引き続き Playwright が必須）。
3. **Code Execution コンテナはインターネットアクセス完全無効**（"Internet access: Completely disabled for security"）。Anthropic API 経由で `requests` で直接外部 API を叩く戦略は不可。外部データは MCP サーバー経由か、クライアント側 Bash 実行（ローカル / Routines の cloud env）でしか取得できない。
4. **Routines / Web 環境のネットワークアクセスは「Trusted」がデフォルトで限定 allowlist**。**MCP コネクタは Anthropic サーバー経由で routing されるため allowlist 不要** — 外部スクレイピングを MCP 化すれば Routines 環境のネットワーク設定を「Trusted」のままで運用できる（現状「Full」で運用中）。
5. **置き換え推奨**: DXY / OHLCV / 経済指標カレンダー → FMP MCP、BTC ETF flow → CoinGlass の kukapay/etf-flow MCP（非公式だが OSS）、FRED マクロデータ → 複数の OSS FRED MCP。**MyFXBook / FXSSI / IG リテールセンチメントは公式 MCP なし** → スクレイピング継続必要。

---

## 2. Claude Code 標準ツールの最新動向（公式裏取りあり）

### 2.1 全組み込みツール一覧（2026-05-09 時点）

公式 tools-reference より抜粋（金融データ取得に関連するもの中心）：

| ツール | 機能 | 制限 |
|---|---|---|
| `WebFetch` | URL からコンテンツ取得 | **JS レンダリング非対応**（後述） |
| `WebSearch` | Web 検索 | API 別課金 |
| `Bash` | シェル実行 | クライアント側（ローカル or cloud env） |
| `Monitor` | バックグラウンドスクリプトの行毎ストリーム | v2.1.98+ |
| `Skill` | Skill の起動 | Bash/外部 API は Bash 経由 |

一次情報: https://code.claude.com/docs/en/tools-reference

### 2.2 WebFetch の最新仕様（API 経由）

`web_fetch_20260209`（最新版）の重要事項を公式ドキュメントから直接引用：

> **The web fetch tool currently does not support websites dynamically rendered via JavaScript.**

> Dynamic filtering: With the `web_fetch_20260209` tool version, Claude can write and execute code to filter the fetched content before loading it into context. (...) requires the code execution tool to be enabled.

- **対応モデル**: Claude Opus 4.7 / 4.6, Sonnet 4.6
- **JS レンダリング**: **非対応**（Investing.com の SPA、TradingView 等は依然として取れない）
- **PDF 自動抽出**: 対応
- **キャッシュ**: 自動（最新版を反映しないことあり）
- **追加コスト**: 無料（コード実行は web_fetch_20260209 利用時無料）

一次情報: https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-fetch-tool

**重要**: Claude Code（CLI / Web / Routines）の `WebFetch` ツールも内部的にこの API を呼ぶ可能性が高いが、Claude Code の `WebFetch` ツール定義には JS レンダリング対応の言及なし。**SPA からの取得は不可と仮定すべき**。

### 2.3 WebSearch の最新仕様

`web_search_20260209`（最新版）：

- ダイナミックフィルタリング対応（Code Execution と組み合わせ）
- 公式例示プロンプト: **"Search for the current prices of AAPL and GOOGL, then calculate which has a better P/E ratio."** — 株価ライブ取得が公式ユースケースとして提示されている
- 価格: **$10 / 1,000 searches**
- API ID: `web_search_20250305`（旧）と `web_search_20260209`（新）が併存

一次情報: https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool

### 2.4 Code Execution ツールの仕様（重大な制約）

`code_execution_20260120`（最新、Opus 4.5+ / Sonnet 4.5+）：

- Python 3.11.12 + bash + ファイル操作
- pre-installed: pandas, numpy, scipy, matplotlib, scikit-learn, pyarrow, openpyxl, pillow, sympy, **その他データサイエンス系のみ**
- **`yfinance` / `requests` / `httpx` などのネットワーク系はプリインストールされず**、かつ pip install しても下記制約で動作しない
- **"Internet access: Completely disabled for security. External connections: No outbound network requests permitted"** — 公式ドキュメントに明記
- Files API 経由でアップロードした CSV/Excel の解析はできるが、外部 API 直叩きは不可
- 価格: web_search/web_fetch と組み合わせ時無料、単独使用は最初の 1,550 hours/月無料

一次情報: https://platform.claude.com/docs/en/agents-and-tools/tool-use/code-execution-tool

**結論**: Code Execution は外部データ取得には使えない。あくまで取得済みデータの後処理用。

### 2.5 Computer Use の Claude Code 統合状況

調査の結果、**Computer Use は Claude Code の組み込みツールとしては提供されていない**。Claude Code から Computer Use を直接呼ぶ手段は公式ドキュメントには見つからなかった。Computer Use は Anthropic API の独立したツール（`computer_20250124` 等）として存在し、別途 SDK で使う必要がある。

不明点として残る: Claude Code の Chrome integration（https://code.claude.com/docs/en/overview の「Debug live web applications」）が browser automation に使えるかは要検証。

### 2.6 過去 6 ヶ月の Claude Code Changelog 抜粋（データ取得関連）

公式 changelog (https://code.claude.com/docs/en/changelog) から関連項目：

- **v2.1.105 (2026-04-13)**: WebFetch が `<style>` / `<script>` を自動除去 → CSS-heavy ページの context 効率化
- **v2.1.111 (2026-04-16)**: PowerShell tool 追加（ロールアウト中）
- **v2.1.105 (2026-04-13)**: `Monitor` tool 追加（バックグラウンドスクリプトの行毎ストリーム）
- **v2.1.121 (2026-04-28)**: MCP server config に `alwaysLoad` オプション追加（tool-search deferral をスキップ）
- **v2.1.128 (2026-05-04)**: `/mcp` で tool count 表示、再接続時の重複表示抑制

**金融データ系の組み込みツール追加は changelog 上では確認できず**。すべて MCP 経由で実現する設計。

---

## 3. 利用可能な金融データ系 MCP / コネクタ

### 3.1 Anthropic 公式 financial-services プラグイン（2026-05-05 リリース）

**リリース**: 2026-05-05 "Agents for financial services" 発表
**一次情報**:
- https://www.anthropic.com/news/finance-agents
- https://github.com/anthropics/financial-services
- https://support.claude.com/en/articles/13851150-install-financial-services-plugins-for-cowork

#### インストール方法（Claude Code 公式手順）

```bash
# マーケットプレイス追加
claude plugin marketplace add anthropics/claude-for-financial-services

# コアプラグイン（financial-analysis）— 全 connector を含む
claude plugin install financial-analysis@claude-for-financial-services
```

#### 同梱される MCP コネクタ一覧（11 個、公式 README より）

| プロバイダ | URL | 主な用途 |
|---|---|---|
| Daloopa | https://mcp.daloopa.com/server/mcp | SEC filings 由来 fundamentals |
| Morningstar | https://mcp.morningstar.com/mcp | ファンド/投資分析 |
| S&P Global (Kensho) | https://kfinance.kensho.com/integrations/mcp | Capital IQ 財務 |
| FactSet | https://mcp.factset.com/mcp | 全般市場データ |
| Moody's | https://api.moodys.com/genai-ready-data/m1/mcp | クレジット/レーティング |
| MT Newswires | https://vast-mcp.blueskyapi.com/mtnewswires | ニュース |
| LSEG | https://api.analytics.lseg.com/lfa/mcp | FX, スワップカーブ, ボラティリティ |
| PitchBook | https://premium.mcp.pitchbook.com/mcp | プライベートマーケット |
| Chronograph | https://ai.chronograph.pe/mcp | PE ファンド分析 |
| Egnyte | https://mcp-server.egnyte.com/mcp | ドキュメント保管 |

これらは多くが**機関投資家向けライセンス必須**（FactSet, S&P Global, Moody's, LSEG, PitchBook 等）。個人トレーダーには現実的でない。

#### 2026-05-05 に追加された 8 社（全て同イベントで発表）

公式発表（https://www.anthropic.com/news/finance-agents ）より：

| プロバイダ | データ範囲 | 個人利用可能性 |
|---|---|---|
| Dun & Bradstreet | 企業 ID 検証 | 低 |
| Fiscal AI | 上場企業 fundamentals | 中 |
| **Financial Modeling Prep** | **quotes, fundamentals, statements, filings, transcripts across equities, ETFs, crypto, forex, and commodities** | **高（個人プラン $19/月〜）** |
| Guidepoint | エキスパートインタビュー | 低 |
| IBISWorld | 業界データ | 中 |
| SS&C Intralinks | DealCenter AI | 低 |
| Third Bridge | エキスパートインタビュー | 低 |
| Verisk | 保険データ | 低 |

**本プロジェクトの最有力候補は Financial Modeling Prep**。

### 3.2 Financial Modeling Prep MCP（最重要）

**公式 MCP server（FMP 提供）**:

- ホスト URL: `https://financial-modeling-prep-mcp-server-production.up.railway.app/mcp`
- 認証: `FMP_ACCESS_TOKEN` を session config で渡す
- 250+ tools across 24 categories: stocks, ETFs, crypto, forex, commodities, economics 他
- 一次情報: https://github.com/imbenrabi/Financial-Modeling-Prep-MCP-Server

**Claude Code への登録コマンド（推定、公式ドキュメントの形式に基づく）**:

```bash
# リモート HTTP 接続
claude mcp add fmp --transport http \
  https://financial-modeling-prep-mcp-server-production.up.railway.app/mcp \
  --header "Authorization: Bearer $FMP_API_KEY"

# または、Anthropic 公式 financial-services プラグインに含まれる FMP コネクタを使う
claude plugin install financial-analysis@claude-for-financial-services
```

**料金**:
- Free: 250 calls/day（テスト用）
- Starter: $19/月、300 calls/分、5 年履歴
- Premium: $49/月、750 calls/分、30+ 年履歴

一次情報: https://medium.com/@kevinmenesesgonzalez/how-to-connect-claude-to-real-financial-data-with-fmp-mcp-c3e7dce777fd
ただし MCP server URL とコマンドは GitHub README が一次情報。料金は FMP 公式サイト（site.financialmodelingprep.com）で再確認推奨。

#### FMP MCP でカバー可能なシンボル

公式 README + 関連記事より：

- **Forex**: 主要ペア（USDJPY, EURUSD, GBPJPY 等）対応
- **Indices/DXY**: USDX index にアクセス可能（FMP API のドキュメント要確認）
- **Crypto**: BTCUSD, ETHUSD 等
- **Commodities**: Gold (XAU), Silver, Oil 等
- **Bonds/Yields**: US10Y, US30Y 等の treasury yields
- **Economic Calendar**: GDP, CPI, NFP, FOMC 等の経済指標
- **ETF Holdings/Flows**: BTC ETF を含む

**未確証**: 各シンボルの具体的な ticker symbol は FMP API ドキュメントで個別確認必要。Houtini 記事（https://houtini.com/articles/ai-in-finance-using-financial-modeling-prep-mcp-for-real-time-market-data-in-claude/ ）では DXY/USDJPY/XAUUSD/BTCUSD/US10Y への明示的言及なし。**実装前に FMP API ダッシュボードで個別 ticker 検索すること**。

### 3.3 FRED MCP（Federal Reserve 経済データ）

複数の OSS 実装あり。代表例：

| Implementation | URL | カバー範囲 |
|---|---|---|
| stefanoamorelli/fred-mcp-server | https://github.com/stefanoamorelli/fred-mcp-server | 800,000+ 時系列、Docker / Node.js インストール |
| cfdude/mcp-fred | https://github.com/cfdude/mcp-fred | FRED API 全エンドポイント |
| Jaldekoa/mcp-fredapi | PulseMCP 経由 | 主要マクロ系列 |

認証: FRED API key（無料、https://fred.stlouisfed.org/ で取得）

**Anthropic 公式の FRED MCP は存在しない**（Moody's が一部マクロデータをカバーするが機関ライセンス必要）。

### 3.4 Polygon.io MCP（公式）

- **GitHub（公式）**: https://github.com/polygon-io/mcp_polygon
- 35+ tools: stocks, options, forex, crypto の OHLCV / quotes / market snapshots
- インストール: `claude mcp add-json "polygon" ...` + `POLYGON_API_KEY` 環境変数
- Polygon 公式運営（Massive.com にリブランド進行中）

一次情報: https://github.com/polygon-io/mcp_polygon
インテグレーション例: https://composio.dev/toolkits/polygon_io/framework/claude-code

### 3.5 CoinGlass / BTC ETF Flow MCP（OSS）

非公式 OSS だが該当機能あり：

- **kukapay/etf-flow-mcp**: https://github.com/kukapay/etf-flow-mcp
  - CoinGlass API から BTC/ETH の ETF flow を構造化 markdown table で返す
  - 履歴データ集計、pivot formatting
- **rcz87/coinglass-crypto-derivatives**: PulseMCP 経由
  - funding rates, open interest, liquidation maps, order book
  - 現状の本プロジェクトの CoinGlass スクレイパー部分を完全代替候補

### 3.6 経済指標カレンダー MCP

専用 MCP は確認できず。以下が候補：

1. **FMP MCP の `economic_calendar` エンドポイント**（FMP 経由で FOMC/NFP/CPI 等を取得） — **第一候補**
2. **claude-trading-skills の Economic Calendar Fetcher Skill**（同じく FMP 経由）
   - https://github.com/tradermonty/claude-trading-skills
3. Investing.com MCP・Trading Economics MCP・MyFXBook MCP は **公式存在せず**（OSS も発見できず）

### 3.7 リテールセンチメント（MyFXBook / FXSSI / IG）

**該当する公式 MCP / OSS MCP は発見できなかった**。スクレイピング継続が現実解。
（要追加調査 — PulseMCP / glama.ai / mcpservers.org の最新カタログを定期監視推奨）

---

## 4. 本プロジェクトでの置き換え提案

| データ | 現状 | 置換候補 | 一次情報 URL |
|---|---|---|---|
| **XAUUSD OHLCV** | Twelve Data API | **FMP MCP**（commodities セクション） | https://github.com/imbenrabi/Financial-Modeling-Prep-MCP-Server |
| **USDJPY OHLCV** | Twelve Data API | **FMP MCP**（forex セクション） | 同上 |
| **BTCUSD OHLCV** | Twelve Data API | **FMP MCP**（crypto セクション） or Polygon MCP | https://github.com/polygon-io/mcp_polygon |
| **DXY** | Investing.com スクレイピング（Twelve Data 非対応のため） | **FMP MCP** または FRED MCP の DTWEXBGS 系列 | https://fred.stlouisfed.org/series/DTWEXBGS |
| **US10Y 利回り** | Investing.com スクレイピング | **FMP MCP** treasury yields or FRED MCP の DGS10 | https://fred.stlouisfed.org/series/DGS10 |
| **経済指標カレンダー** | Investing.com スクレイピング | **FMP MCP `economic_calendar` API** | https://site.financialmodelingprep.com/developer/docs#economic-calendar |
| **FedWatch** | CME スクレイピング | FMP MCP / FRED MCP では完全代替できない可能性 → **CME 公式 API 直叩きをスキル化** | https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html |
| **BTC ETF flow** | SoSo Value 等スクレイピング | **kukapay/etf-flow-mcp**（CoinGlass バックエンド） | https://github.com/kukapay/etf-flow-mcp |
| **CoinGlass L/S Ratio + Funding Rate** | Playwright スクレイピング | **rcz87/coinglass-crypto-derivatives MCP** | https://www.pulsemcp.com/servers/rcz87-coinglass-crypto-derivatives |
| **MyFXBook センチメント** | Playwright | **公式 MCP なし → スクレイピング継続** | — |
| **FXSSI Current Ratio** | Playwright | **公式 MCP なし → スクレイピング継続** | — |
| **IG Client Sentiment** | Playwright | **公式 MCP なし → スクレイピング継続** | — |
| **CFTC COT** | 公式 API（既に最適） | 変更不要 | https://publicreporting.cftc.gov/ |

### 4.1 Routines 環境での運用最適化

現在の README より、Routines は「Network access: Full」で運用中。FMP / FRED / CoinGlass MCP に移行すれば：

- **MCP コネクタは Anthropic サーバー経由で routing される**ため、cloud env の allowlist 不要（公式ドキュメント記載）
- 残るスクレイピング（MyFXBook / FXSSI / IG）の domain だけ「Custom」allowlist に登録
- → Network access を「Full」から「Custom（必要 domain のみ）」に絞れる ＝ セキュリティ向上

一次情報: https://code.claude.com/docs/en/routines#environments-and-network-access

---

## 5. 置き換えできない / すべきでないもの

### 5.1 Code Execution コンテナで yfinance / requests を使う案

**不可**。Anthropic Code Execution コンテナは "Internet access: Completely disabled" と公式明記。
> Reference: https://platform.claude.com/docs/en/agents-and-tools/tool-use/code-execution-tool#networking-and-security

外部 API 呼び出しは Bash tool（クライアント側）か MCP 経由でしかできない。

### 5.2 WebFetch で Investing.com / TradingView を取る案

**不可**。WebFetch は **JS レンダリング非対応**と公式明記：
> "The web fetch tool currently does not support websites dynamically rendered via JavaScript."

DXY ティッカーページ、経済指標カレンダー、TradingView チャート画面はすべて SPA。WebFetch では取得不可。

### 5.3 Anthropic 公式 financial-services プラグインの全部入り採用

**慎重判断推奨**。理由：
- 11 connectors のうち FactSet / S&P Global / Moody's / LSEG / PitchBook は**機関投資家ライセンス必須**で個人不可
- 個人トレーダーが恩恵を受けるのは Daloopa（SEC filings）、Morningstar（一部）、MT Newswires のみ
- → **financial-analysis プラグインを丸ごとインストールするより、必要な MCP（FMP, FRED, CoinGlass）を個別 add する方が軽量**

### 5.4 リテールセンチメント MCP 自前実装

公式 MCP がない MyFXBook / FXSSI / IG をどうしても使いたい場合、自分で MCP server 実装する選択肢はあるが、**保守コストはスクレイピング維持と同等**。今回はスクレイピング継続で問題なし。

### 5.5 Computer Use 経由のブラウザ自動化

Claude Code の組み込みツールとしては未提供（2026-05-09 時点）。導入のためには Anthropic API を別途叩く構成が必要で、複雑度が増す。
**現状 Playwright で動いているなら積極的に Computer Use に置き換える理由は薄い**。

---

## 6. 推奨実装プラン（Phase 分け）

### Phase 0 — 検証（2〜3 時間）

1. FMP の Free アカウント取得（FMP_API_KEY 発行）
2. FMP MCP を Claude Code に add：
   ```bash
   claude mcp add fmp --transport http \
     https://financial-modeling-prep-mcp-server-production.up.railway.app/mcp \
     --header "Authorization: Bearer $FMP_API_KEY"
   ```
3. Claude Code セッションで以下を確認：
   - DXY ticker の実在（"USDX" or "DX-Y.NYB" or 他のシンボル名）
   - USDJPY / XAUUSD / BTCUSD の OHLCV 取得（直近 7 日 1H 足）
   - economic_calendar API の精度（次週 FOMC が含まれるか）
   - US10Y treasury yield 取得可否
4. 不足あれば FRED MCP（stefanoamorelli/fred-mcp-server）を追加検討
5. 結果を `tasks/lessons.md` に記録

### Phase 1 — Twelve Data 完全置換（1 日）

1. `scrapers/economic_calendar.py` の Investing.com スクレイピングを廃止 → FMP MCP の `economic_calendar` 呼び出しに変更
2. `main.py` の Twelve Data 価格取得部分を FMP MCP 呼び出しに変更
3. Investing.com の DXY / US10Y スクレイピングを廃止 → FMP or FRED に置換
4. **CoinGlass スクレイピングを `rcz87/coinglass-crypto-derivatives` MCP に置換検討**（OSS なので動作確認後に判断）

### Phase 2 — Routines 環境のネットワーク絞り込み（30 分）

1. `claude.ai/code/routines` で ict-daily-bias / ict-weekly-bias の Environment を編集
2. Network access を「Full」→「Custom」に変更
3. Allowed domains に MyFXBook / FXSSI / IG / SoSo Value のみ追加
4. （MCP コネクタは自動 routing なので allowlist 不要）
5. 1 回 Run now で動作確認

### Phase 3 — リテールセンチメント保守体制（継続）

1. MyFXBook / FXSSI / IG スクレイパーは Playwright で継続
2. 月次セレクタチェックを `tasks/todo.md` のリマインダーに追加
3. PulseMCP / glama.ai を四半期ごとに監視し、リテールセンチメント MCP 登場時に再評価

### Phase 4（オプション） — Anthropic 公式 financial-services プラグイン採用

個人プランで意味がある場合のみ：
- Daloopa MCP は SEC filings の自動アクセスに有用（Bias Report のファンダメンタルズ強化）
- ただしライセンス確認必要
- 採用判断は Phase 1 の効果検証後

---

## 補遺 — 確証が取れていない項目（要追加調査）

| 項目 | 不明点 | 検証方法 |
|---|---|---|
| FMP の DXY 取扱 | DXY 系列の正確な ticker 名（USDX / DX-Y.NYB / その他） | FMP API ダッシュボード（要 API key 取得後） |
| FMP の経済指標カレンダーの粒度 | 過去どこまで遡れるか、forecast/actual/previous の網羅性 | 同上 |
| Anthropic 公式 financial-services プラグインの個人プラン制約 | 各 connector のライセンス要件詳細 | https://support.claude.com/en/articles/13851150-install-financial-services-plugins-for-cowork |
| Claude Code の Chrome integration が SPA 取得に使えるか | ドキュメント未読 | https://code.claude.com/docs/en/chrome を別途確認推奨 |
| `web_fetch_20260209` の dynamic filtering で SPA データを取れるか | 公式は「JS 非対応」と明記。ただし dynamic filtering で何らかの workaround があるかは要実験 | 実機で試す |
| CME FedWatch の API 化 | CME 公式 API の有無、認証方式 | https://www.cmegroup.com/market-data/cme-group-api-services.html |

---

## 主要参照 URL（一次情報）

### Anthropic 公式 docs
- https://code.claude.com/docs/en/overview — Claude Code overview
- https://code.claude.com/docs/en/changelog — 直近 changelog
- https://code.claude.com/docs/en/tools-reference — 全組み込みツール一覧
- https://code.claude.com/docs/en/mcp — MCP 接続
- https://code.claude.com/docs/en/skills — Skill 仕様
- https://code.claude.com/docs/en/routines — Routines
- https://code.claude.com/docs/en/claude-code-on-the-web — Cloud env
- https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool — Web Search
- https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-fetch-tool — Web Fetch
- https://platform.claude.com/docs/en/agents-and-tools/tool-use/code-execution-tool — Code Execution

### Anthropic 公式 blog / GitHub
- https://www.anthropic.com/news/finance-agents — Agents for financial services（2026-05-05）
- https://github.com/anthropics/financial-services — 公式 financial-services リポジトリ
- https://support.claude.com/en/articles/13851150-install-financial-services-plugins-for-cowork — Cowork plugin インストール手順

### MCP server 一次情報
- https://github.com/imbenrabi/Financial-Modeling-Prep-MCP-Server — FMP MCP（コミュニティ実装、人気）
- https://github.com/polygon-io/mcp_polygon — Polygon 公式 MCP
- https://github.com/stefanoamorelli/fred-mcp-server — FRED MCP（コミュニティ）
- https://github.com/kukapay/etf-flow-mcp — ETF Flow MCP（CoinGlass バックエンド）
- https://www.pulsemcp.com/servers/rcz87-coinglass-crypto-derivatives — CoinGlass derivatives MCP

### 補助情報（三次ソース）
- https://blakecrosley.com/blog/code-with-claude-sf-2026-recap — Code with Claude 2026 recap
- https://medium.com/@kevinmenesesgonzalez/how-to-connect-claude-to-real-financial-data-with-fmp-mcp-c3e7dce777fd — FMP MCP 使用例
- https://houtini.com/articles/ai-in-finance-using-financial-modeling-prep-mcp-for-real-time-market-data-in-claude/ — FMP MCP 紹介
- https://github.com/tradermonty/claude-trading-skills — トレード用 Skills 集

---

**調査者注**: 本レポートは 2026-05-09 時点の公式 docs に基づく。Anthropic は四半期ごとに大きな更新を入れるため、6 ヶ月後（2026 Q4）には FMP 以外の個人向け金融 MCP が追加されている可能性が高い。Phase 3 の継続監視を必須とすること。
