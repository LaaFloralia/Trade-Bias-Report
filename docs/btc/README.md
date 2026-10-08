# BTCUSD chart-intel (off-chart analysis)

Code: `btc/` (job, sources, facts, scoring, parent analysis, report, machine.json). Tests: `tests/btc/` (no network; the
fixture is a trimmed live collection whose news text and article URLs are synthetic). Parent procedure:
`btc/job_template/PARENT-WORKFLOW.md` (installed into the job root).

## Install and run

- Trial or dev: `python -m btc.install_job --root <dir outside the repo and /tmp> --source-repo <repo> --python <venv python>`.
- Production (parent only): `python -m btc.install_job --production`. This needs the default source repo on `master`
  and installs into `/Users/laa/.codex/jobs/chart-intel-btcusd`.
- Stages: `run.sh daily|weekly` → parent writes `analysis.json` → `run.sh <mode> --parent-package <package.json>` →
  parent review → `run.sh <mode> --parent-review <review.json>` → check `latest-<mode>.json`.
- The worker runs the committed snapshot under `sandbox-exec -f boundary.sb`. The browser check uses Playwright's
  bundled Chromium (`python -m playwright install chromium`). The runner strips `BTC_PLAYWRIGHT_CHANNEL`; that
  variable is for dev tests only.

## Operations: manual incident release (owner instruction only)

A critical incident recorded on parent acceptance (`history/btc-incidents.json`) keeps `incident_hold` until an
adopted edition carries a code-verified `incident_recoveries` entry: an official primary-body recovery notice plus,
for every `affected_source_ids` source, observations made after that notice. An incident recorded without
`affected_source_ids` cannot be released by the parent. If the official recovery never appears as a code-verified
primary body, or no affected source was named, release it only on the owner's explicit instruction, outside the
sandbox:

```
cd <job root>/runtime   # or the source repo
<venv python> -B -m btc.carry list --root <job root>
<venv python> -B -m btc.carry release --root <job root> --incident <incident_id> --reason "<why, who confirmed>"
```

- `list` is read-only: prints JSON `{"status": "ok", "open_incidents": [...]}` with `incident_id`, `published_at`,
  `title`, `affected_source_ids`, `recorded_edition_id`, `recorded_as_of`. On an invalid carried file it prints
  `{"status": "invalid", "error": "<code>"}` and exits 2.

- Sets `status: released_manually`, `release_reason`, `released_at` (UTC) and `released_by: owner_instruction`.
- Refuses unknown ids, ids that are not open, and a reason that is empty, longer than 500 characters or contains any
  control character in the raw text (tab, newline and carriage return included; exit 2). Only then are runs of
  spaces collapsed. Never deletes records; writes atomically under the same lock as acceptance.
- `release` refuses to run while either carried file is invalid (exit 2, file untouched).
- A released incident (code-verified recovery or manual) never re-opens from the identical publication. The
  `incident_id` covers title cluster, URL and `published_at`, so the same title and URL with another publication time
  (an in-place update, a re-post or a re-dated old article) is a new incident and holds again (safe side).
- Closures and the 14-day display are evaluated as of the edition's `as_of`: a release made while an edition is being
  built (or after an old collection) takes effect from the next edition; that edition still holds.
- The parent and the scheduled run never use these commands (they are not in `PARENT-WORKFLOW.md`).

Trust model. There is no local authentication barrier: `released_by: owner_instruction` is an operator assertion
written by whoever runs the CLI, and the parent agent runs with full access, so a prompt-injected parent could call
it. Protection is by operating rule plus visibility:

