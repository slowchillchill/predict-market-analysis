# Polymarket Crypto Up/Down Analysis

**English** | [简体中文](README.zh-CN.md)

Collect trade records from Polymarket crypto Up/Down markets, analyze wallet activity in SQLite, and generate daily reports, daily, weekly and monthly posters, and English post text for X.

The main workflow covers 38 market series and groups markets by their **UTC end date**. Collection is resumable; reporting and poster generation run offline against a read-only database connection. This repository contains the tools, tests, and poster assets; download market data locally before generating reports.

## Features

- Discover markets across eight crypto assets and five timeframes.
- Store trade records and pagination progress in SQLite, with each page committed atomically.
- Export daily wallet volume, unique wallet counts, and the top 200 wallets to CSV, alongside a Chinese Markdown report.
- Generate a 1600 × 2000 English PNG poster, supporting JSON, and English post text for X.
- Generate burgundy-and-gold weekly posters with week-over-week comparisons, a seven-day chart, asset shares, and suspected bot activity; weekly post text fits ordinary X posts.
- Generate monthly posters in ivory, forest green, and graphite with calendar-month comparisons, daily averages, full-month charts, asset shares, suspected bot activity, and English post text.
- Query coverage and wallet rankings through SQL views.
- Retain the original BTC 5-minute workflow for historical comparisons.

## Market coverage

The series catalog is defined in [`scripts/polymarket_updown_daily.py`](scripts/polymarket_updown_daily.py).

| Assets | Timeframes | Series |
| --- | --- | ---: |
| BTC, ETH, SOL, XRP, DOGE, BNB, HYPE | 5 minutes, 15 minutes, 1 hour, 4 hours, daily | 35 |
| ZEC | 5 minutes, 15 minutes, 4 hours | 3 |

Once all series are operating, the configured schedule expects **3,295 markets per UTC day**. Coverage is checked per series using verified historical launch boundaries, shared by daily, weekly and monthly reports. Unverified gaps remain incomplete rather than being treated as zero volume.

## Quick start

Use Python 3.10 or newer. The commands below use Bash on Linux or macOS and run from the repository root. The `sqlite3` CLI is optional for direct SQL queries.

### 1. Install

```bash
git clone https://github.com/slowchillchill/predict-market-analysis.git
cd predict-market-analysis
python3 -m venv .venv-btc5m
.venv-btc5m/bin/python -m pip install -r requirements-btc5m.txt
.venv-btc5m/bin/python -m pip install -r requirements-poster.txt
```

The poster dependency is Pillow; omit that installation if you only need collection and CSV/Markdown reports. Offline daily reporting uses only the Python standard library.

### Run yesterday's complete workflow

After installing both dependencies, run directly. The script defaults to the local Hong Kong proxy at `socks5h://127.0.0.1:20810`:

```bash
.venv-btc5m/bin/python scripts/polymarket_updown_yesterday.py
```

This fixes yesterday's UTC date at startup using the system clock, collects that day's markets using existing download progress, then generates the English poster, JSON, and post text only after collection succeeds. It uses the built-in proxy address without switching VPN nodes or checking the exit location; use `--proxy` to override the address in another environment. Previously completed markets are reused. Day-over-day comparisons use existing prior-day data; missing prior-day data displays `N/A`. A rerun after UTC midnight targets the new yesterday; use the explicit-date commands below to finish an older date.

### 2. Collect data

The collector requires an explicit `--proxy` URL; its CLI and existing setup use a local Hong Kong proxy. It does not read proxy settings from environment variables automatically. Enter your configured URL, for example `socks5h://127.0.0.1:PORT` with your actual port:

```bash
read -r -p 'Local Hong Kong proxy URL: ' UPDOWN_PROXY

.venv-btc5m/bin/python scripts/polymarket_updown_daily.py collect \
  --start-date 2026-09-17 --end-date 2026-09-19 \
  --proxy "$UPDOWN_PROXY" \
  --db data/raw/btc5m.sqlite3
```

This example collects markets ending on September 17 and 18, 2026 (UTC). Replace the dates as needed: the start is inclusive and the end is exclusive. Collect the preceding day too if you want day-over-day comparisons on a poster.

Press `Ctrl+C` to stop and rerun the same command to resume. Completed markets are skipped. Run only one collector per database at a time. The default database keeps its historical name, `btc5m.sqlite3`, and is shared by both workflows; no separate copy of trade data is needed.

Market discovery uses Gamma `/events/keyset`, passing the returned `next_cursor` as `after_cursor` for the next page until no cursor remains. Missing scheduled slots are also queried through `/events/slug/{slug}` because an inactive market may remain available by slug while being omitted from the list endpoint. Market end dates and series membership determine inclusion regardless of active or closed flags. Slots unavailable through the detail endpoint remain coverage gaps.

### 3. Export a daily report

