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
  - US holidays are not modelled.
  - When the expected day's row is still incomplete, the 5-day sum is missing ("5営業日がそろわない").
- **Macro group:** it needs a recent common date for the 2-year yield and DTWEXBGS. DTWEXBGS is a weekly H.10 series
  with a lag, so the group is often "unknown" (`common_observation_stale`).
- **Calendar:**
  - FOMC times are tentative: the date comes from the Fed page, the 14:00 ET time is convention.
  - The BLS schedule is a parent-verified cache, valid until its `coverage_end`.
  - CME expiry is "unconfirmed" unless an official per-contract calendar is available (B8).
- **News:**
  - Clustering uses a hash of the normalised title, so the same story from different publishers forms separate
    clusters.
  - Only official bodies count toward the BTC-specific event group, which limits double counting.
  - Body checks are capped at 8 articles and 90 s.

## Facts and scoring

- **Field names:** facts carry both `display` (printed text) and `display_value` (the rounded number used in figure
  bindings).
- **Percentiles:** the display is short (`百分位 X`); the window and n are in `coverage.note`, and prose prints both.
- **Walls and strikes:** the display is the centre price; the 25 bp band, hits and OI share are in the note.
- **Wall detection:**
  - Walls need book depth reaching at least 100 bp on that side.
  - The 25 bp bucket that holds the best quote is excluded (it is always thick).
- **Funding percentiles:** need at least 42 settled observations.
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
