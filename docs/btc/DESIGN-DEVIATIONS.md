# BTCUSD chart-intel: deviations from the design

This page lists only the places where the implementation differs from the design (including the P1–P9 decisions)
or narrows it.

## Data and sources

- **History store:** no SQLite. History is append-only JSONL under `history/`.
- **Rate limiting:** no per-host limiter. Sources run in parallel inside one budget, with at most one retry per
  request; 401, 403, 404 and 429 are never retried.
- **Not collected:** X posts, IBIT holdings and liquidation feeds (B7). CoinGlass is "not_applicable" (paid,
  model-based). Coinbase is not used because of its terms.
- **OKX open interest:** the 24-hour change comes from our own history, so it reads "history insufficient" until a
  24-hour-old observation exists.
- **ETF days:** a Farside row dated after the last completed US session (today's row while the session runs) is
  excluded from the latest day and from the 5-day sum. These rows are listed in `state.etf.in_progress_rows`.
  - US trading days come from a static NYSE full-day holiday table (`btc/us_calendar.py`, 2026–2028, verified
    2026-10-08). Beyond its coverage end the trading calendar is unknown (ETF status `unknown`), never plain weekdays.
    Extend the table before 2028-12-31.
  - Lag, all-columns-numeric and total reconciliation are tracked separately (`state.etf.expected_day`). The ETF
    bundle counts only when the expected session's row is complete and reconciled; `incomplete`, `lagged` and
    `stale` days do not count toward normal coverage.
  - A provider total mismatch keeps both values (`etf_reported_total_usd` and the known partial sum, status
    `conflict`), scores neither and is the hard invalid `etf_total_mismatch` (data_hold).
  - The 5-day sum covers the 5 NYSE trading days ending at the latest row; every one must be complete and
    reconciled, and a gap is never filled with an older row.
  - Lagged fallback (design 5, 7.3): when the expected session's row is present but not all-numeric (and not a
    conflict), the 5-day sum uses the 5 trading days ending at the previous trading day if all of them are
    reconciled. The ETF status is `lagged` (lag 1, `state.etf.lagged_fallback`), the dates are printed, the bundle is
    not counted, and the expected day stays shown as incomplete. Any gap in that window keeps the sum missing.
- **Macro group:** it needs a recent common date for the 2-year yield and DTWEXBGS. DTWEXBGS is a weekly H.10 series
  with a lag, so the group is often "unknown" (`common_observation_stale`).
- **Calendar:**
  - FOMC times are tentative: the date comes from the Fed page, the 14:00 ET time is convention.
  - The BLS schedule is a parent-verified cache, valid until its `coverage_end`.
  - Every source fails closed (`state.calendar.sources`): the record must be fully parsed (`ok`), verified at or
    before `as_of` (fetched pages within 24 h), and its `coverage_end` must reach the end of the next 24 h (New York
    date). The Fed `coverage_end` is Dec 31 of the latest year the page lists (capped at the 120-day parse window);
    the BEA one is its latest listed release. Only parsed structure sets `coverage_end`. Candidates are captured
    before strict parsing: in a Fed year section any token that starts with a month name (in range), and on the BEA
    page any row with a GDP/PCE title in any cell. A candidate that does not parse exactly (one-token "October 6 –
    7*", invalid days, a short BEA row, hour outside 1–12 or minute outside 00–59) makes the source `partial`, so
    the next 24 h become unknown. Fed entries "D (notation vote)" / "D (unscheduled)" are not scheduled meetings
    and are excluded explicitly (`excluded_non_meetings`); BEA "To Be Announced" rows are counted separately.
  - CME expiry is "unconfirmed" unless an official per-contract calendar is available (B8).
- **News:**
  - Clustering uses a hash of the normalised title, so the same story from different publishers forms separate
    clusters.
  - Only official bodies count toward the BTC-specific event group, which limits double counting.
  - Body checks are capped at 8 articles and 90 s.
  - Known events and unresolved incidents are carried in the job's `history/` (`btc-known-news.json`,
    `btc-incidents.json`), written only when an edition passes parent review. Matching is by URL, code cluster,
    primary body hash or the parent's `known_event_ids` link; a headline rewrite with a new body and no link is not
    detectable by the code. Known events count from `first_known_at` (no extension); `follow_up_new_facts` needs a
    new primary body published after the event became known. Matching retention (180 days, at most 5000 records,
    oldest dropped) is separate from the scoring window; the parent briefing lists only the last 14 days. A
    primary-body match wins over a URL match, which wins over a cluster/link match; ties go to the latest
    `first_known_at`. A follow-up needs a body unknown to every retained record. An adopted follow-up is its own
    record (`follow_up_of`) counted from its own publication.
  - Both files are validated strictly on read; an invalid file gives `carry_state_invalid` (data_hold), acceptance
    skips carry writes and the file is never overwritten.
  - An incident is released only by an `incident_recoveries` entry (official primary body after the incident and
    facts for every `affected_source_ids` source observed on the exchange after the recovery notice; derived facts
    need every input to pass, date-only facts never count). An incident without `affected_source_ids` cannot be
    released by the parent; only the owner's manual release (`btc.carry release`) clears it. A closed incident
    never re-opens from the identical publication; `incident_id` hashes the code cluster, URL and `published_at`,
    so an updated or re-dated article with the same title and URL is a new incident (fail closed). This identity
    changed before production had any `btc-incidents.json`, so no migration exists.
    Closures (`released_at` / `released_as_of`) and the 14-day display are evaluated as of the edition's `as_of`; a
    release made while an edition is being built takes effect from the next edition. `released_by` is an operator assertion, not authentication; each manual
    release stays visible in every edition (MD/HTML section 7 and the parent briefing) for 14 days.
  - Reactions use contiguous, deduplicated, closed 1 m bars per segment; a segment with a gap is not computed
    (status `partial`/`missing`). The bars used are kept in the news record with the raw hash.