After collection has created the database and installed the Up/Down views:

```bash
.venv-btc5m/bin/python scripts/polymarket_updown_daily.py report \
  --date 2026-09-18 \
  --db data/raw/btc5m.sqlite3 \
  --output-dir outputs/updown
```

For a complete day, this writes:

- `updown_daily_2026-09-18.csv` — daily totals and top-200 volume share.
- `updown_top200_2026-09-18.csv` — wallet addresses, ranks, and volumes.
- `updown_daily_2026-09-18.md` — Chinese report and coverage details.

An incomplete day produces only the coverage report. Rerun collection to address gaps.

### 4. Generate an English poster and post text

```bash
.venv-btc5m/bin/python scripts/polymarket_updown_poster.py \
  --date 2026-09-18 \
  --db data/raw/btc5m.sqlite3 \
  --output-dir outputs/posters
```

This writes `updown_2026-09-18.png`, `updown_2026-09-18.json`, and `updown_2026-09-18_tweet.md`. The Markdown file contains the English post body. The script generates files locally; publishing to X is a separate step. Generating the same date again replaces its output files.

The current day must be complete. If the previous day is incomplete or its comparison value is zero, the affected day-over-day comparison displays `N/A`. The background and Inter font are bundled, so rendering needs no image-generation service or system font installation.

For these workflows, exit code `0` means success, `2` indicates incomplete coverage (argument errors also use `2`), and `130` indicates an interrupted collection. CLI help, progress messages, and daily Markdown reports are currently in Chinese; posters and post text are in English.

### 5. Generate last week's poster and post text

```bash
.venv-btc5m/bin/python scripts/polymarket_updown_weekly_poster.py
```

The default selects the previous complete Monday-to-Sunday week using the UTC date at startup. To select a specific week:

```bash
.venv-btc5m/bin/python scripts/polymarket_updown_weekly_poster.py --week-start 2026-09-14
```

This writes `updown_weekly_2026-09-14_2026-09-20.png`, matching `.json`, and `_tweet.md` files under `outputs/posters/`. It reads the local database without collecting data. All seven target days must be complete; comparisons use the preceding complete week and display `N/A` for missing or zero baselines.

Weekly wallets are deduplicated across the entire week. Suspected bots are classified daily: the weekly count is the union of flagged wallets, and bot volume includes only each flagged wallet's volume in the corresponding day's markets. Post text keeps the core figures, leaves detailed methodology in the chart, and includes no narrative takeaway. The template uses a 280-character budget and shortens supporting text when numbers require more space.

For scheduling, run the command without a date argument so each invocation selects the previous week automatically. Timers are local configuration and are not installed by cloning this repository. See the [weekly poster guide](docs/updown_weekly_poster.md).

### 6. Generate the previous month's poster and post text

```bash
.venv-btc5m/bin/python scripts/polymarket_updown_monthly_poster.py
```

The UTC date at startup selects the previous complete calendar month and compares it with the month before that. For example, a run on October 1, 2026 reports September against August. Year boundaries and leap years are supported. To specify a historical month, database, and output directory:

```bash
.venv-btc5m/bin/python scripts/polymarket_updown_monthly_poster.py \
  --month 2026-09 \
  --db data/raw/btc5m.sqlite3 \
  --output-dir outputs/posters
```

This writes `updown_monthly_2026-09.png`, matching `.json`, and `_tweet.md` files. Rerunning the same month replaces its outputs. The monthly palette uses ivory, forest green, and graphite to distinguish it from the dark blue daily and burgundy-and-gold weekly posters. Post text uses the ordinary X post budget of 280 characters.

Wallets are deduplicated across the month, and daily averages use each month's actual day count. Suspected bots are classified daily: monthly wallets are the union of flagged wallets, and bot volume sums each flagged wallet's volume in the corresponding day's markets. Monthly share changes are expressed in percentage points. The poster includes daily trends and asset shares; changes caused by series launches are disclosed in the poster and post text.

Weekly and monthly reports share a query that aggregates one day at a time, with both comparison periods read in one read-only transaction. Monthly reporting does not collect data. Initialize the views through the collector and prepare both months before running it. An incomplete target month exits with code `2` without generating new files; an incomplete prior month or a zero comparison baseline displays `N/A` for the affected comparison.

**Scheduled runs:** the existing deployment has `polymarket-updown-monthly.timer` enabled for **12:30 Asia/Tokyo on day 1 of each month** (11:30 Beijing time, 03:30 UTC), after daily collection and weekly reporting. It invokes the default command above. With `Persistent=true`, reactivating the user timer triggers one catch-up run after a missed activation; it does not backfill every missed month or wake the computer. A delayed run still selects the previous complete UTC month at startup; use `--month` to backfill an older month.

The service and timer are local configuration and are not installed by cloning this repository. On a configured host, inspect status and logs with:

