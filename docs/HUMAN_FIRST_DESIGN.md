# Human-First レンダラ設計記録（認知負荷対策）

2026-08-17 導入。`scripts/human_report.py` + `templates/style_human.css` の設計判断を記録する。
汎用の設計原則は `~/.claude/skills/human-first-docs/SKILL.md`（全プロジェクト共通スキル）を正とし、
本ファイルはこのリポジトリ固有の実装と、汎用原則からの意図的な逸脱のみを扱う。

## 構成

```
MD（AI が読む正本、Brain に保存）
  └→ scripts/human_report.py   … 構造抽出 + ダッシュボード HTML 生成
      └→ scripts/render_report.py … 既定で human レンダラ、失敗時 legacy へ自動フォールバック
          └→ Playwright print → PDF（output/ + Google Drive）
```

- 3 層構造: **1 ページ目 = 意思決定に必要な全情報**（判定 / 信頼度 / リスク / 価格レベルマップ /
  条件付きシナリオ / 待ちの妥当性）→ 2〜3 ページ = 根拠パネル → 以降 = 全文詳細。
- 抽出はすべて Optional。LLM 生成の揺れでパターンが取れなくてもウィジェットを描かないだけで、
  全文詳細は常に残るため情報は欠落しない。レンダラ全体が例外を出したら legacy テンプレへフォールバック。
- **確定訂正の解決**: セクション0 の初期値と自己検証セクションの確定値（Daily「確定スコアは N」/
  Weekly「訂正後セクション0-1: …」）が食い違う場合、ダッシュボードは必ず確定値を表示し、
  訂正があったことをコールアウトで明示する。旧レンダラは初期値をそのまま表紙に出しており誤読リスクがあった。

## 図（インライン SVG、外部ライブラリなし）

| 図 | 関数 | 内容 |
|---|---|---|
| 価格レベルマップ | `svg_price_ladder` | 現値・BSL/SSL 帯・注目ゾーン・Draw・無効化・PWH/PWL を縦軸配置。ラベルは衝突回避付き |
| スコアゲージ | `svg_score_gauge` | 統一スコア −8〜+8、閾値バンド（Low/Med-cautious/Med/High）+ 現在値マーカー |
| リテール比率バー | `svg_share_bar` | Short/Long の 100% バー + 平均建値・含み損益 |
| FedWatch 確率バー | `svg_prob_bar` | レートレンジ確率のスタックバー（青系順序ランプ） |

## 配色（dataviz validate_palette.js 全項目 PASS）

| 役割 | HEX | 備考 |
|---|---|---|
| Bullish / Long / +1 | `#047857` | ▲ を必ず併記 |
| Bearish / Short / −1 | `#c22f2f` | ▼ を必ず併記 |
| 注意 / 様子見 / Med | `#d97706` | ◆ を併記 |
| ブランド / 構造 | `#2547a8` | 見出し・パネル枠 |

検証結果: 白背景で Lightness band / Chroma floor / CVD separation（deutan ΔE 8.8）/
Normal-vision floor / Contrast すべて PASS。方向は常に「色 + 記号 + 語」で符号化し色単独に意味を持たせない。

## 汎用原則からの意図的な逸脱（2026-08-17 リサーチ結果に対する判断）

1. **緑/赤を維持**（リサーチ推奨は Okabe-Ito 青/橙）。社長は TradingView のグローバル慣習
   （緑=上昇/赤=下落）で日常トレードしており、慣習と逆の色は誤読リスクの方が大きい。
   CVD 安全性は上記の検証済みパレット + 記号併記で担保。
2. **Hiragino Sans を維持**（リサーチ推奨は Noto Sans JP / BIZ UD）。本 PDF は配布物ではなく
   社長本人の閲覧用で、Hiragino 単独指定は PDF サイズ最適化の既存決定（style.css の経緯）を引き継ぐ。
   外部配布する資料を作る場合はスキル側の推奨（Noto / BIZ UD + url() 指定）に従うこと。

## 運用

- 既定: human レンダラ。`--legacy` フラグまたは環境変数 `REPORT_RENDERER=legacy` で旧テンプレへ即時退避可能
  （cron 緊急時のロールバック手段）。
- テスト: `tests/test_human_report.py`（パーサ / SVG / build_html）+ `tests/test_deep_bias.py`（両レンダラの回帰）。
- レイアウトは日ごとに変えない（位置記憶で認知負荷が下がる）。ウィジェットの追加・並べ替えは
  このファイルに理由を追記してから行う。
