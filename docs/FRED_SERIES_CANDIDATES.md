# FRED 追加系列 — 候補比較レポート

作成日: 2026-05-19
位置付け: **採用実装の前段。本書時点では実装しない。**
判断: 社長
前提: FRED API key は Keychain に登録済み、`scrapers/fred.py` で DGS10 / DGS2 / DTWEXBGS の 3 系列を稼働中。

---

## 1. サマリー（採用優先度の机上判定）

| 優先度 | 系列 ID | 名称 | 更新頻度 | ICT Bias レポートへの主な利得 |
|---|---|---|---|---|
| **A（強く推奨）** | `DFII10` | 10Y TIPS yield（実質金利） | Daily | XAUUSD の最も信頼度の高いマクロドライバー。実質金利低下 = ゴールド bull。 |
| **A（強く推奨）** | `T10YIE` | 10Y Breakeven Inflation Rate | Daily | インフレ期待。XAUUSD（インフレヘッジ需要）と USD（Fed pivot）の両方に効く。 |
| **B（推奨）** | `SOFR` | Secured Overnight Financing Rate | Business-daily | 短期 USD funding コスト。Fed funds の市場実勢、流動性ストレスの早期指標。 |
| **C（条件付き）** | `DFF` | Effective Federal Funds Rate | Daily | Fed funds の **実績値**（NY Fed が日次集計）。FOMC 決定と一致。`FEDFUNDS` の高頻度版。 |
| **D（重複）** | `FEDFUNDS` | Federal Funds Effective Rate | Monthly | `DFF` の月次集計。`DFF` を採用するなら不要。 |

---

## 2. 系列ごとの詳細

### 2-1. DFII10 — 10Y TIPS yield（実質金利）【優先度 A】

- **意味**: 10Y Treasury Inflation-Protected Securities の利回り。名目金利からインフレ期待を除いた **実質金利** の代表値。
- **更新頻度**: 営業日ベース（米国時間 16:00 ET 直後に当日値が公開）。
- **stale 閾値の推奨**: 5 暦日（DGS10 と同等）。
- **ICT Bias レポートでの利得**:
  - **XAUUSD の最有力ドライバー**。ICT で言う「Macro Liquidity / Premium-Discount」のマクロ環境判定に直結。
  - 実質金利低下（DFII10 ↓）→ ゴールドの opportunity cost 低下 → bull bias 強化。
  - 実質金利上昇（DFII10 ↑）→ ゴールドが現金 / 短期債に劣後 → bear bias 強化。
  - **DGS10 と DFII10 の差 = T10YIE（breakeven）** なので、`T10YIE` と同時採用で内部整合チェックが可能。
- **採用判断**: **強く推奨**。XAUUSD 主軸の本プロジェクトでは最優先で追加する価値あり。
- **想定実装**: `SERIES_CONFIG` に `"DFII10": {"label": "US 10Y TIPS yield (real)", "stale_days": 5}` を追加。

### 2-2. T10YIE — 10Y Breakeven Inflation Rate【優先度 A】

- **意味**: `DGS10 - DFII10` = 名目 10Y 利回り − 10Y 実質金利 = 10 年先までのインフレ期待。
- **更新頻度**: 営業日ベース（DGS10 / DFII10 の派生値）。
- **stale 閾値の推奨**: 5 暦日。
- **ICT Bias レポートでの利得**:
  - **インフレ期待の市場ベース指標**。CPI が月 1 回しか出ないのに対し、T10YIE は毎営業日更新。
  - 上昇 → インフレヘッジ需要（XAUUSD bull / BTC bull）/ Fed タカ派観測（USD bull）の **両方** を含む → 単独では方向判定不能、DXY / SPX の動きと組み合わせて読む。
  - **Deep Bias の S6-X（中期方向）セクションで「インフレ期待 vs 実質金利」マトリクスを作る** 用途に直結。
- **採用判断**: **強く推奨**。DFII10 とセットで実装が自然。
- **想定実装**: `"T10YIE": {"label": "10Y Breakeven Inflation", "stale_days": 5}`。

### 2-3. SOFR — Secured Overnight Financing Rate【優先度 B】

- **意味**: 米国債担保レポ市場のオーバーナイト金利（LIBOR の後継）。短期 USD funding の実勢。
- **更新頻度**: 営業日（NY Fed が前日分を翌営業日 08:00 ET に公開）。
- **stale 閾値の推奨**: 5 暦日。
- **ICT Bias レポートでの利得**:
  - **短期流動性ストレスの早期指標**。9 月期末 / 12 月期末 / 四半期末に SOFR が急騰すると、リスクオフ → USD bull → BTC / XAUUSD bear の前兆になる。
  - 通常時は Fed funds の上限近辺で安定。スパイクは年数回しかないが、起きたときの示唆は強い。
  - `macro_liquidity.py` の WALCL / RRP / TGA Net Liquidity と組み合わせて読むと精度向上。
- **採用判断**: **推奨**。ただしスパイク時しか活きないため、`note` フィールドで「平時は Fed funds 近辺で意味なし、スパイク時のみ警戒」と明示する設計を推奨。
- **想定実装**: `"SOFR": {"label": "Secured Overnight Financing Rate", "stale_days": 5}`。

### 2-4. DFF — Effective Federal Funds Rate【優先度 C】

- **意味**: NY Fed が **日次集計** する Federal Funds の実効レート。FOMC 決定の **実勢** を反映。
- **更新頻度**: 営業日。
- **stale 閾値の推奨**: 5 暦日。
- **ICT Bias レポートでの利得**:
  - FOMC 決定値（target range）と DFF（実勢）の **差分** が市場のストレス指標になる。通常は range の中央付近で安定。
  - SOFR との差分も意味あり（DFF − SOFR が拡大 → レポ市場のストレス）。
  - ただし **平時はほぼ FOMC 決定値そのもの** で動かないため、毎日のレポートで毎回出す価値はやや薄い。
