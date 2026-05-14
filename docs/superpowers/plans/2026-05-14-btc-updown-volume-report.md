# BTC Up/Down Volume Report Execution Plan

> For agentic workers: implement this plan task by task. Keep changes small, verify each step, self-review diffs before committing, and do not include unrelated local files.

## Goal

Produce reproducible reports for Polymarket BTC Up/Down market volume across February, March, and April 2026.

The report must cover five market types:

- `5min`
- `15min`
- `hourly`
- `4hour`
- `daily`

For each month and market type, produce:

- per-market volume detail
- daily total volume
- monthly total volume

## Scope and Definitions

- Target platform: Polymarket.
- Target market family: BTC Up/Down only.
- Date range: `2026-02-01T00:00:00Z` through `2026-05-01T00:00:00Z`, using half-open UTC month windows.
- Date bucketing: UTC API time, using each event or market `endDate`.
- Volume basis: market-assigned cumulative volume from Gamma API, not trade timestamp volume from individual `/trades` rows.
- Primary volume field priority:
  1. `market.volumeClob`
  2. `market.volumeNum`
  3. `market.volume`
  4. `event.volume`

This means a market ending on `2026-03-01T00:00:00Z` belongs to March, not February.

## Data Sources

Use public Polymarket APIs only.

- Gamma API base: `https://gamma-api.polymarket.com`
- Primary endpoint: `GET /events`
- Optional audit endpoint: `https://data-api.polymarket.com/trades`

Official references:

- API overview and authentication: `https://docs.polymarket.com/api-reference/introduction`
- Market discovery: `https://docs.polymarket.com/market-data/fetching-markets`
- Events API: `https://docs.polymarket.com/api-reference/events/list-events`
- Trades API: `https://docs.polymarket.com/api-reference/core/get-trades-for-a-user-or-markets`
- Rate limits: `https://docs.polymarket.com/api-reference/rate-limits`

## Market Type Mapping

Use these series slugs as the canonical classifier:

| market_type | series_slug |
| --- | --- |
| `5min` | `btc-up-or-down-5m` |
| `15min` | `btc-up-or-down-15m` |
| `hourly` | `btc-up-or-down-hourly` |
| `4hour` | `btc-up-or-down-4h` |
| `daily` | `btc-up-or-down-daily` |

Do not classify `4hour` from `series.recurrence`; live samples show the `btc-up-or-down-4h` series can report `recurrence=daily`. Classify it by `series.slug`.

## Query Strategy

For each market type and month, query:

```text
GET https://gamma-api.polymarket.com/events
  ?closed=true
  &series_slug=<series_slug>
  &end_date_min=<month_start_utc>
  &end_date_max=<next_month_start_utc>
  &order=end_date
  &ascending=true
  &limit=100
  &offset=<offset>
```

Month windows:

| month_utc | start | end |
| --- | --- | --- |
| `2026-02` | `2026-02-01T00:00:00Z` | `2026-03-01T00:00:00Z` |
| `2026-03` | `2026-03-01T00:00:00Z` | `2026-04-01T00:00:00Z` |
| `2026-04` | `2026-04-01T00:00:00Z` | `2026-05-01T00:00:00Z` |

Pagination rules:

- Start with `offset=0`.
- Increase offset by `limit`.
- Stop when the response contains fewer than `limit` events.
- Maintain a set of seen event slugs and condition IDs.
- Stop and fail validation if the same page repeats, because that indicates unsafe pagination behavior.

Fallback query if `series_slug` stops working:

```text
GET https://gamma-api.polymarket.com/events
  ?closed=true
  &tag_slug=bitcoin
  &end_date_min=<month_start_utc>
  &end_date_max=<next_month_start_utc>
  &order=end_date
  &ascending=true
  &limit=100
  &offset=<offset>
```

Then filter locally to events where any `event.series[].slug` equals one of the five target slugs.

## Output Files

Write outputs under deterministic paths:

- Raw API copy:
  - `data/raw/btc_updown_gamma_events_2026-02_2026-04.jsonl`
- Per-market detail:
  - `outputs/btc_updown_market_detail_2026-02_2026-04.csv`
- Daily totals:
  - `outputs/btc_updown_daily_volume_2026-02_2026-04.csv`
- Monthly totals:
  - `outputs/btc_updown_monthly_volume_2026-02_2026-04.csv`
- Human summary:
  - `reports/btc_updown_volume_summary_2026-02_2026-04.md`

## Output Schemas

