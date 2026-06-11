# 未導入 OSS MCP — 机上検証レポート

作成日: 2026-05-19
位置付け: **導入実装の前段。本書時点では実装しない。**
判断材料: 既存の [`MODERNIZATION_RESEARCH.md`](../MODERNIZATION_RESEARCH.md) で確認済みの一次情報 + 本書の机上整理。

対象 3 件:
1. Financial Modeling Prep (FMP) — Free 枠
2. CoinGlass 系 OSS MCP（`rcz87/coinglass-crypto-derivatives`）
3. ETF Flow OSS MCP（`kukapay/etf-flow-mcp`）

導入判断は社長が下す。本書は **机上で確認できた事実 / 不明点 / 想定運用 / リスク** だけを並べる。

---

## 1. FMP Free 枠

### 1-1. 基本仕様（一次情報）
| 項目 | 値 |
|---|---|
| MCP server URL | `https://financial-modeling-prep-mcp-server-production.up.railway.app/mcp` |
| 認証 | HTTP `Authorization: Bearer $FMP_API_KEY` header / env |
| GitHub | https://github.com/imbenrabi/Financial-Modeling-Prep-MCP-Server |
| 公式 docs | https://site.financialmodelingprep.com/developer/docs |
| Anthropic 公式採用 | 2026-05-05 "Agents for financial services" 発表時に 8 社の 1 社として正式同梱 |
| 料金 | Free 250 calls/day / Starter $19/月 300 calls/min / Premium $49/月 |

### 1-2. 本プロジェクトでの想定置換対象
- `scrapers/economic_calendar.py`（Investing.com Playwright → FMP `economic_calendar` REST）
- `scrapers/dxy.py`（Investing.com / MarketWatch / Stooq → FMP forex/indices ティッカー）
- `scrapers/twelvedata.py`（XAUUSD / USDJPY / BTCUSD OHLCV → FMP commodities / forex / crypto）
- 副次: FRED から取れない補助系列（VIX, SPX 当日値 等）

### 1-3. Free 250 calls/day で本プロジェクトを賄えるか
| 1 回の Daily 実行で必要な FMP 呼び出し（推定） | 回数 |
|---|---|
| economic_calendar（当日 + 翌日） | 1 |
| DXY 現値 + 前日比 | 1 |
| XAUUSD / USDJPY / BTCUSD の OHLCV（24h, 1H 等） | 3 |
| US10Y / US2Y 利回り（FRED で代替済みだが二重化候補） | 0–2 |
| 補助（VIX, SPX 等） | 1–2 |
| **計** | **6–9 calls / 実行** |

- Daily を 1 日 1〜2 回 + Weekly + ad-hoc Deep Bias を含めても 1 日 30 calls を超えない見込み。
- **Free 250 calls/day で平時運用は十分**。月跨ぎ復元・障害再試行・新規シンボル検証で一時的に増えても 100/day 以下に収まる想定。
- **Starter $19/月への移行が必要になるシナリオ**: 5 分足以上の高頻度ポーリング / 過去 5 年超のヒストリ取得 / Deep Bias の網羅クエリを 1 日 10 回以上回す場合。**現状の Daily/Weekly では不要。**

### 1-4. 不明点（実機 API key 取得後に検証）
| 項目 | 検証クエリ |
|---|---|
| DXY ticker の実在 | `/quote/USDX` / `/quote/DX-Y.NYB` / `/quote/^DXY` のいずれが当たるか |
| economic_calendar の粒度・国フィルタ | `?from=YYYY-MM-DD&to=YYYY-MM-DD&country=US` 等のクエリパラメータ仕様 |
| Free 枠の rate-limit ヘッダ | 429 時の `Retry-After` ヘッダの有無 |
| FX ティッカー命名 | `USDJPY` か `USDJPY=X` か `EURUSD` の表記 |
| US Treasury yields | `/treasury` エンドポイントの粒度（1Y / 2Y / 5Y / 10Y / 30Y） |

### 1-5. リスク
- **Railway 上のホスティング**: コミュニティ実装は Railway 個人 deployment。サービス停止リスクあり（過去 12 ヶ月の uptime 公開なし）。Anthropic 公式 `financial-analysis` プラグイン経由なら Anthropic 側が routing するため独立。
- **Free 枠の終了**: FMP が Free 枠を縮小した場合（過去 2 年で 500→250 と縮小実績）、Daily 実行が即停止する可能性。
- **MCP server の symbol mapping**: 実 REST API のレスポンスを LLM 経由で返すため、symbol 不一致時のエラーメッセージが verbose になり、context を圧迫する懸念。
- **既存 schema との接合**: 戻り値は OHLCV の素 dict。`scrapers/metadata_schema.py` の `ensure_metadata()` でラップする運用を想定。

