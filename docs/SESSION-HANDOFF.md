# Session handoff

Updated: 2026-09-23 (JST)

## Purpose and completed scope

Improve the chart-intel Jev-assisted analysis path by keeping incomplete FedWatch observations explicit through collection, numeric facts, generated input, and HTML. Also fail closed when Twelve Data supplies invalid or internally inconsistent price fields.

The Investing.com parser now accepts only anchored, four-cell target-rate rows. It ignores bare range headings and unrelated rows such as VIX. Blank, dash, and N/A probability cells are retained as unavailable values rather than converted to zero. Probabilities outside 0–100, malformed rows, invalid meeting dates, and invalid ranges fail closed. A partial distribution remains partial even when known rows sum to 100%; it is excluded from FedWatch history, includes each missing range in analysis input, and does not render as a complete probability bar. This also works when every current probability is missing.

The numeric-facts layer retains unavailable ranges and marks partial distributions without inferring a full total or policy-direction probability. The chart-intel job source also includes regression coverage for partial, subtotal-only, and all-current-missing cases. Jev remains an auxiliary quote-to-summary checker; typed output does not establish source truth, report acceptance, or trading authority.

Twelve Data validation now checks that `change` is finite, OHLC values are positive and finite, and the daily high/low contain open/close. If a symbol has an invalid price field, its full price section is excluded from model input and the warning omits the raw bad value; other symbols remain available.

## Changes and verification

Implementation commit: `1f5979551830973ec7b80ac168e6be41a245d70e` on branch `codex/laa-atelier-reports-20260909`.

- Repo tests: `uv run pytest -q` — 407 passed.
- Chart-intel workflow: `test_parent_workflow.py` and `test_jev_integration.py` — 31 passed.
- Python compilation and `git diff --check` passed.
- The committed source was copied into the dedicated runtime under the existing execution lock. All 53 snapshot files matched their recorded SHA-256 values, and `scripts/jev_report_audit.py` is present.
- One live public Investing.com fetch at `2026-09-22T21:51:04Z` returned the Oct 28, 2026 meeting with two known current rows and one unavailable current range. The known subtotal was 100%, but the observation stayed partial; VIX was excluded, and the formatted facts and HTML retained the incomplete status. This fetch check did not generate a daily report or invoke Jev.
- Privacy-safe fetch evidence: `/Users/laa/.codex/work/chart-intel-20260922/live-fedwatch-fix-20260923.json`.
- Independent review found no blocking issue and replayed the all-current-missing path and patch ordering.

## Runtime patch boundary

The job directory `/Users/laa/.codex/jobs/chart-intel` is outside Git. Its `numeric_validation.py` and `test_parent_workflow.py` were updated for partial FedWatch data. `docs/runtime-patches/20260923-fedwatch-partial.patch` is an incremental zero-context test patch based on the verified post-Jev preimage hash; apply it only after the Jev patch and only with `git apply --unidiff-zero`. The companion manifest records preimage, postimage, and patch hashes, and sequential application was tested.

The original `numeric_validation.py` preimage was not retained. `docs/runtime-patches/20260923-fedwatch-partial/numeric_validation.py` is therefore an exact after-state snapshot for manual review, not a reversible source diff. Its current SHA-256 matches the job source. Do not claim a proven rollback to the unretained preimage.

## Jev review boundary and next step

`semanticAudit.reviewed` records that the parent inspected the hash-bound semantic audit; it is not a Jev pass. The written workflow requires unresolved or unassessed pairs to be checked against source material, while the current machine check only requires non-empty notes and blocks deterministic quote mismatches. This leaves semantic sufficiency as a human review responsibility. No Jev acceptance rule was changed here; local parent review still does not enable publication, and publication remains disabled.

No report-generation run, external publication, schedule change, Brain access, or trade action was performed. The implementation and handoff are tracked on the branch above; the next scheduled invocation will use the committed runtime snapshot and retain the existing publication boundary.

## 2026-09-24: chart-external inputs (liquidity, positioning, macro surprise)

Scope confirmed by the owner: this report covers only information not visible on the chart — order concentration (liquidity), positioning ratios, and macro/rates/geopolitical bias. Design: Brain/Inbox `2026-09-24-チャート外分析ベストプラクティス.md`.

