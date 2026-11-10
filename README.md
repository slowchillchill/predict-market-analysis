# Polymarket Crypto Updown Markets Analysis

**English** | [简体中文](README.zh-CN.md)

Collect trade records from Polymarket crypto Up/Down markets, analyze wallet activity in SQLite, and generate daily reports, daily, weekly and monthly posters, and English post text for X.

The main workflow covers 38 market series and groups markets by their **UTC end date**. Production collection and SQL analysis run on Kaggle; the local scheduled workflow downloads completed statistics and renders posters without downloading raw trade databases. Manual tools can collect into a separate database or read existing SQLite snapshots.

See the [Kaggle migration and operations guide](docs/kaggle_migration.md) for public daily datasets, continuous historical backfill, Secrets, scheduling, and local retries.

Completed v2 daily databases have three entity tables: `markets`, `collection_progress`, and `trades`. Trades use short market-local `group_id` values; only unfinished markets retain a durable `pending_group_map` for cursor recovery. Market volume is derived from trade groups, with no `market_volume` entity table or separate `taker_only=true` collection. Raw metadata is v2; the local report JSON interface remains v1.

## Verified deployment — November 10, 2026

| Resource | Published version or status |
| --- | --- |
| [Public Kaggle project](https://www.kaggle.com/code/qiuqiuslowchillchill/polymarket-crypto-updown-markets-analysis) | Production Notebook v8; native Daily enabled from November 11, 2026 |
| [2026-11-09 daily database](https://www.kaggle.com/datasets/qiuqiuslowchillchill/pm-updown-raw-2026-11-09/versions/2) | Raw version 2; 1,916,447 wallet records and 686,988 market/transaction groups |
| [November analysis dataset](https://www.kaggle.com/datasets/qiuqiuslowchillchill/pm-updown-analysis-2026-11/versions/3) | Version 3; report JSON references raw version 2 |
| Local report timers | Daily 09:30, Monday 11:30, and month day 1 at 12:30, all Asia/Tokyo; pending reports retry every 30 minutes |

The converted SQLite file decreased from **547,946,496 to 378,290,176 bytes (30.96%)**, with all wallet records and business statistics preserved. Every market amount matched the prior API summary across 3,295 markets. All **340 tests** passed, and a real two-session collection resumed four pages totaling 3,209 records and 1,200 groups without changing previously committed records or group IDs.

Historical backfill continues in subsequent Daily sessions; the entire history is not yet complete. Previous cloud versions and the local historical database remain preserved, so the smaller new file does not mean the same amount of account storage has been reclaimed. See the [deployment evidence](docs/kaggle_migration.md).

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

Use Python 3.11 or newer for the production workflow and Kaggle CLI. The commands below use Bash on Linux or macOS. The `sqlite3` CLI is optional for direct SQL queries.

```bash
git clone https://github.com/slowchillchill/predict-market-analysis.git poly-market-analysis
cd poly-market-analysis
python3 -m venv .venv-btc5m
.venv-btc5m/bin/python -m pip install -r requirements-kaggle.txt
```

### Production: Kaggle statistics and local posters

After configuring the Kaggle producer and local API token using the [operations guide](docs/kaggle_migration.md), check due reports:

```bash
.venv-btc5m/bin/python scripts/polymarket_kaggle_reports.py --kind all
```

The first run records the deployment date. Pending report periods stay fixed and are retried every 30 minutes by the supplied systemd units. The scheduled times are the first checks; unavailable results remain pending. Existing assets are preserved. See the [operations guide](docs/kaggle_migration.md) for Secret authorization and timer installation.

### Manual SQLite tools

The remaining examples use the environment installed above. Collection examples write to `data/raw/manual_updown.sqlite3`; the preserved `data/raw/btc5m.sqlite3` is available for read-only reports and is protected from collection writes. Production scheduling uses the Kaggle JSON consumer above. Offline CSV/Markdown reporting uses only the Python standard library; poster rendering uses Pillow.

### 2. Collect data

To collect an explicit UTC market-end range manually:

```bash
.venv-btc5m/bin/python scripts/polymarket_updown_daily.py collect \
  --start-date 2026-09-17 --end-date 2026-09-19 \
  --db data/raw/manual_updown.sqlite3
```

This example collects markets ending on September 17 and 18, 2026 (UTC). Replace the dates as needed: the start is inclusive and the end is exclusive. Collect the preceding day too if you want day-over-day comparisons on a poster.

Press `Ctrl+C` to stop and rerun the same command to resume. Completed markets are skipped. Run only one collector per database at a time. Use the same explicit `--db` path for subsequent manual reports.

Market discovery uses Gamma `/events/keyset`, passing the returned `next_cursor` as `after_cursor` for the next page until no cursor remains. Missing scheduled slots are also queried through `/events/slug/{slug}` because an inactive market may remain available by slug while being omitted from the list endpoint. Market end dates and series membership determine inclusion regardless of active or closed flags. Slots unavailable through the detail endpoint remain coverage gaps.

### 3. Export a daily report

After collection has created the database and installed the Up/Down views:

```bash
.venv-btc5m/bin/python scripts/polymarket_updown_daily.py report \
  --date 2026-09-18 \
  --db data/raw/manual_updown.sqlite3 \
  --output-dir outputs/updown
```

For a complete day, this writes:

- `updown_daily_2026-09-18.csv` — daily totals and top-200 volume share.
- `updown_top200_2026-09-18.csv` — wallet addresses, ranks, and volumes.
- `updown_daily_2026-09-18.md` — Chinese report and coverage details.

An incomplete day produces only the coverage report. Rerun collection to address gaps.

Export all-series BUY distribution statistics separately, using only the Python standard library:

```bash
python3 scripts/polymarket_updown_daily.py distribution \
  --date 2026-11-07 --db data/raw/manual_updown.sqlite3 --output-dir outputs/updown
```

This writes `updown_buy_trade_distribution_all_series_2026-11-07.json` with reconstructed trade counts,
Shares, Up-equivalent notional, and explicit percentage denominators. It reads local data only and returns
`2` without exporting new statistics if coverage or reconstruction validation fails.

### 4. Generate an English poster and post text

```bash
.venv-btc5m/bin/python scripts/polymarket_updown_poster.py \
  --date 2026-09-18 \
  --db data/raw/manual_updown.sqlite3 \
  --output-dir outputs/posters --with-distribution
```

This writes `updown_2026-09-18.png`, `updown_2026-09-18.json`, and `updown_2026-09-18_tweet.md`. The Markdown file contains the English post body. The script generates files locally; publishing to X is a separate step. Generating the same date again replaces its output files.

`--with-distribution` also writes `updown_buy_trade_distribution_all_series_2026-09-18.png` and matching JSON.
It also updates the English caption and Chinese Douyin caption with both BUY populations. The four Douyin cards become overview, all-wallet BUY distribution, suspected-bot BUY distribution, and methods. English platform manifests include the overview and distribution images in order; see [publishing rules](docs/updown_publishing.md).
The overview JSON links both assets with SHA256 hashes. Yesterday's automated workflow enables this flag;
omitting it manually retains overview-only generation. Distribution notional uses Up-equivalent price × shares
and does not replace the overview's taker-side market volume. See [distribution definitions](docs/updown_bot_price_poster.md).

The current day must be complete. If the previous day is incomplete or its comparison value is zero, the affected day-over-day comparison displays `N/A`. The background and Inter font are bundled, so rendering needs no image-generation service or system font installation.

For these workflows, exit code `0` means success, `2` indicates incomplete coverage (argument errors also use `2`), and `130` indicates an interrupted collection. CLI help, progress messages, and daily Markdown reports are currently in Chinese; posters and post text are in English.

### 5. Generate last week's poster and post text

```bash
.venv-btc5m/bin/python scripts/polymarket_updown_weekly_poster.py --db data/raw/manual_updown.sqlite3
```

The default selects the previous complete Monday-to-Sunday week using the UTC date at startup. To select a specific week:

```bash
.venv-btc5m/bin/python scripts/polymarket_updown_weekly_poster.py \
  --week-start 2026-09-14 --db data/raw/manual_updown.sqlite3
```

This writes `updown_weekly_2026-09-14_2026-09-20.png`, matching `.json`, and `_tweet.md` files under `outputs/posters/`. It reads the local database without collecting data. All seven target days must be complete; comparisons use the preceding complete week and display `N/A` for missing or zero baselines.

Weekly wallets are deduplicated across the entire week. Suspected-bot labels persist from the first qualifying UTC market-end date onward. The weekly count is the union of active labelled wallets; volume includes their activity on and after that first date. Future labels never apply to earlier dates. Post text uses a 280-character budget.
The default weekly entry also generates the approved all-wallet/suspected-bot BUY distribution PNG/JSON, updates English and Chinese captions, and writes two English images and six Douyin cards to the publish manifest. Use `--without-distribution` for the original package.

The production weekly timer runs `polymarket_kaggle_reports.py --kind weekly` to read cloud results. Timers are local configuration and are not installed by cloning this repository. See the [operations guide](docs/kaggle_migration.md) for production scheduling and the [weekly poster guide](docs/updown_weekly_poster.md) for manual rendering.

### 6. Generate the previous month's poster and post text

```bash
.venv-btc5m/bin/python scripts/polymarket_updown_monthly_poster.py --db data/raw/manual_updown.sqlite3
```

The UTC date at startup selects the previous complete calendar month and compares it with the month before that. For example, a run on October 1, 2026 reports September against August. Year boundaries and leap years are supported. To specify a historical month, database, and output directory:

```bash
.venv-btc5m/bin/python scripts/polymarket_updown_monthly_poster.py \
  --month 2026-09 \
  --db data/raw/manual_updown.sqlite3 \
  --output-dir outputs/posters
```

This writes `updown_monthly_2026-09.png`, matching `.json`, and `_tweet.md` files. Rerunning the same month replaces its outputs. The monthly palette uses ivory, forest green, and graphite to distinguish it from the dark blue daily and burgundy-and-gold weekly posters. Post text uses the ordinary X post budget of 280 characters.

Wallets are deduplicated across the month, and daily averages use each month's actual day count. Bot labels persist from the first qualifying date onward; monthly counts use active-wallet unions and exclude activity before the first hit. Monthly share changes use percentage points. The poster includes daily trends and asset shares; series launches are disclosed in the poster and post text.
The monthly entry includes the approved BUY distribution page by default, with matching English text, Chinese text, six Douyin cards and a two-image English manifest. Both BUY populations use the same reconstruction and persistent-label rules as daily reports. `--without-distribution` keeps the original package. See [publishing rules](docs/updown_publishing.md).

Weekly and monthly reports share a query that aggregates one day at a time, with both comparison periods read in one read-only transaction. Monthly reporting does not collect data. Initialize the views through the collector and prepare both months before running it. An incomplete target month exits with code `2` without generating new files; an incomplete prior month or a zero comparison baseline displays `N/A` for the affected comparison.

**Scheduled runs:** the production `polymarket-updown-monthly.timer` checks cloud results at **12:30 Asia/Tokyo on day 1 of each month** (11:30 Beijing time, 03:30 UTC). Its service runs `polymarket_kaggle_reports.py --kind monthly`, rather than the manual SQLite command above. Due periods are retained in the local state file; a separate timer retries pending reports every 30 minutes. Existing assets are preserved. See the [operations guide](docs/kaggle_migration.md) for the daily and weekly schedules, deployment cutoff, and restart order.

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
| Market volume | Derived from each complete market's trade groups: sum the integer micro-USDC amount on the earliest saved page/row in each `group_id`. Used for poster totals, trends, and asset shares. |
| Wallet volume | Buy expenditure plus sell proceeds. Each record's `price × size` is rounded using `ROUND_HALF_UP` to integer micro-USDC before summing. This is not a single-sided market-volume metric. |
| Unique wallets | Lowercase wallet addresses deduplicated across all selected markets in the day, week, or month. Weekly and monthly counts are not sums of daily wallet counts. |
| Top 200 | Wallets ranked by aggregated volume descending, then address ascending to break ties. |
| Suspected bots in posters | First observed daily hit across all crypto Up/Down series: average trade interval ≤60 seconds and span ≥90 minutes. Labels persist from that date onward, with no future backfill. Daily, weekly and monthly statistics share the registry. This is a behavioral heuristic, not identity verification. |
| Completeness | Expected per-series market counts plus completed trade pagination. This is API collection coverage, not a full on-chain reconciliation. |

The collector uses `/v2/trades` with `taker_only=false`, `filter_type=TOKENS`, `filter_amount=1e-18`, and `limit=1000`, following cursors until `next_cursor=null`. It preserves every wallet record and assigns a short `group_id` within each market, retaining the original page/row order. Market volume and BUY-event reconstruction share these groups; choosing the first saved row as an anchor is this project's method, not a claim that the API guarantees that ordering permanently. Poster wallet amounts sum BUY only. With a distribution, the overview bot panel uses the same reconstructed BUY-event totals and Up-equivalent denominator as the distribution; the ratio measures participation coverage, not labelled wallets' own cash contribution. Classification features retain the BUY+SELL activity rule.

Production backfills history on Kaggle after fresh dates. Older cloud databases are converted and published as new versions on Kaggle; previous Dataset versions are not automatically deleted. The local historical database remains untouched. Missing or incomplete target dates cannot form a complete period, and missing comparison periods display `N/A`. Earlier valid bot hits found during backfill can update cloud statistics; local historical assets are not automatically overwritten.

The legacy [`README_BTC5M.md`](README_BTC5M.md) describes a different workflow: BTC 5-minute summaries use the **UTC market start date** and top 10 wallets. Its manual bot-analysis query also uses different date and threshold rules. Use the definitions above for the all-series workflow.

## SQL access

In Kaggle, attach a daily dataset to a separate viewing Notebook and open its `data.sqlite3` with Python's `sqlite3` module. To inspect a preserved local database manually, use the optional `sqlite3` CLI:

```bash
sqlite3 -readonly -header -column data/raw/btc5m.sqlite3 <<'SQL'
SELECT * FROM updown_coverage WHERE date_utc = '2026-09-18';
SELECT * FROM updown_series_coverage
WHERE date_utc = '2026-09-18' AND NOT is_complete;
SELECT * FROM updown_daily_summary WHERE date_utc = '2026-09-18';
SQL
```

The summary and ranking views return only complete days. Existing snapshots use their saved views; new manual databases receive these views during collection.

## Project layout and documentation

| Path | Purpose |
| --- | --- |
| [`scripts/`](scripts/) | Collection, offline reports, and poster generation |
| [`sql/`](sql/) | SQLite schema, coverage views, wallet statistics, and poster queries |
| [`tests/`](tests/) | Collection, calculation, and rendering tests |
| [`assets/`](assets/) | Poster background and bundled font; font license in [`Inter-OFL.txt`](assets/fonts/Inter-OFL.txt) |
| [`kaggle/`](kaggle/) | Public producer entrypoint and Kaggle metadata |
| `data/raw/` | Preserved local snapshots and optional manual databases; ignored by Git |
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

Install `requirements-kaggle.txt`, then run the offline test suite:

```bash
.venv-btc5m/bin/python -m unittest discover -s tests -v
```

Tests cover pagination and resumption, detail lookups, historical launch boundaries, atomic page commits, UTC date boundaries, shared-database compatibility, decimal rounding, coverage gaps, bot thresholds, calendar months across year boundaries and leap years, monthly wallet deduplication, daily-average comparisons, post length, and poster layout. When reporting a problem or proposing a change, include the command, expected behavior, and a reproducible example without local credentials or raw database files.

## License

This project is licensed under the [MIT License](LICENSE), except for third-party materials with their own licenses. MIT permits commercial use, modification, and redistribution, including in closed-source projects, provided the copyright and license notices are retained. The software is provided without warranty.

The bundled Inter font remains under the [SIL Open Font License 1.1](assets/fonts/Inter-OFL.txt). Dependencies retain their respective licenses. The MIT License does not grant rights to third-party market data or services accessed by these tools.
