# チャート外分析の根拠照合

2026-09-22。Jevは、要約が引用元の本文を正確に伝えているかを補助点検します。本文の事実と公開原資料の照合、数値・日付・鮮度の検査、最終レビューは引き続き親とコードが担当します。

## 処理の分担

1. 収集時に、FedWatchは金利レンジ別の確率を保持します。最大確率のレンジを現行政策金利と推定しません。政策変更の分類は、Fed公式の現行金利を別途照合した後です。
2. 価格のNaN・無限大・真偽値・不正文字列を除外します。現在値または前日終値が壊れた銘柄は、整形済み価格欄ごと分析入力から除き、元データは保持します。
3. COTは取得時刻と観測日を別に保存します。欠損・未来・14暦日超の観測は現在判断から除外し、銘柄別日付と比較日を表示します。14日は運用上の保守的な許容上限であり、最新データ取得の保証ではありません。通常は火曜時点のデータが金曜に公表され、休日には遅延があります。[CFTC公表日程](https://www.cftc.gov/MarketReports/CommitmentsofTraders/ReleaseSchedule/index.htm)
4. 要約の `source_quote` が本文中に実在するかコードで照合します。空欄・不存在は拒否します。完全同文はAPIへ送りません。
5. 明示的にpublicとした要約だけ、Jevが `supports / contradicts / insufficient` を返します。confidenceと選択肢確率が両方0.90以上の場合にのみ型付きの補助結果とし、未達は `uncertain` です。
6. 親が未判定・指摘を原文へ戻って確認します。監査JSONのSHA-256と確認内容を親レビューへ記録し、本文・要約・監査が変わったら再検証します。

Jevの `supports` は「引用が要約を支える」という意味だけです。原資料の真偽、市場予測の的中率、売買の許可、レポート完成を意味しません。API成功・コード照合だけでは合格にしません。

## 現行ジョブで使う

`/Users/laa/.codex/jobs/chart-intel/PARENT-WORKFLOW.md` の作成手順に従います。新しい `package.json` に `summary_data_class: "public"` を付ける前に、要約のtextと引用に非公開情報がないことを親が確認します。省略または `local` なら外部送信しません。

`--parent-package` は `.semantic-audit.json` を版に保存し、そのハッシュをbundleへ結び付けます。`--parent-review` では次が必要です。

```json
{"semanticAudit":{"sha256":"監査JSONの実ファイルSHA-256","reviewed":true,"notes":"指摘と未判定を原文に戻って確認した内容"}}
```

認証は既存1Passwordラッパー、通信と予算は導入済み `typesafe-ai/scripts/jev_filter.py` を再利用します。別の秘密値、SDK、予算台帳、常駐処理は追加しません。1回最大64項目、API通信8秒、認証を含む補助処理25秒、共有上限はUTC日ごとにUSD 0.01です。予算不足や障害は未判定として残します。timeoutの未確定利用分は台帳の予約を保持します。

```sh
LAA_JEV_DISABLED=1 /Users/laa/.codex/jobs/chart-intel/run.sh daily --parent-package /absolute/path/package.json
```

この指定で即時にコード照合へ戻ります。定期タスクのモデル・日時・売買ルール・自動配信の停止状態は変更しません。

## 2026-09-22の評価

実装を見ずに別担当が作った人工16ケース（日本語8・英語8）を、期待ラベルを含めずAPIへ送りました。否定反転、条件欠落、予想と実績、観測時点、標本の一般化、因果、数値と単位を含みます。

| 指標 | 実測 |
|---|---:|
| 素の分類の一致 | 15 / 16 |
| 閾値以上の補助結果 | 6 / 16 |
| 未判定として親に残した件数 | 10 / 16 |
| 支持されない主張を閾値以上でsupportsとした件数 | 0 / 12 |
| API通信時間 | 705.575 ms |
| API入力 / 出力tokens | 4,299 / 735 |
| 公示単価による費用見積り | USD 0.000180558 |

誤分類1件は、標本を全市場へ一般化した主張をinsufficientではなくcontradictsとしたものです。confidence 0.58で未判定に残りました。閾値はこの結果に合わせて下げていません。小さな人工標本の結果であり、実市場の正確性・利益・Codex利用枠の改善は未測定です。

既存の過去レポートでも別途動作確認し、引用付き5項目のうち3項目をJevで照合、2項目は完全同文としてコードだけで処理しました。現在の相場分析として再発行したものではありません。

- [独立ケース](../tests/fixtures/jev_audit_cases.json)
- [評価の実測JSON](evaluations/jev-summary-20260922.json)
- [TypeSafe HTTP API](https://docs.typesafe.ai/api)
- [TypeSafeの引用検査例](https://docs.typesafe.ai/cookbooks/citation_check)

現在の実装・テスト結果・配備確認・復元方法は [SESSION-HANDOFF.md](SESSION-HANDOFF.md) を参照してください。