- **採用判断**: **条件付き**。SOFR を採用するなら DFF も同時採用すると整合性チェックが効く。SOFR を採用しないなら DFF も不要。
- **想定実装**: `"DFF": {"label": "Effective Federal Funds Rate (daily)", "stale_days": 5}`。

### 2-5. FEDFUNDS — Federal Funds Effective Rate（月次）【優先度 D — 重複】

- **意味**: DFF の月次集計。
- **更新頻度**: 月次（前月分を翌月 1〜2 営業日後に公開）。
- **ICT Bias レポートでの利得**:
  - **DFF を採用するなら追加価値ゼロ**。月次粒度では日次レポートとミスマッチ。
  - 「過去 N ヶ月の Fed funds 推移を時系列で見たい」場合のみ意味あり（本プロジェクトの Daily / Weekly レポートではこの用途なし）。
- **採用判断**: **不採用推奨**。`DFF` を採用すれば不要。

---

## 3. 横断的な判断ポイント

### 3-1. 既存 3 系列との関係
- 現状: DGS10（10Y nominal）/ DGS2（2Y nominal）/ DTWEXBGS（Broad USD Index, weekly）。
- DFII10 + T10YIE を追加すると、**DGS10 = DFII10 + T10YIE** の恒等式が成立し、内部整合チェックに使える。
- DGS10 - DGS2（=2s10s スプレッド）は既に `scrapers/rate_spreads.py` で扱っているため、本書では FRED 直接の系列追加には含めない。

### 3-2. レポート上の置き場所（採用時の案）
- Daily / Weekly 速報: DFII10 / T10YIE を **DGS10 / DGS2 と同じセクション** に並べる。
- Deep Bias: SOFR / DFF を S5-X（流動性ストレス）または S6-X（中期方向）の新サブセクションに配置。
- master_prompt.md / master_prompt_weekly.md / master_prompt_deep.md への反映は採用時に併せて実施。

### 3-3. API call コストへの影響
- FRED API は無料・rate-limit 緩い（120 req/min 程度）。
- 系列追加は実質コスト増ゼロ。`fetch_fred_data()` の `SERIES_IDS` リストに追加するだけ。
- 並列実行（既存実装）でレスポンスタイムも増えない（max 5 系列でも 1〜2 秒）。

### 3-4. レポート長への影響
- 全 4 系列追加（DFII10 + T10YIE + SOFR + DFF）で、Daily レポートに 4 行 ×（値 / 前日比 / as_of）= 12 行程度の追加。
- 速報の 1500〜2000 字制約には収まる。Deep Bias は字数制約緩く影響小。

### 3-5. stale ハンドリング
- 全系列とも DGS10 と同じく `stale_days=5` で十分。
- T10YIE は派生値だが FRED 側で独立に publish されるため、stale 判定も独立で OK。

---

## 4. 採用判断の推奨パターン

| パターン | 追加系列 | 想定運用 |
|---|---|---|
| **最小限**（推奨） | DFII10 + T10YIE | XAUUSD bias の精度向上のみ。実装コスト最小、レポート長への影響軽微。 |
| **標準** | DFII10 + T10YIE + SOFR | 上記 + 短期流動性ストレス監視。Deep Bias の S5-X 強化。 |
| **最大** | DFII10 + T10YIE + SOFR + DFF | 上記 + Fed funds 実勢追跡。SOFR と DFF を組み合わせてレポ市場のストレス検知。 |

**机上判定**: **「最小限」が最もコスト対効果が高い**。XAUUSD 主軸である本プロジェクトでは DFII10 + T10YIE の 2 系列追加が最も投資対効果が大きく、レポート長と保守工数も最小。SOFR / DFF は Deep Bias が安定運用に乗ってから検討で十分。

---

## 5. 不明点（採用前に実機で確認すべき項目）

| 項目 | 確認方法 |
|---|---|
| DFII10 / T10YIE の publish 時刻が日本時間で何時か | FRED API を 1 週間ポーリングして `realtime_end` を観測 |
| 連休跨ぎでの stale 判定の挙動 | 既存 DGS10 と同じく `stale_days=5` で問題ないかを月跨ぎで確認 |
| SOFR がスパイクした履歴（2024 〜 2026） | FRED 上のヒストリを目視 + Brain のマーケットノートと突合（本書では Brain 参照禁止のため省略） |
| DFF と FOMC target range の差分の典型値 | FRED の `DFEDTARU` / `DFEDTARL` と並べて確認 |

---

## 6. 参照リンク（一次情報）

- https://fred.stlouisfed.org/series/DFII10 — 10Y TIPS yield
- https://fred.stlouisfed.org/series/T10YIE — 10Y Breakeven Inflation Rate
- https://fred.stlouisfed.org/series/SOFR — Secured Overnight Financing Rate
- https://fred.stlouisfed.org/series/DFF — Effective Federal Funds Rate (daily)
- https://fred.stlouisfed.org/series/FEDFUNDS — Federal Funds Effective Rate (monthly)
- https://fred.stlouisfed.org/docs/api/fred/series_observations.html — FRED API ドキュメント
- 関連: [`scrapers/fred.py`](../scrapers/fred.py) — 既存 FRED 実装
- 関連: [`MODERNIZATION_RESEARCH.md`](../MODERNIZATION_RESEARCH.md) — FRED 採用経緯
