# fundamental-macro-analysis — Codex Rules

社長呼称「チャート外分析」。リテールセンチメント、経済指標、FedWatch、ETFフロー、COT 等を収集し、Daily / Weekly Bias の Markdown、JSON、PDF を生成する。

## Runtime

- Python 3.12 以上、依存管理は `uv`
- LLM の既定は `INTEL_ENGINE=claude`、Claude Code CLI のサブスク枠を使う
- Daily / Weekly / Quick は Claude Opus 5、effort `high` に固定する
- Hermes cron は `/Users/laa/.hermes/scripts/intel-daily.sh` と `intel-weekly.sh` から起動する
- `INTEL_ENGINE=codex` は比較検証用に残すが、定時運用の既定にしない

## Safety and Data

- `config.yaml` を銘柄設定の正本とする
- 実データと時刻を確認し、欠損値を推測で埋めない
- トレード判断では観測、モデル推論、無効化条件を分ける
- Brain への保存は既存パイプラインの許可範囲だけ。`Brain/Atlas/` は変更しない
- API key は 1Password ラッパーから注入し、表示・保存しない

## Validation

- `uv run pytest`
- レポート変更時は Markdown の情報保存テストと PDF 全ページの目視確認
- スクレイパー変更時は mock test に加え、許可された範囲で対象ソースの実取得を確認
- cron wrapper 変更時は `bash -n` と `intel.py --help` を確認する

旧 `.claude/commands/` と README 内の Routines 記述は履歴・互換資料として残す。現行実行経路は本ファイルを優先する。