### 1-6. 導入判断の Go/No-Go 基準（案）
- **Go**: ① DXY ticker が確認できる ② economic_calendar に少なくとも FOMC / NFP / CPI / GDP が forecast 付きで含まれる ③ Free 枠で Daily 連続 1 週間が回る — の **3 条件全充足**。
- **No-Go**: Free 枠で 1 日でも 429 を踏む / DXY ticker 不明 / Anthropic 公式 8 社リストから将来外される兆候あり。

---

## 2. CoinGlass OSS MCP（`rcz87/coinglass-crypto-derivatives`）

### 2-1. 基本仕様（一次情報）
| 項目 | 値 |
|---|---|
| 一覧 | https://www.pulsemcp.com/servers/rcz87-coinglass-crypto-derivatives |
| 想定リポジトリ | rcz87/coinglass-crypto-derivatives（PulseMCP 経由ホスト） |
| 認証 | CoinGlass API key（無料枠あり、課金枠もあり） |
| 取得対象 | funding rates, open interest, liquidation maps, order book heatmap |
| 公式 CoinGlass docs | https://docs.coinglass.com/ |

### 2-2. 本プロジェクトでの想定置換対象
- `scrapers/coinglass.py`（Playwright で `https://www.coinglass.com/LongShortRatio` を SPA レンダー → DOM 抽出）
- `scrapers/crypto_funding.py`（Binance/Bybit/OKX 個別取得 + 3 取引所平均算出）の **二重化チェック**

### 2-3. 置換価値
- **Playwright 依存を消せる** → Routines 環境の SSL / chromium download / セレクタ崩れリスクが消える。
- **取得値の網羅性向上**: 現状の Playwright は Long/Short Ratio と Funding Rate の 2 種のみ。MCP 化で OI / liquidation も自然に取れる → Deep Bias 強化に直結。
- **セレクタ保守コストゼロ**: CoinGlass の DOM 更新で月次 1–2 件発生していたメンテが不要になる。

### 2-4. 不明点
| 項目 | 検証方法 |
|---|---|
| API key 取得経路と無料枠の上限 | https://www.coinglass.com/account |
| MCP server がカバーする tool 一覧 | PulseMCP の tool 詳細ページ |
| GitHub 上に独立 OSS リポジトリがあるか | `rcz87/coinglass-crypto-derivatives` で GitHub 検索 |
| レート制限 | CoinGlass docs（V3 API は req/min 制限あり、free は 30 req/min 程度との情報） |
| MCP のリモート / ローカル モード | PulseMCP 経由の hosted モードか、self-host が必要か |

### 2-5. リスク
- **個人運営の OSS**: rcz87 は個人開発者。サーバー停止リスク・脆弱性パッチの遅延リスク。
- **CoinGlass API の変更**: V2 → V3 移行が過去 18 ヶ月で発生。MCP 側のメンテが追従しない可能性。
- **API key の取り扱い**: macOS Keychain に `COINGLASS_API_KEY` として保管する想定（FRED と同形式）。

### 2-6. 導入判断の Go/No-Go 基準（案）
- **Go**: ① 無料枠で Daily 30 req/day を 1 週間連続でクリア ② Long/Short Ratio + Funding Rate + OI の 3 系列を MCP で取得できる ③ 既存 `scrapers/coinglass.py` と数値が ±5% 以内で一致。
- **No-Go**: 無料枠が 5 req/min 未満 / OI が取れない / 既存値と数値が大きく乖離。

---

## 3. ETF Flow OSS MCP（`kukapay/etf-flow-mcp`）

### 3-1. 基本仕様（一次情報）
| 項目 | 値 |
|---|---|
| GitHub | https://github.com/kukapay/etf-flow-mcp |
| バックエンド | CoinGlass API（BTC / ETH スポット ETF の集計データ） |
| 戻り値形式 | 構造化 markdown table（{date, ETF ticker, net flow USD M, total}） |
| 認証 | CoinGlass API key |
| 想定 tool | `get_etf_flow`, `get_etf_flow_history`, pivot 形式の summary |

### 3-2. 本プロジェクトでの想定置換対象
- `scrapers/btc_etf.py`（SoSo Value / Farside 等のスクレイピング → CoinGlass 集計）

### 3-3. 置換価値
- **集計済みデータが返る** → 現状のように個別 ETF（IBIT, FBTC, BITB, ...）を縦に並べて自前で合計する処理が不要。
- **未発表セルの扱いが明確**: CoinGlass は当日数値未発表の ETF を明示的に空セルで返す → 現状の `flows[etf] = None` を「未発表」と表記する main.py のロジックがそのまま流用可能。
- **ETH ETF も同 API で取れる**: 将来的に ETH ETF flow を追加する場合の拡張余地。

