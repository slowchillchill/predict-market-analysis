# ETH Up/Down Cross-Platform Volume Report Plan

> Review gate for Issue #6. This is a plan-only change. Do not implement code
> until this plan is reviewed and explicitly approved.

## Goal

Extend the existing cross-platform Up/Down volume report so it can generate
ETH Up/Down volume statistics for Polymarket and Kalshi using the same semantic
guardrails as the BTC report.

The report should cover February, March, and April 2026 and show:

- Polymarket-only daily and monthly totals
- Kalshi-only daily and monthly totals
- Combined daily and monthly totals across semantically comparable rows

## Assumptions

- Date range stays aligned with the current BTC report:
  `2026-02-01T00:00:00Z <= close/end time < 2026-05-01T00:00:00Z`.
- Market types stay aligned with the current report:
  `5min`, `15min`, `hourly`, `4hour`, and `daily`.
- Volume basis stays platform-reported cumulative contract/share volume:
  Polymarket Gamma market volume fields and Kalshi `market.volume_fp`.
- Generated raw data, CSVs, and Markdown summaries remain local artifacts and
  are not committed to GitHub.

## Semantic Rule

Only combine ETH markets that are directional Up/Down equivalents.

Do not include Kalshi absolute above/below, range, one-touch, ATH, min/max,
EOY, ETF, fork, or multi-strike products in combined ETH Up/Down totals. These
are different products and would distort the comparable totals.

## Read-Only Discovery Snapshot

Read-only API probes on 2026-05-14 found these working candidates.

Polymarket exact ETH Up/Down series:

| market_type | Polymarket series_slug |
| --- | --- |
| `5min` | `eth-up-or-down-5m` |
| `15min` | `eth-up-or-down-15m` |
| `hourly` | `eth-up-or-down-hourly` |
| `4hour` | `eth-up-or-down-4h` |
| `daily` | `eth-up-or-down-daily` |

Kalshi candidate mapping:

| market_type | Kalshi series_ticker | title | classification |
| --- | --- | --- | --- |
| `15min` | `KXETH15M` | `ETH 15M price up down` | exact directional candidate |

Kalshi exclusions:

| series_ticker | reason |
| --- | --- |
| `ETH` | daily Ethereum range market |
| `KXETHD` | absolute above/below multi-strike market |
| `ETHD` | daily absolute above/below market |
| `KXETH` | range bucket market |
| `ETHATH` / `KXETHATH` | all-time-high markets |
| `ETHETF` / `KXETHETF` | ETF listed markets |
| `KXETHMAXMON` / `KXETHMINMON` | monthly one-touch markets |
| `ETHMAXY` / `KXETHMAXY` | annual high threshold markets |
| `ETHMINY` / `KXETHMINY` | annual low threshold markets |

Additional ETH-related Kalshi diagnostics from the 2026-05-14 probe include
`BTCETHATH`, `BTCETHRETURN`, `KXBTCETHATH`, `KXBTCETHRETURN`, `KXBTCVSETH`,
`KXETHE`, `KXETHEU`, `KXETHFLIP`, `KXETHFORK`, `KXETHMAXM`, `KXETHY`,
`KXREVETH`, `KXREVSOL`, `KXSOLETHRATIO`, and `KXSOLFLIPETH`. These are not
directional ETH Up/Down equivalents and must not enter comparable totals.

## Kalshi ETH Candidate Predicate

Kalshi ETH discovery must not use a naive substring check such as
`"eth" in ticker_or_title.lower()`. That creates false positives from current
Kalshi rows such as:

| ticker | title |
| --- | --- |
| `KXHEGSETH` | `Hegseth Senate Yeas` |
| `KXTETHERPAUSE` | `Tether pause` |
| `KXBETHELSEAT` | `GA Supreme Court: Bethel seat winner?` |
| `KXYCAITOGETHER` | `Altman and Musk on stage together` |
| `KXMOSTSTREAMEDSOMETHINGBEAUTIFUL` | `Most streamed song on Miley Cyrus's Something Beautiful` |

The implementation must use an explicit ETH candidate predicate:

- include ticker families that start with `ETH` or `KXETH`;
- include titles with token `ETH` or word `Ethereum`;
- include cross-asset ETH diagnostics only when title or ticker has `ETH` as a
  token or explicit ticker segment, such as `BTCETHRETURN` or `KXBTCVSETH`;
- exclude lowercase substring-only matches such as `Hegseth`, `Tether`,
  `Bethel`, and `together`.

Only `KXETH15M` may be classified as `exact_direction` unless a later reviewed
probe proves another Kalshi series is semantically equivalent to ETH Up/Down.
Every other ETH candidate discovered by the predicate must be listed as
non-comparable diagnostics and excluded from combined totals.

## Proposed Implementation

1. Keep the existing BTC behavior backward compatible.
2. Add an asset configuration layer for report-specific values:
   - display name and title phrase
   - Polymarket series mapping
   - Kalshi exact directional series
   - Kalshi candidate predicate and excluded diagnostics
   - output file prefix
3. Add an explicit CLI option such as `--asset btc|eth`, defaulting to `btc`.
4. Reuse the existing collector, normalization, aggregation, and writer logic.
5. Generate ETH outputs with deterministic names:
   - `data/raw/eth_updown_platform_events_2026-02_2026-04.jsonl.gz`
   - `outputs/eth_updown_market_detail_by_platform_2026-02_2026-04.csv`
   - `outputs/eth_updown_daily_volume_by_platform_2026-02_2026-04.csv`
   - `outputs/eth_updown_monthly_volume_by_platform_2026-02_2026-04.csv`
   - `outputs/eth_updown_daily_volume_combined_2026-02_2026-04.csv`
   - `outputs/eth_updown_monthly_volume_combined_2026-02_2026-04.csv`
   - `reports/eth_updown_cross_platform_volume_summary_2026-02_2026-04.md`
   - `reports/kalshi_eth_series_discovery_2026-02_2026-04.md`
6. Update `.gitignore` for the new generated ETH report names.

## Validation Plan

Before implementation:

```bash
python3 -m unittest discover -s tests
```

During implementation, add failing tests first for:

- ETH asset configuration and output path names
- ETH Polymarket series-to-market-type mapping
- Kalshi `KXETH15M` exact directional classification
- Kalshi ETH candidate predicate rejecting substring false positives such as
  `Hegseth`, `Tether`, `Bethel`, and `together`
- Kalshi ETH diagnostics, including legacy/non-`KX` tickers, staying out of
  comparable totals unless explicitly approved as exact direction
- ETH directional market title validation

After implementation:

```bash
python3 -m unittest discover -s tests
python3 scripts/btc_updown_volume_report.py --asset eth
```

Expected result:

- Unit tests pass.
- ETH generation exits with code `0`.
- Output files are created at the deterministic ETH paths above.
- Combined ETH totals include only `comparable=true` rows.
- Kalshi discovery report lists `KXETH15M` as comparable, lists every ETH
  candidate matched by the explicit predicate as diagnostics, and excludes all
  non-directional ETH products from combined totals.

## Review Questions

- Should ETH use the same February-April 2026 date range as BTC?
- Should the existing script name remain `btc_updown_volume_report.py` with
  `--asset eth`, or should a later cleanup introduce a neutral script name?
