# UNIFIED_DESIGN — チャート外分析 4 本→2 本統合（2026-08-11）

設計者: Claude (Fable 5)。前段調査: 4 プロンプトのセクション単位インベントリ + 起動フロー/データ供給の依存調査 + 独立 3 設計案の合議（2026-08-09〜10 実施）。

## 0. 2026-08-12/13 XAUUSD 特化再設計（本ドキュメントの上書き事項）

社長指示による再設計。以下は §1〜5 の記述を**上書き**する現行仕様（詳細な経緯は git log 参照）。

| 項目 | 現行仕様 |
|---|---|
| 銘柄範囲 | **XAUUSD 特化 + DXY 上流文脈のみ**（§2 の「非対称化」を置換）。USDJPY / BTCUSD は `/daily-bias <SYM>` / `intel.py brief --daily --symbol <SYM>` の個別指定時のみ、スリム版（master_prompt_symbol.md、`{{SYMBOL}}` 置換）で生成 |
| 定時配信 | **復活**（§2「オンデマンド化」を置換。社長確定指示 2026-08-12）。平日デイリー 18:00 JST / 土曜ウィークリー 07:00 JST を Hermes cron（intel-daily / intel-weekly、no-agent script、Telegram 配信）で実行。オンデマンド実行も並存 |
| チャート構造レベル | **FVG / EQH / EQL / Premium-Discount / OB のレベル一覧は本文非掲載**（社長の自力チャート認識訓練を阻害しないため）。XAU-TF アンカーは AI 内部判断専用（プラン価格帯・スコア #3・前回照合）。オーダーブック由来のリクイディティプールは掲載対象（チャートから見えない情報） |
| /xau-tf 単体レポート | **廃止**（コマンド + `.agents` 重複コピー削除）。計算エンジン run_report.py は内部データ供給役として存続（Brain/Calendar/XAU-TF の MD = アンカー入力、live-h1.csv = スイープ検証入力）。アンカー抽出にボラ/ATR百分位・COT百分位を追加 |
| リテール分析 | 新設 `scrapers/retail_analytics.py` — P/L 構造（損切り燃料判定）・プール距離・**前日プールに対するスイープ→反転検証**（0.5×ATR 閾値、`output/history/liquidity_pools.json`）。scraped_data に `### リテール分析` ブロックとして注入 |
| FedWatch | `scrapers/fedwatch_history.py` — スナップショット履歴（`output/history/fedwatch.json`）+ 前日比/前週比の決定論的計算（FOMC 会合切替ガード付き）。Daily は前日比 / Weekly は前週比を常時記載 |
| 振り返り蓄積 | 新設 `scrapers/bias_review.py` — Brain `Atlas/Bias-Review-Log.md` に構造化エントリを蓄積（同日同モード置換）。直近 5 件を report_anchor が `[Bias Review 直近5件]` として再注入（学習ループ）。PDF 本文の照合は Daily §8-1 = 3〜5 行 / Weekly §1 = コンパクト表 + 学び 3 点に圧縮 |
| Weekly 前回レビュー入力 | 新設 `scrapers/weekly_review.py` — main.py --weekly が scraped_data に `### 前回レビュー入力` を自動注入（interactive / headless 同一入力。旧 headless の「照合不能」バグ解消） |
| 追加契約 | 個別銘柄の出力は `scraped_data_{SYM}_DATE.*` + Brain `Calendar/Daily-Bias/{SYM}/`（既存 glob を汚染しない別名・別配置）。intel JSON に `review` フィールドを additive 追加（既存 6 キー不変）。intel.py は headless でも Brain git add/commit/push を行う |
| シークレット | `run-with-secrets.sh --batch` は旧トークンファイル → Keychain 管理の `~/.config/laa/op-run-batch.sh` へフォールバック（2026-08-11 移行後の headless 破損を修正） |
| 生成モデル | 定時配信は **Opus 5 / effort high** に明示ピン（`INTEL_CLAUDE_MODEL` / `INTEL_CLAUDE_EFFORT`）。未指定だとセッション既定を継承し `/model` 切り替えが定時配信に波及するため。Fable 5 は Max の週次プールを共有消費するので毎営業日の自動生成には使わない。手動 `/daily-bias` はセッションのモデルをそのまま使う |
| ニュース検索 | headless でもレポート本文生成時のみ `WebSearch` / `WebFetch` を許可（`INTEL_CLAUDE_ALLOWED_TOOLS`）。許可がないとセクション5 が常時「検索不可環境」で空になっていた |
| チャート外要素の拡充（2026-08-13） | ①**Disaggregated COT**（`scrapers/cot_disaggregated.py`、CFTC `72hh-3qpy`）— Managed Money / Swap Dealer / Producer 内訳と業者数。Legacy の "Large Spec" は Managed Money と Other Reportables を混ぜるため投機の実体が読めなかった ②**相関定量**（`scrapers/correlation.py`）— 20日/60日ローリング相関係数を日次リターンで計算し、無相関化時はスコア #1 を 0 にする規則をプロンプトに明記（水準相関は見せかけ相関になるため使わない）③**セッション統計**（`scrapers/session_stats.py`）— アジアレンジ スイープ率・刈り取り後の反転率・PDH/PDL スイープ率を H1 250 営業日から決定論計算。旧「季節性」は LLM 記憶頼みで廃止したが、本統計は実データ計算のためその問題を持たない。②③はネットワーク呼び出しを増やさない |
| 見送った要素 | オプション GEX / 上海プレミアム（無料で安定的に取れる公開ソースがなくスクレイパーが脆くなる）、米債入札結果（金への効果は実質金利経由で FRED DFII10 が既に捕捉しており重複） |