### 3-4. 不明点
| 項目 | 検証方法 |
|---|---|
| CoinGlass 無料枠で ETF flow API が叩けるか（有料 only の可能性） | https://docs.coinglass.com/v3/reference/post-etf |
| 当日 flow の更新タイミング（NY 時間 16:00 ET 直後か翌日午前か） | 実機で 1 週間タイムスタンプを観測 |
| Asia 時間に取得した場合の「今日」表記の扱い | 同上 |
| 過去 N 日 history の上限 | docs に明記がなければ実機検証 |

### 3-5. リスク
- **kukapay/etf-flow-mcp の保守頻度**: 過去 6 ヶ月の commit 履歴を GitHub で要確認。
- **CoinGlass MCP（§2）との API key 重複**: 同じ key を 2 つの MCP で共有するなら、Keychain の 1 entry で済む。
- **markdown table 戻り値**: LLM 直渡し前提のフォーマット。Python 側で parse する場合に正規表現でセル抽出が必要。

### 3-6. 導入判断の Go/No-Go 基準（案）
- **Go**: ① CoinGlass の ETF flow が無料枠で叩ける ② 既存 `scrapers/btc_etf.py` と過去 7 日分の数値が ±1M USD 以内で一致 ③ 未発表セルの扱いが明示的。
- **No-Go**: 有料 only / 既存値と乖離が大きい / 当日値の更新タイミングがレポート時刻（朝 JST）に間に合わない。

---

## 4. 3 件横断の判断軸

### 4-1. CoinGlass API key を 1 つに集約できる
- CoinGlass MCP（§2）と ETF Flow MCP（§3）は **同じ CoinGlass API key で運用可能**。Keychain には `COINGLASS_API_KEY` 1 entry で足りる。

### 4-2. Routines 環境の Network access を絞れる
- 3 件すべて MCP 化すれば、Routines の Network access を「Full」→「Custom」に絞れる
  （MyFXBook / FXSSI / IG の Playwright 残存 domain のみ allowlist）。
  詳細は [`MODERNIZATION_RESEARCH.md §4-1`](../MODERNIZATION_RESEARCH.md)。

### 4-3. 共通メタデータスキーマとの整合
- 全 3 件で MCP 経由戻り値を `scrapers/metadata_schema.ensure_metadata()` でラップして取り込む方針。
- `source` 名は `FMP` / `CoinGlass MCP` / `ETF Flow MCP (CoinGlass backend)` を提案。

### 4-4. 優先度（推奨）
1. **FMP Free**（最大の置換範囲、Anthropic 公式採用済みで信頼度高）
2. **CoinGlass OSS MCP**（Playwright を 1 系統消せる、保守工数の削減効果）
3. **ETF Flow OSS MCP**（CoinGlass MCP 導入のついでに同時検討、key 共有可）

### 4-5. 一括検証フロー（実装時の参考、現段階では未実施）
1. CoinGlass / FMP の API key を取得（FRED と同じく 1Password → Keychain 転記）
2. `claude mcp add` で 3 件を順次登録（ローカルセッションで動作確認）
3. Daily Bias 実行内で **既存スクレイパーと MCP 戻り値の数値比較** を 1 週間継続
4. 数値が安定一致したらスクレイパー本体を MCP 呼び出しに置換、`scrapers/<name>.py` は archive へ
5. `tasks/lessons.md` と本書を更新（採用 or 不採用の判断結果）

---

## 5. 参照リンク（一次情報）

### Anthropic 公式
- https://www.anthropic.com/news/finance-agents — Agents for financial services（2026-05-05）
- https://support.claude.com/en/articles/13851150-install-financial-services-plugins-for-cowork — Cowork plugin インストール手順

### MCP server / API 一次情報
- https://github.com/imbenrabi/Financial-Modeling-Prep-MCP-Server — FMP MCP
- https://site.financialmodelingprep.com/developer/docs — FMP REST docs
- https://www.pulsemcp.com/servers/rcz87-coinglass-crypto-derivatives — CoinGlass MCP
- https://docs.coinglass.com/ — CoinGlass API docs
- https://github.com/kukapay/etf-flow-mcp — ETF Flow MCP

### 関連ドキュメント
- [`MODERNIZATION_RESEARCH.md`](../MODERNIZATION_RESEARCH.md) — 全体の置換戦略
- [`scrapers/metadata_schema.py`](../scrapers/metadata_schema.py) — 共通メタデータ helper（本書と同セッションで追加）
