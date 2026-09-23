# チャート外分析の有用性レビュー — 検証スクリプトと結果

2026-09-23。結論と判断事項は [Brain/Inbox のレビュー](/Users/laa/Brain/Inbox/2026-09-23-チャート外分析の有用性とJev活用.md) にまとめ、ここには再現用のスクリプトと数値を置きます。売買シグナルや手法の合格判定ではありません。

| ファイル | 内容 |
|---|---|
| `fetch_prices.py` | Twelve Data の XAU/USD 1時間足・日足、FRED の DFII10・DTWEXBGS・DGS10 を取得 |
| `fetch_events.py` | FRED の発表日程（雇用統計 release 50、CPI release 10）と FRB の FOMC 日程ページを取得 |
| `evaluate.py` → `summary.json`, `bias_rows.csv` | 過去 Daily 25 件のバイアスとその後の値動き、金と実質金利・ドルの年別関係。標準ライブラリのみ |
| `event_study.py` → `event_study.json` | 指標発表の時間帯の値幅・スプレッド（Dukascopy H1、2021-06〜2026-06）と、xau-strategy-lab の price_only 取引の指標日成績 |
| `build_literature_records.py` ほか → `jev_literature_filter.json` | 論文11本を Jev で選別したときの削減量・取りこぼし・費用。本文は著作物のため保存しない |

## 再現手順

```sh
cd /Users/laa/dev/fundamental-macro-analysis
DIR=/absolute/work/offchart && mkdir -p "$DIR/data"
./scripts/run-with-secrets.sh --batch python3 docs/evaluations/offchart-review-20260923/fetch_prices.py "$DIR/data"
./scripts/run-with-secrets.sh --batch python3 docs/evaluations/offchart-review-20260923/fetch_events.py "$DIR/data"
python3 docs/evaluations/offchart-review-20260923/evaluate.py "$DIR/data" "$DIR"
/Users/laa/dev/xau-strategy-lab/.venv/bin/python docs/evaluations/offchart-review-20260923/event_study.py "$DIR"
```

## 前提と限界

- バイアスの記録は `output/intel/intel_daily_*.json`（6/11〜9/4）と chart-intel の 9/9〜9/11 版。6/11・6/12 は発行時刻がなく、ファイル更新時刻で代用した。
- Twelve Data の1時間足・日足には市場休止中の土日バーがあるため、評価では除外した。本体の日足処理も同日に修正した（e4937bb）。
- 指標日程は実際の発表日の一覧で、取引判断時点に取得した予定（PIT）ではない。xau-strategy-lab のニュース条件の合格判定には使えない診断値。
- 取引の成績は各 run の仮定費用に基づく。v6 は 2024・2025 年の base/price_only を8方式まとめ、同一エントリーを重複除去した。