## 1. 結論

| 旧（4 本） | 新（2 本） |
|---|---|
| master_prompt.md（Daily 速報） | **master_prompt.md（統合 Daily）** — オンデマンドで「その瞬間の全体像」 |
| master_prompt_deep.md（Deep Daily） | ↑ に価値要素を吸収して廃止（archive/prompts/ に凍結） |
| master_prompt_weekly.md（Weekly 速報） | **master_prompt_weekly.md（統合 Weekly）** — 「前回レポート以降の振り返り + 来週の展望」 |
| master_prompt_deep_weekly.md（Deep Weekly） | ↑ に W1 先週レビュー等を吸収して廃止（同上） |

**深さ軸（速報/Deep）を廃止**し、時間軸 2 本のみ残す。理由:

1. 定期実行の廃止（オンデマンド化）により「Daily/Weekly」の意味が「毎朝/毎週月曜」から「トレード直前/週末レビュー」に変わった。深さの使い分けは自然に時間軸に畳み込まれる（直前=速い Daily、週末=深い Weekly）
2. Deep は Daily の上位互換ではなかった（セクション1.5 ファンダ大局は Daily 専有。Daily が 4 本中最も精錬されている——社長の認識と一致）
3. データ層は既に統合済みだった（`--weekly` の実データ差分は COT のみ、+約4秒）

## 2. 統合の主要判断

| 判断 | 内容 | 根拠 |
|---|---|---|
| COT 常時取得化 | main.py の weekly ゲートを外す | 差分 +4 秒。Daily でも XAU-TF 百分位の裏付けに使う |
| XAUUSD 価格レベルの委譲 | IPDA / 流動性マップ / PWH・PWL テーブル / COT 生値（XAU 分）を削除し XAU-TF アンカー引用に一本化 | 旧プロンプト自身が「検索ベースの参考値」と自認。Dukascopy 実データ（検証済みコード）に品質で負ける重複 |
| 季節性 / Quarterly Shift 削除 | 全廃 | データパイプラインが存在せず LLM の記憶頼み（ハルシネーション源）。tradeability も low |
| Weekly Profile 12 パターン / 曜日単位 PO3 予測 削除 | 全廃 | オンデマンド起動では予測対象の曜日の半分が既に過去 |
| 先週レビューの再設計 | 「暦週」基準 → 「前回レポート発行日以降」基準。入力はアンカー + 前回レポート本文 + intel JSON | 旧 W1-3 は前回レポートを Read する手順が未定義で実質ハルシネーションだった（AUDIT.md L282）。dev 全体で唯一の自己フィードバック機構なので実データ駆動で存続 |
| 信頼度スコアの一本化 | 7/8/11 項目の 3 体系 → 8 項目 1 体系（Daily/Weekly 共通） | 閾値: ≥7 High / 5-6 Med / 3-4 Med-cautious / ≤2 Low(様子見)。初期値であり実生成分布で調整 |
| 銘柄の非対称化 | XAUUSD フル / DXY 準フル / USDJPY・BTC コンパクト | 社長のトレードは XAUUSD 中心・夜間 killzone。4 銘柄対称が字数を 4 倍にしていた |
| WebSearch の削減（Daily） | 8〜12 → 基本 2 + 条件付き最大 4 | トレード直前の所要時間要件。scraped_data が 20 ラベル分の実データを既に持つ。深掘りは Weekly に集約（4〜8 クエリ） |
| 自己検証の軽量化 | 4 本立て + 自動再生成ループ → 3 チェック 1 パス | 生成時間を倍にする割に判断を変えない。欠損列挙は維持（黙る欠損の検知はデータ充足の生命線） |
| Routines / Slack 経路の削除 | コマンドから分岐を撤去 | STACK.md §23 で不採用決定済みの残骸 |
| X-Search 停止 | config.yaml x_search.enabled=false + Hermes cron pause | 社長判断（X は手動で直接確認）。8/1 以降 xAI 認証切れで死んでいた |

