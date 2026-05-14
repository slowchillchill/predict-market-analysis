# Cross-Platform BTC Up/Down Volume Report Plan

> For agentic workers: this is a plan-only change. Implement only after review approval. Keep generated data local; do not upload data artifacts to GitHub.

## Goal

Extend the current BTC Up/Down volume report so it can include Kalshi data alongside Polymarket data for February, March, and April 2026.

The resulting report must show:

- Polymarket-only daily and monthly totals
- Kalshi-only daily and monthly totals
- Combined daily and monthly totals across comparable platform rows

The output must continue to cover the requested market types:

- `5min`
- `15min`
- `hourly`
- `4hour`
- `daily`

## Non-Negotiable Semantic Rule

Only combine semantically comparable BTC direction markets.

Polymarket `BTC Up or Down` markets resolve whether BTC is up/down over a time interval. Kalshi markets must represent the same directional question before their volume can be included in the combined totals.

Do not include Kalshi BTC absolute-threshold, range, one-touch, ATH, min/max, or multi-strike above/below markets in the combined BTC Up/Down totals. Those are different products and can overcount one event if multiple strike markets exist for the same event.

## Current Kalshi Discovery Notes

These notes are from a read-only API probe on `2026-05-14`.

Official Kalshi public market data endpoints:

- Base URL: `https://external-api.kalshi.com/trade-api/v2`
- Public market data requires no authentication for the endpoints used here.
- Kalshi `GET /markets` supports cursor pagination and filters including `series_ticker`, `min_close_ts`, `max_close_ts`, and `status`.
- Kalshi historical data is partitioned. `GET /historical/cutoff` currently returned `market_settled_ts=2026-03-14T00:00:00Z`; older settled markets must be read from `/historical/markets`.

Relevant BTC series found from `GET /series?include_volume=true`:

| series_ticker | title | frequency | classification |
| --- | --- | --- | --- |
| `KXBTC15M` | `Bitcoin price up down` | `fifteen_min` | exact Kalshi BTC directional candidate for `15min` |
| `KXBTCD` | `Bitcoin price Above/below` | `hourly` | not equivalent; absolute threshold multi-strike market |
| `BTCD` | `Bitcoin price Above/below` | `daily` | not equivalent; absolute threshold market and no 2026 sample rows in probe |
| `BTCD-B` | `Above/below` | `daily` | not equivalent; absolute threshold market and no 2026 sample rows in probe |
| `KXBTC` | `Bitcoin range` | `hourly` | not equivalent; range/strike market |

Implementation must start by re-running this discovery and writing a local discovery artifact. If new exact Kalshi BTC directional series exist for `5min`, `hourly`, `4hour`, or `daily`, add them only after tests prove the title/rules match direction semantics.

## Platform Mapping

Initial approved mapping:

| market_type | Polymarket series_slug | Kalshi series_ticker | comparable |
| --- | --- | --- | --- |
| `5min` | `btc-up-or-down-5m` | none discovered | Polymarket only |
| `15min` | `btc-up-or-down-15m` | `KXBTC15M` | yes |
| `hourly` | `btc-up-or-down-hourly` | none discovered | Polymarket only |
| `4hour` | `btc-up-or-down-4h` | none discovered | Polymarket only |
| `daily` | `btc-up-or-down-daily` | none discovered | Polymarket only |

Explicit exclusions:

- Do not map `KXBTCD` to `hourly`.
- Do not map `BTCD` or `BTCD-B` to `daily`.
- Do not map `KXBTC` to `hourly`.

These excluded series may be listed in a discovery diagnostics section, but they must not be included in combined BTC Up/Down totals.

## Volume Basis

Use platform-reported cumulative market volume:

- Polymarket: existing Gamma market volume priority remains:
  1. `market.volumeClob`
  2. `market.volumeNum`
  3. `market.volume`
  4. `event.volume`
- Kalshi: `market.volume_fp`

Treat both as platform contract/share volume, not USD notional. The combined totals are useful for platform-reported market activity comparison, not exact traded-dollar notional.

If a platform volume field is missing:

- Keep the market in detail output.
- Set volume to `0`.
- Set `volume_source` to `missing_as_zero`.
- Surface counts in the Markdown anomaly section.