- `4602a6b` Investing.com actual column is now stored; released events in the last 36h get `actual − forecast` and a rule-based gold direction (US indicators only). Missing forecasts are filled from the ForexFactory weekly feed only on country/±5 min/name match. Earlier note that "all forecasts were N/A" was wrong; the missing field was the actual.
- `8750d13` Round-number levels (±2%, 50/100 USD) and an append-only positioning history (`output/history/positioning.jsonl`) with same-source percentiles; fewer than 20 samples → 判定保留. Backfill: `scripts/backfill_positioning.py`. Runtime history seeded 2026-09-24 with 45 scraped snapshots and 149 CFTC weeks (current published values, not first-release vintages).
- `55d1ca8` Wired into `collect_all_data` / `format_scraped_data`; failures keep other inputs. `fe9fe1f` master prompts: operating rules, 取得不可/方向未定/中立 distinction, coverage line, `bias` non-zero only with ≥2 aligned independent drivers.
- Verification: 421 tests; live full collection showed all three new sections. The 2026-09-24 09:00 Hermes run succeeded on snapshot `c93534f`; its Jev audit ran local-only because the parent set `summary_data_class: local`. The 18:00 run is the first to use `fe9fe1f`.
- Not done: CME QuikStrike option OI (terms of use unconfirmed), OANDA order/position book (needs account and API key).

## 2026-09-26: off-chart report redesign and data fixes

OANDA REST API needs GOLD status (owner is SILVER) and CME's terms prohibit automated QuikStrike access, so neither is used. Instead:

- `4153b41` GVZ (FRED `GVZCLS`) gives a 1-day ±1σ expected range; round numbers inside it are marked. CME option OI is explicitly not collected.
- `9c07eb5` Forecasts of unreleased events are archived at each run (`output/history/calendar_forecasts.jsonl`); surprises use the pre-release record and state its provenance. COT percentile lines state the comparison window.
- `68229f1` With Brain disabled, `report_anchor` reads this job's parent-passed editions (weekly, previous-day daily). Correlation falls back to weekday-filtered Twelve Data daily closes.
- `f4f8782` Bug: the runner re-formats model input from saved JSON, which lacks `_raw_quote_*`, so the round-number section never reached the 9/24–9/25 reports. Liquidity is now built at collection time and stored.
- `fb806e4` Daily/weekly prompts: sections keep their contract headings but now cover off-chart bias hypotheses with cancel conditions, no-trade windows, expected range and round numbers; score #3 is macro-surprise alignment, #2 uses own-history percentiles. Price structure/entries are left to the owner.
- Runtime (outside Git): runner no longer stubs the anchor, sets `REPORT_ANCHOR_FALLBACK_DIR`, and its analysis rules accept pre-release forecasts and plan A/B as off-chart hypotheses. Diffs in `docs/runtime-patches/20260926-*.patch`; originals in `.codex/jobs/chart-intel/backups/20260926-offchart/`.
- Trial run 2026-09-26 20:35 JST (Codex gpt-6-astra/high, same prompt as the scheduled daily, off-slot): `succeeded_local` / `parent_passed`, 42 images reviewed, 8/8 checks passed, snapshot `c408295`. Report now carries the off-chart bias table, GVZ range, round numbers, weekly/previous-daily anchors, restored correlation and COT percentile with window. Follow-ups from review: score item #3 now also counts a ±5pp FedWatch day-over-day shift (it was unscoreable); coverage count defined; report length 7,011 chars exceeds the 2,400–3,800 target; DXY provider change (-0.31) disagrees with close − previous close (-0.27) — investigate `scrapers/dxy.py`.
- 2026-09-26 GPT-6 Astra (max, read-only via `codex exec`) reviewed the design twice. Round 1: 15 findings (4 P1) → fixed in `174c040..abbc542`. Round 2: 2 resolved / 13 partly resolved, 6 new → fixed in `78c0968`, `3af3d63` (m/m normalization, old-JSON formatting, hourly dedupe, weekly coverage, shared direction rule, structured `方向付与 ｜ イベント停止` line, concrete re-evaluation conditions). Not adopted: capping #1+#3 (would make High unreachable under the fixed 7/5/3 thresholds); audit appendix split (renderer change). Reviews: scratchpad `astra-review*/review.md` (not in Git).
- Trials: 7,011 → 3,998 → 4,617 chars (target 2,400–3,800). Trial 3 shows the structured judgment line, per-horizon direction, a concrete re-evaluation condition and footnote URLs; machine.json `no_trade_reason` now starts with 方向付与保留/イベント停止. Remaining: DXY provider change inconsistency, individual-ratio history still <20, pre-release forecasts start accumulating next week.