- Operating rule: only the owner (or an operator acting on the owner's explicit instruction) runs `release`; the
  parent workflow must never call the CLI, and `PARENT-WORKFLOW.md` never mentions it.
- Visibility: for 14 days after a manual release, every edition shows it in the MD/HTML incident lines (section 7)
  and in the parent briefing: `incident_id`, title, `released_at` in JST, reason and `released_by`
  (`state.incidents.recently_released`). A `released_manually` record without a matching audit entry needs no other
  check; the visible line is the control.

Carried files (`btc-known-news.json`, `btc-incidents.json`) are validated strictly on every read (schema version,
record keys and types, incident status, and the evidence each closed status needs: `released` has
`released_edition_id`, `released_as_of` and `recovery`; `released_manually` has a non-empty `release_reason`,
`released_at` and `released_by: owner_instruction`). A path that exists but is not a regular file is invalid. An invalid file never reads as "nothing known, nothing open": the edition
goes to `data_hold` (`carry_state_invalid`), acceptance skips every carry write (`carry.skipped`), and the file is
left byte-for-byte for the owner to inspect and repair.

## Security boundary

### Credentials

No secret value ever enters the sandbox except `FRED_API_KEY` in the collect worker.

| Step | Where | Secrets |
|---|---|---|
| 1. collect worker (`--collect-worker`): context sources and news headlines | `op-run-batch.sh <tpl> /usr/bin/env -u OP_SERVICE_ACCOUNT_TOKEN sandbox-exec ...` | `FRED_API_KEY` only; op's service-account token is removed before `sandbox-exec` |
| 2. Jev headline triage | the entry, outside the sandbox (`runtime/btc/headline_triage.py`, standard library only) | the triage script resolves its own credential with op, as for XAU |
| 3. news-detail worker (`--finish-worker`): selection, article bodies, reactions, quotes and books | `sandbox-exec` without any wrapper | none |
| 4. prepare, package and review workers | `sandbox-exec` | none |

- Every worker exits 78 with the label `credential_env_present` before any file or network work if any `OP_*`
  variable is present. The entry records the label. Names and values are never printed.
- Triage hand-off (constants equal `scrapers/news_triage.py`; a test checks this):
  - `news-candidates.json` holds public headline fields only: id, url, title, excerpt, publisher, published_at.
  - The entry validates it strictly: at most 200 rows, 512 KB, fixed lengths, https URLs, `n###` ids. It never
    follows symlinks or opens FIFOs.
  - The entry runs the script in a private temp directory with a minimal environment and a 25 s timeout.
    `LAA_JEV_DISABLED=1` is honoured here.
  - `news-triage.json` holds per-id scores and a fixed `{mode, reason}` record only. The worker validates it again;
    anything invalid falls back to the keyword rule.
  - The Jev daily ledger (`/Users/laa/.codex/work/jev`) is written by the entry, outside the sandbox.

### Writable paths

Everything outside the paths below is denied by `(deny file-write* (subpath "/"))`.

| Writable path | Why |
|---|---|
| `<root>/reports`, `work`, `logs`, `history`, `latest-(daily\|weekly).json` | job outputs and state |
| `(param "TMPDIR")` (private per-run temp) | temp files, Playwright profile, `MAC_CHROMIUM_TMPDIR` |
| `/dev/null`, `/dev/zero`, `/dev/dtracehelper`, `/dev/tty*`, `/dev/fd/*` | standard streams |

Not writable: `/private/tmp`, including op's `/tmp/com.agilebits.op.<uid>`; the per-user temp dir
(`DARWIN_USER_TEMP_DIR`, where the user's Chrome keeps its singleton socket); the Jev ledger. The tests check this
allow-list exactly and probe the denials with `sandbox-exec`.

### Readable paths under /Users/laa

`(deny file-read-data (subpath "/Users/laa"))`, then only these are readable:

| Readable path | Why |
|---|---|
| the worker python's `sys.prefix` and `sys.base_prefix` | interpreter and site-packages. `install_job` derives both from `--python` at install time and refuses home, a parent of a protected tree or odd characters |
| `/Users/laa/Library/Caches/ms-playwright` | bundled Chromium |
| `/Users/laa/.agents/skills/human-first-docs` | report renderer |
| `<root>` | runtime snapshot, work, reports, history |
| `(param "TMPDIR")` | per-run temp |

After the allow block, so that nothing can reopen them:
- `file-read*` is denied for `/Users/laa/Brain`, the XAU job, `~/Library/Keychains` and `/Library/Keychains`.
- `mach-lookup` is denied for the Security services: `SecurityServer`, `securityd`, `securityd.xpc`,
  `security.agent`, `security.authhost` and `secd`.
- `process-exec` is denied for `/usr/bin/security` and for op: the `/opt/homebrew/bin/op` and `/usr/local/bin/op`
  symlinks, the resolved Caskroom binary (exec rules match the resolved path), `/Applications/1Password.app` and any
  path ending in `/op`.

Python stays inside these paths. Measured on 2026-10-08 with a full chain under an audit hook (collect, finish,
prepare and package workers): no read or exec under `/Users/laa` outside the table. The worker's cwd is the runtime
snapshot; a process whose cwd or `sys.path` entry is outside these paths fails at import. Fonts come from
`/System/Library/Fonts` (Hiragino Sans is first in the CSS); `~/Library/Fonts` is not readable.

In-sandbox probes in the tests print fixed labels only; nothing touches the Keychain outside the sandbox. All of
these return EPERM: exec of `security list-keychains` and of `op --version`, listing `~/Library/Keychains` and
`~/.ssh`, and reading `/Users/laa/AGENTS.md` or the source repository.

### Network fetches (SSRF)

`btc.fetch.Fetcher` (guard on by default) applies these rules:
- Accepted URLs: `https` on the default port with a DNS host name; no userinfo and no IP literal in any notation.
- Redirects: followed one hop at a time, to the same host only.
- Name resolution: each connection resolves the name once, refuses any non-public answer, and connects to the vetted
  address while TLS verifies the host name.
  - Refused: loopback, private, link-local, multicast, reserved, unspecified and shared addresses, including the
    IPv4-mapped, 6to4, Teredo and NAT64 forms.
  - Because the socket uses the vetted address, a second DNS answer cannot redirect it.
- No proxy or netrc.

Article bodies are fetched only for each feed's fixed host (`btc.news.BODY_HOSTS`); requests always use
`allow_redirects=False`.

| Fetch | URL source |
|---|---|
| Binance, Bybit, OKX derivatives and books; Kraken, Bitstamp, Binance tickers | constants plus constant parameters |
| Deribit `get_book_summary_by_currency`, `get_instruments`, `get_volatility_index_data` | constants; timestamps from our clock |
| Deribit `ticker` | `instrument_name` comes from Deribit's own list: pattern-checked (`BTC-<d><MON><yy>-<strike>-<C\|P>`), params-encoded |
| Farside, Alternative.me FGI, CFTC Socrata, DefiLlama, mempool.space, CoinGecko, Treasury XML, FRED | constants plus constant or validated parameters (month string, series id from code) |
| Fed FOMC calendar, BEA schedule | constants; page links are not followed |
| RSS feeds (5 media, SEC, CFTC, Fed) | constants |
| Article bodies | RSS item link; fetched only if the host equals the feed's allowed host (otherwise `url_not_allowed`, not requested) |
| Binance 1-minute klines (news reaction) | constant URL; `startTime` from a parsed timestamp |

The code fetches nothing from iShares or EDGAR and follows no next-page tokens or in-page links. BLS comes from the
committed cache.

## Storage per run (measured 2026-10-08)

| Item | Size | Location |
|---|---|---|
| Collection (`collection.json`) | about 0.7 MB | work folder and edition (`.data.json`) |
| Facts | about 0.45 MB | work folder and edition |
| Analysis input | about 50 KB | work folder and edition |
| Edition without images | about 2 MB | `reports/<mode>/editions/<name>/` |
| Render evidence (about 120 PNG) | about 34 MB | `<edition>.render-evidence/` |
| History (append-only JSONL) | about 0.1 MB after a few runs | `history/` |
| Carried state (known news, incidents; written on parent acceptance) | a few KB | `history/btc-known-news.json`, `history/btc-incidents.json` |

Raw HTTP bodies are never stored, only their SHA-256.

Two dailies a day come to about 75 MB a day, mostly screenshots. There is no automatic pruning yet. Proposed caps:
- keep render evidence for 30 days, except for parent-passed editions referenced by Weekly;
- keep work folders for 14 days;
- keep editions without images for 1 year.

## Schemas

`btc/schemas/{facts,machine,parent-analysis}.schema.json` are checked by `btc/jsonschema_lite.py`, which treats
unknown keywords as errors.

Cross-check on 2026-10-08 with `jsonschema` 4.26.0 (`Draft202012Validator` and `FormatChecker`, with
`rfc3339-validator` and `rfc3987` in a temporary venv):
- all three schemas pass `check_schema`;
- live outputs (facts, machine, analysis) have 0 errors;
- an extra machine property and a bad `date-time` are rejected.