Do not reconstruct missing volume from trade rows unless a later plan explicitly changes the volume basis for both platforms.

## Time Windows and Bucketing

Use the same UTC half-open month windows as the existing Polymarket report:

| month_utc | start | end |
| --- | --- | --- |
| `2026-02` | `2026-02-01T00:00:00Z` | `2026-03-01T00:00:00Z` |
| `2026-03` | `2026-03-01T00:00:00Z` | `2026-04-01T00:00:00Z` |
| `2026-04` | `2026-04-01T00:00:00Z` | `2026-05-01T00:00:00Z` |

For Polymarket, bucket by existing `event.endDate` logic.

For Kalshi, bucket by `market.close_time` in UTC. Apply local half-open filtering:

```text
month_start_utc <= close_time < next_month_start_utc
```

API timestamp filters are discovery filters only. Every row must pass local half-open filtering before it is written to raw, detail, daily, monthly, or combined outputs.

## Kalshi Query Strategy

Before fetching markets:

1. Call `GET /historical/cutoff`.
2. Parse `market_settled_ts`.
3. Split each target month into historical and live portions as needed.

Historical portion:

```text
GET /historical/markets
  ?series_ticker=<series_ticker>
  &limit=1000
  &cursor=<cursor>
```

The historical markets endpoint supports `series_ticker`, `event_ticker`, `tickers`, cursor, and `mve_filter`, but not close-time filters. Fetch pages for the target series and apply local `close_time` filtering. If pages are sorted newest-first, stop once the oldest page close time is earlier than the global start; otherwise continue until cursor is empty.

Live portion:

```text
GET /markets
  ?series_ticker=<series_ticker>
  &min_close_ts=<month_start_unix>
  &max_close_ts=<next_month_start_unix>
  &limit=1000
  &cursor=<cursor>
```

Do not pass `status=settled` together with `min_close_ts/max_close_ts`; Kalshi documents `min_close_ts/max_close_ts` as compatible with `closed` or empty status, and live API samples return completed markets with `status=finalized`. Filter locally to completed statuses such as `settled`, `finalized`, or `closed`.

Deduplicate Kalshi rows by `market.ticker`.

## Output Files

Generated data and report files remain local-only review artifacts. They must not be committed to GitHub. Implementation should update `.gitignore` if any new generated report name is not already ignored.

Recommended deterministic local paths:

- Raw normalized platform records:
  - `data/raw/btc_updown_platform_events_2026-02_2026-04.jsonl.gz`
- Per-market platform detail:
  - `outputs/btc_updown_market_detail_by_platform_2026-02_2026-04.csv`
- Daily platform totals:
  - `outputs/btc_updown_daily_volume_by_platform_2026-02_2026-04.csv`
- Monthly platform totals:
  - `outputs/btc_updown_monthly_volume_by_platform_2026-02_2026-04.csv`
- Daily combined totals:
  - `outputs/btc_updown_daily_volume_combined_2026-02_2026-04.csv`
- Monthly combined totals:
  - `outputs/btc_updown_monthly_volume_combined_2026-02_2026-04.csv`
- Human summary:
  - `reports/btc_updown_cross_platform_volume_summary_2026-02_2026-04.md`
- Kalshi discovery diagnostics:
  - `reports/kalshi_btc_series_discovery_2026-02_2026-04.md`

## Output Schemas

Detail CSV:

```text
platform,month_utc,date_utc,market_type,comparable,platform_series,platform_event_id,platform_market_id,title,subtitle,end_time_utc,status,result,volume,volume_source,raw_volume
```

`platform` values:

- `polymarket`
- `kalshi`

Daily platform CSV:

```text
platform,date_utc,month_utc,market_type,market_count,total_volume
```

Monthly platform CSV:

```text
platform,month_utc,market_type,market_count,total_volume
```

Daily combined CSV:

```text
date_utc,month_utc,market_type,platform_count,market_count,total_volume
```

Monthly combined CSV:

```text
month_utc,market_type,platform_count,market_count,total_volume
```

Combined rows must include only detail rows with `comparable=true`.

## Implementation Steps

1. Refactor the existing script around provider-specific collectors:
   - Polymarket collector
   - Kalshi collector
   - shared normalization/aggregation/writer helpers