## 3. 出力（新運用）

| 成果物 | 置き場所 |
|---|---|
| MD（正本） | Brain `Calendar/{Daily,Weekly}-Bias/`（master 直接 commit+push、従来どおり） |
| PDF（常時生成） | `output/` + Google Drive ローカル同期 `マイドライブ/Trading/Bias-Reports/`（scripts/publish_report.py。Drive 未マウント時は WARN してスキップ） |
| 機械用 JSON | `output/intel/intel_{daily,weekly}_YYYY-MM-DD.json`（logos-engine 契約、無変更） |
| XAU-TF レポート | 従来どおり Brain `Calendar/XAU-TF/` + 同じ Drive フォルダに PDF |

起動: `/daily-bias`（約 4〜5 分。Step 0 で XAU-TF レポートが古ければ自動再生成 → main.py → 生成 → MD+PDF+push）/ `/weekly-bias`（約 8〜12 分。前回レポート・intel JSON を Read して前回レビューを実データで実施）/ ヘッドレスは `scripts/intel.py brief --daily|--weekly`（Hermes 経由の Telegram 起動も従来どおり）。

推論エンジン: 既定 Claude（サブスク枠、`claude -p`）。`INTEL_ENGINE=codex` で Codex CLI に切替可能なシームを intel.py に用意（実験的扱い）。

## 4. 維持した外部契約

1. H1 タイトル / `## セクション0: エグゼクティブサマリー` / `信頼度: High|Med|Med-cautious|Low`（render_report.py 表紙 + report_anchor 抽出）
2. Daily の `## 〜ファンダメンタル大局〜` 見出し（report_anchor が次回へ大局を継承）
3. XAUUSD レベル SSoT = XAU-TF アンカー（非 STALE 時。乖離時は「乖離: TD ○○ / XAU-TF ○○（採用）」）
4. scraped_data ファイル名規約（`scraped_data(_weekly)_YYYY-MM-DD.*`）と intel JSON 6 キー（logos-engine gates.py）
5. Brain 保存先ディレクトリ名・ファイル名規約（`*_Bias_Report_YYYY-MM-DD.md`）
6. Twelve Data 呼び出し設計（レート制限回避の直列実行）に不介入

変更した契約: report_anchor の DAILY_STALE_DAYS 3→7（オンデマンド間隔対応）、prev_daily にセクション0 抽出（`prev_daily_exec`）を追加（additive）。

## 5. 捨てたものの完全リスト（復活させる場合は本ドキュメントを更新すること）

- IPDA Data Range（20/40/60 日）テーブルと解釈 — XAU-TF §2/§3 が代替
- 未到達 PD Array テーブル — 「価格はチャートで確認」と自認していた空欄表
- 季節性 & Quarterly Shift（詳述・他銘柄サマリー・スコア項目）
- Weekly Profile 12 パターン推定 / 曜日単位 Weekly PO3 予測
- 週間パフォーマンステーブル / 先週イベント年表（→ セクション1 前回レビューに吸収）
- ICT テクニカルフォーカスレベル表（XAUUSD 分。他銘柄は TwelveData 値をプラン内で使用）
- COT 生テーブルの XAUUSD 行（XAU-TF 百分位を正とし、乖離時のみ併記）
- 4 銘柄対称のフル詳細分析（A〜D × 4 銘柄の繰り返し）
- Deep 系自己検証のフルセット（スコア再計算表 + 自動再生成ループ）
- 「プラン 2 は別銘柄優先」制約（→ 同一銘柄の逆条件シナリオ）
- Routines 環境分岐 / Slack 通知 / 字数制限の Routines 特例
- Hermes X-Search 統合（enabled=false。契約と実装は温存、docs/XSEARCH_INTEGRATION.md 参照）

## 6. 関連ファイル

- 実装: main.py / config.yaml / config.py / scripts/{intel,publish_report,render_report}.py / scrapers/report_anchor.py / .claude/commands/{daily-bias,weekly-bias}.md / tests/
- アーカイブ: archive/prompts/{master_prompt_deep,master_prompt_deep_weekly}.md
- dev ルート: .claude/commands/xau-tf.md（PDF 発行ステップ追加）
- 上流ドキュメント: ~/HQ/STACK.md §8（slash commands）/ §21（チャート外分析）を同日更新