`btc_updown_market_detail_2026-02_2026-04.csv`:

```text
month_utc,date_utc,market_type,series_slug,event_id,event_slug,market_id,market_slug,condition_id,title,end_date_utc,closed_time_utc,volume,volume_source,volume_clob,volume_num,event_volume
```

`btc_updown_daily_volume_2026-02_2026-04.csv`:

```text
date_utc,month_utc,market_type,market_count,total_volume
```

`btc_updown_monthly_volume_2026-02_2026-04.csv`:

```text
month_utc,market_type,market_count,total_volume
```

## Implementation Steps

1. Create a stdlib-only Python script at `scripts/btc_updown_volume_report.py`.
2. Define constants for the five market types, three month windows, output paths, and the Gamma API base URL.
3. Implement HTTP GET with `urllib.request`, timeout, JSON parsing, and a fixed user agent such as `poly-market-analysis/0.1`.
4. Implement paginated event fetch for `(market_type, month)` using the primary `series_slug` query.
5. Implement fallback fetch with `tag_slug=bitcoin` and local series filtering.
6. Normalize each returned event into one market detail row.
7. Validate each market row:
   - target `series_slug` exists
   - title contains `Bitcoin Up or Down`
   - outcomes parse to `["Up", "Down"]`
   - condition ID is present and not duplicated
   - volume parses as a non-negative number
8. Write raw JSONL before aggregation.
9. Write the per-market detail CSV.
10. Aggregate detail rows into daily totals by `(date_utc, market_type)`.
11. Aggregate daily totals into monthly totals by `(month_utc, market_type)`.
12. Write a Markdown summary with:
    - monthly total matrix
    - market count matrix
    - output file paths
    - generated timestamp
    - anomaly count

## Validation Plan

Run these checks after implementation:

```bash
python3 scripts/btc_updown_volume_report.py
```

Expected:

- Creates all five output files listed above.
- Exits with code `0`.
- Prints a concise generation summary.

Run structural checks:

```bash
python3 - <<'PY'
import csv
from collections import defaultdict
from decimal import Decimal

detail_path = "outputs/btc_updown_market_detail_2026-02_2026-04.csv"
daily_path = "outputs/btc_updown_daily_volume_2026-02_2026-04.csv"
monthly_path = "outputs/btc_updown_monthly_volume_2026-02_2026-04.csv"

detail_daily = defaultdict(Decimal)
condition_ids = set()
with open(detail_path, newline="") as f:
    for row in csv.DictReader(f):
        key = (row["date_utc"], row["month_utc"], row["market_type"])
        detail_daily[key] += Decimal(row["volume"])
        condition_id = row["condition_id"]
        if condition_id in condition_ids:
            raise SystemExit(f"duplicate condition_id: {condition_id}")
        condition_ids.add(condition_id)

daily_monthly = defaultdict(Decimal)
with open(daily_path, newline="") as f:
    for row in csv.DictReader(f):
        key = (row["date_utc"], row["month_utc"], row["market_type"])
        value = Decimal(row["total_volume"])
        if detail_daily[key] != value:
            raise SystemExit(f"daily mismatch: {key}")
        daily_monthly[(row["month_utc"], row["market_type"])] += value

with open(monthly_path, newline="") as f:
    for row in csv.DictReader(f):
        key = (row["month_utc"], row["market_type"])
        if daily_monthly[key] != Decimal(row["total_volume"]):
            raise SystemExit(f"monthly mismatch: {key}")

print("aggregation checks passed")
PY
```

Expected:

```text
aggregation checks passed
```

Run Git review checks:

```bash
git diff --check
git status --short
```

Expected:

- No whitespace errors.
- Only intended script, data outputs, report outputs, and related docs are staged for implementation work.

## Known Data Caveats

- `5min` markets may not exist for every day in February 2026. A live sample found no `5min` events on `2026-02-01`, but did find them on `2026-02-23`.
- Gamma API volume fields are platform-reported cumulative market volume. They may not match a trade-timestamp reconstruction from `/trades` if the trade API has pagination, taker-only defaults, or indexing differences.
- The plan intentionally uses UTC API time because that was the selected reporting boundary, even though many BTC Up/Down titles are written in ET.

## Commit and Review Discipline

- Keep the plan-only commit separate from implementation commits.
- For implementation, create one small commit after the script and its validation logic are self-reviewed.
- Push only after a larger step is complete and verified.
- Request review from `awgcoder` on the pull request for this repository only.
