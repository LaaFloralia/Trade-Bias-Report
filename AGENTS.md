# fundamental-macro-analysis — Codex Rules

社長呼称「チャート外分析」。リテールセンチメント、経済指標、FedWatch、ETFフロー、COT 等を収集し、Daily / Weekly Bias の Markdown、JSON、PDF を生成する。

## Runtime

- Python 3.12 以上、依存管理は `uv`
- 現行入口は `/Users/laa/.codex/jobs/chart-intel/run.sh daily|weekly`。本文・機械JSON・最終レビューは現在のCodex親タスクが担当する
- 同入口は本repoのコミット済みコードを専用runtimeへsnapshotする。未コミット修正だけでは定期処理へ反映されない
- 手順は `/Users/laa/.codex/jobs/chart-intel/PARENT-WORKFLOW.md`、再開時は [引き継ぎ](docs/SESSION-HANDOFF.md) を読む
- Claude/Hermes/旧Routinesは休止資産。`scripts/intel.py brief` など旧一気通貫入口を現行ジョブの代わりに起動しない
- Jevは公開市場資料の要約と根拠文の照合補助に限定する。固定の数値・日付・鮮度検査と親の受入判定を代替しない

## Safety and Data

- `config.yaml` を銘柄設定の正本とする
- 実データと時刻を確認し、欠損値を推測で埋めない
- トレード判断では観測、モデル推論、無効化条件を分ける
- 現行ジョブはBrain参照・書込と自動配信を停止中。今回の改善で保存先・公開権限・売買ルールを拡大しない
- API key は 1Password ラッパーから注入し、表示・保存しない

## Validation

- `uv run pytest`
- レポート変更時は Markdown の情報保存、実際の出力形式（HTMLのPC/スマホ、PDFを作成した場合は全ページ）を確認する
- スクレイパー変更時は mock test に加え、許可された範囲で対象ソースの実取得を確認
- wrapper変更時は `bash -n` と現行runnerの `--help` / `--dry-run` を確認する

旧 `.claude/commands/` と README 内の Routines 記述は履歴・互換資料として残す。現行実行経路は本ファイルを優先する。