```bash
systemctl --user status polymarket-updown-monthly.timer
journalctl --user -u polymarket-updown-monthly.service
```

**One-time adjustment to the first report:** the adjusted September-versus-August 2026 report explicitly excludes 214 unavailable ZEC slots from August under specific user authorization. The poster and post text disclose the exclusion, and the JSON records the exact list. That report and its one-time rebuild script remain in local `outputs/`; they do not change the standard reporting rules. Daily, weekly, and future monthly reports retain completeness checks, and the standard `--month 2026-09` command also follows the strict behavior above.

See the [monthly poster guide](docs/updown_monthly_poster.md).

## How to interpret the data

| Metric or behavior | Definition |
| --- | --- |
| Date | UTC date of the market's `endDate`, not its settlement confirmation or individual trade date. All returned trades for selected markets are included, including early and cross-day trades. |
| Wallet volume | Buy expenditure plus sell proceeds. Each record's `price × size` is rounded using `ROUND_HALF_UP` to integer micro-USDC before summing. This is not a single-sided market-volume metric. |
| Unique wallets | Lowercase wallet addresses deduplicated across all selected markets in the day, week, or month. Weekly and monthly counts are not sums of daily wallet counts. |
| Top 200 | Wallets ranked by aggregated volume descending, then address ascending to break ties. |
| Suspected bots in posters | Wallets with an average trade interval of at most 60 seconds and a first-to-last trade span of at least 90 minutes, across the selected markets. No minimum market count is required. This is a behavioral heuristic, not identity verification. |
| Completeness | Expected per-series market counts plus completed trade pagination. This is API collection coverage, not a full on-chain reconciliation. |

The collector uses `/v2/trades` with `taker_only=false`, `filter_type=TOKENS`, `filter_amount=1e-18`, and `limit=1000`, following cursors until `next_cursor=null`. It preserves returned records rather than deduplicating by transaction hash.

The legacy [`README_BTC5M.md`](README_BTC5M.md) describes a different workflow: BTC 5-minute summaries use the **UTC market start date** and top 10 wallets. Its manual bot-analysis query also uses different date and threshold rules. Use the definitions above for the all-series workflow.

## SQL access

With the optional `sqlite3` CLI:

```bash
sqlite3 -readonly -header -column data/raw/btc5m.sqlite3 <<'SQL'
SELECT * FROM updown_coverage WHERE date_utc = '2026-09-18';
SELECT * FROM updown_series_coverage
WHERE date_utc = '2026-09-18' AND NOT is_complete;
SELECT * FROM updown_daily_summary WHERE date_utc = '2026-09-18';
SQL
```

The summary and ranking views return only complete days. These queries require a database initialized by the all-series collector.

## Project layout and documentation

| Path | Purpose |
| --- | --- |
| [`scripts/`](scripts/) | Collection, offline reports, and poster generation |
| [`sql/`](sql/) | SQLite schema, coverage views, wallet statistics, and poster queries |
| [`tests/`](tests/) | Collection, calculation, and rendering tests |
| [`assets/`](assets/) | Poster background and bundled font; font license in [`Inter-OFL.txt`](assets/fonts/Inter-OFL.txt) |
| `data/raw/` | Local databases; ignored by Git |
| `outputs/` | Generated reports and posters; ignored by Git |

Detailed guides are currently in Chinese:

- [All-series collection and reporting](docs/updown_daily.md)
- [Poster generation and metric definitions](docs/updown_poster.md)
- [Weekly posters, post text, and scheduled runs](docs/updown_weekly_poster.md)
- [Monthly posters, post text, and scheduled runs](docs/updown_monthly_poster.md)
- [Legacy BTC 5-minute guide](README_BTC5M.md)
- [BTC collector implementation notes](docs/btc5m_daily.md)
- [Market-series inventory](reports/crypto_updown_market_types_2026-09-17.md)

## Development

Install both requirements files, then run the offline test suite:

```bash
.venv-btc5m/bin/python -m unittest discover -s tests -v
```

Tests cover pagination and resumption, detail lookups, historical launch boundaries, atomic page commits, UTC date boundaries, shared-database compatibility, decimal rounding, coverage gaps, bot thresholds, calendar months across year boundaries and leap years, monthly wallet deduplication, daily-average comparisons, post length, and poster layout. When reporting a problem or proposing a change, include the command, expected behavior, and a reproducible example without local credentials or raw database files.

## License

This project is licensed under the [MIT License](LICENSE), except for third-party materials with their own licenses. MIT permits commercial use, modification, and redistribution, including in closed-source projects, provided the copyright and license notices are retained. The software is provided without warranty.

The bundled Inter font remains under the [SIL Open Font License 1.1](assets/fonts/Inter-OFL.txt). Dependencies retain their respective licenses. The MIT License does not grant rights to third-party market data or services accessed by these tools.