2. Keep stdlib-only Python unless a later review explicitly approves dependencies.
3. Add a Kalshi series discovery function:
   - fetch `GET /series?include_volume=true`
   - identify BTC candidate series
   - classify exact directional vs excluded related products
   - write local diagnostics
4. Implement Kalshi market fetch:
   - dynamic historical cutoff read
   - live/historical routing
   - cursor pagination
   - local half-open `close_time` filtering
   - completed-status filtering
   - ticker dedupe
5. Normalize Polymarket and Kalshi detail rows into one detail schema with `platform`.
6. Aggregate:
   - platform daily/monthly rows by `(platform, date/month, market_type)`
   - combined daily/monthly rows by summing comparable platform rows
7. Write all local-only outputs.
8. Update the Markdown summary to include:
   - platform monthly volume matrix
   - combined monthly volume matrix
   - platform market count matrix
   - Kalshi coverage notes for missing market types
   - anomaly counts
   - local output paths

## Tests

Add focused stdlib `unittest` coverage before implementation:

- Kalshi exact series classifier accepts `KXBTC15M` as `15min`.
- Classifier rejects `KXBTCD`, `BTCD`, `BTCD-B`, and `KXBTC` as non-comparable.
- Kalshi half-open `close_time` filter excludes `close_time == next_month_start`.
- Historical/live split uses `/historical/markets` for dates before `market_settled_ts`.
- Cursor pagination stops on empty cursor and rejects repeated cursors.
- Detail aggregation uses `Decimal`.
- Combined daily/monthly totals exactly equal the sum of comparable platform rows.
- Combined totals do not include non-comparable diagnostics rows.

## Validation Commands

After implementation:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 scripts/btc_updown_volume_report.py
```

Run structural checks:

```bash
python3 - <<'PY'
import csv
from collections import defaultdict
from decimal import Decimal

platform_daily = defaultdict(Decimal)
with open("outputs/btc_updown_market_detail_by_platform_2026-02_2026-04.csv", newline="") as f:
    for row in csv.DictReader(f):
        key = (row["platform"], row["date_utc"], row["month_utc"], row["market_type"])
        platform_daily[key] += Decimal(row["volume"])

combined_daily_expected = defaultdict(Decimal)
with open("outputs/btc_updown_daily_volume_by_platform_2026-02_2026-04.csv", newline="") as f:
    for row in csv.DictReader(f):
        key = (row["platform"], row["date_utc"], row["month_utc"], row["market_type"])
        value = Decimal(row["total_volume"])
        if platform_daily[key] != value:
            raise SystemExit(f"platform daily mismatch: {key}")
        combined_daily_expected[(row["date_utc"], row["month_utc"], row["market_type"])] += value

with open("outputs/btc_updown_daily_volume_combined_2026-02_2026-04.csv", newline="") as f:
    for row in csv.DictReader(f):
        key = (row["date_utc"], row["month_utc"], row["market_type"])
        if combined_daily_expected[key] != Decimal(row["total_volume"]):
            raise SystemExit(f"combined daily mismatch: {key}")

print("cross-platform aggregation checks passed")
PY
```

Also run:

```bash
git diff --check
git status --short --ignored=matching
```

Expected Git status:

- Only intended code/tests/docs are tracked.
- Generated `data/`, `outputs/`, and generated `reports/` are ignored.

## Review Risks

- Kalshi has exact `15min` directional BTC series in current discovery, but no exact 5min/hourly/4hour/daily directional series was found. This means combined totals initially add Kalshi only for `15min`.
- Kalshi `KXBTCD` has large volume, but it is an hourly absolute above/below multi-strike product, not an up/down interval product. Including it in combined BTC Up/Down totals would be misleading.
- Kalshi historical endpoints do not expose close-time filters on `/historical/markets`; implementation must rely on cursor pagination plus local filtering.
- Kalshi status strings in live samples can be `finalized`, while docs list `settled`; implementation must validate against live samples without loosening to active/open rows.
- Cross-platform `total_volume` is platform contract/share volume, not traded USD notional.

## Commit and Review Discipline

- Keep this plan-only commit separate from implementation commits.
- For the implementation phase, commit small verified units locally first.
- Push only after a complete large step is verified.
- Request review from `awgcoder` on this repository only.