## Facts and scoring

- **Field names:** facts carry both `display` (printed text) and `display_value` (the rounded number used in figure
  bindings).
- **Percentiles:** the display is short (`百分位 X`); the window and n are in `coverage.note`, and prose prints both.
- **Walls and strikes:** the display is the centre price; the 25 bp band, hits and OI share are in the note.
- **Wall detection:**
  - Walls need book depth reaching at least 100 bp on that side.
  - The 25 bp bucket that holds the best quote is excluded (it is always thick).
- **Funding percentiles:** need at least 42 settled observations.
- **Scorable facts:** only `ok`/`provisional`, non-stale, valued facts enter scoring and bundles. Stale, partial
  and conflict facts are displayed but are unknown for scoring.
- **Open interest:** USDT-margined OI is kept as native USDT notional (`oi_notional_usdt`). `oi_usd` exists only
  when a fresh Kraken USDTUSD mid is available (mark price and rate in `source_fact_ids`); otherwise it is missing.
  The OI-weighted funding uses the USDT notional as weights.
- **Settled funding (B3):** comes from Binance and Bybit `fundingRate` history and OKX realized rates. Current or
  predicted funding is shown separately and is never scored.
- **Freshness (B5):** the code checks freshness at collection time. At finalize it lists the values that have expired.
  - Expired values do not change the score.
  - If they back the destination, the destination becomes none (失効).
  - Book walls are valid for about 60–90 s and option OI for 15 min, so a book-based destination is normally expired
    when the parent finishes.

## Outputs

- **Timestamps:**
  - `as_of` is the collection completion time.
  - `available_at` equals `generated_at`.
  - Machine times are UTC `Z`.
  - `data_as_of` is the JST date.
- **Two-phase collection:** context sources run first and quotes and books last, to keep books fresh. As a result,
  `retrieval_started_at` and `retrieved_at` differ per source (B1).
- **Markdown safety:**
  - External strings (headlines, excerpts, labels, event names) are neutralised to full-width look-alikes when they
    enter the facts.
  - Parent text containing markup characters is rejected.
  - Non-http(s) feed links are dropped.
- **Missing values:** they print a Japanese reason label; the reason code stays in `missing_reason` and in the
  section 10 fact table.
- **Dev browser:** dev tests may use `BTC_PLAYWRIGHT_CHANNEL=chrome`. Production strips it and uses the bundled
  Chromium (the render record says `browser: playwright-chromium`).

## Security additions not in the original design

These are described in `docs/btc/README.md`:
- the SSRF guard with pinned addresses;
- the per-feed host allow-list for article bodies;
- the Deribit instrument pattern check;
- the exact sandbox write allow-list (no op temp dir, no Jev ledger) and the read restrictions under `/Users/laa`;
- the credential split: op's token is removed before `sandbox-exec`, every worker refuses `OP_*` variables, and the
  Jev headline triage runs in the entry outside the sandbox (P4 is unchanged: same script, constants and shared
  daily budget as XAU).

## Collection phases

The design has one collection process. The run now has three steps inside the same 540 s budget:
1. The collect worker gathers context sources and headlines (310 s budget).
2. The entry runs the triage.
3. The news-detail worker handles selection, article bodies and reactions (up to 120 s), then quotes and books
   (up to 140 s).

`as_of` is still the completion of the books, and `collection.json` keeps the same shape. It now has three phase
records: `context`, `news_detail` and `quotes_and_books`. News reactions still use the collection start as "now".
