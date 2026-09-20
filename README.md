# Polymarket Crypto Up/Down Analysis

**English** | [简体中文](README.zh-CN.md)

Collect trade records from Polymarket crypto Up/Down markets, analyze wallet activity in SQLite, and generate daily reports and English posters for X.

The main workflow covers 38 market series and groups markets by their **UTC end date**. Collection is resumable; reporting and poster generation run offline against a read-only database connection. This repository contains the tools, tests, and poster assets; download market data locally before generating reports.

## Features

- Discover markets across eight crypto assets and five timeframes.
- Store trade records and pagination progress in SQLite, with each page committed atomically.
- Export daily wallet volume, unique wallet counts, and the top 200 wallets to CSV, alongside a Chinese Markdown report.
- Generate a 1600 × 2000 English PNG poster, supporting JSON, and English post text for X.
- Query coverage and wallet rankings through SQL views.
- Retain the original BTC 5-minute workflow for historical comparisons.

## Market coverage

The series catalog is defined in [`scripts/polymarket_updown_daily.py`](scripts/polymarket_updown_daily.py).

| Assets | Timeframes | Series |
| --- | --- | ---: |
| BTC, ETH, SOL, XRP, DOGE, BNB, HYPE | 5 minutes, 15 minutes, 1 hour, 4 hours, daily | 35 |
| ZEC | 5 minutes, 15 minutes, 4 hours | 3 |

The configured schedule expects **3,295 markets per UTC day**. Coverage is checked per series. Dates before a series existed, or changes to the platform's schedule, may not meet these expectations; incomplete days do not produce full-day monetary summaries.

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

## How to interpret the data

| Metric or behavior | Definition |
| --- | --- |
| Date | UTC date of the market's `endDate`, not its settlement confirmation or individual trade date. All returned trades for selected markets are included, including early and cross-day trades. |
| Wallet volume | Buy expenditure plus sell proceeds. Each record's `price × size` is rounded using `ROUND_HALF_UP` to integer micro-USDC before summing. This is not a single-sided market-volume metric. |
| Unique wallets | Lowercase wallet addresses deduplicated across all selected markets for that day. |
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
- [Legacy BTC 5-minute guide](README_BTC5M.md)
- [BTC collector implementation notes](docs/btc5m_daily.md)
- [Market-series inventory](reports/crypto_updown_market_types_2026-09-17.md)

## Development

Install both requirements files, then run the offline test suite:

```bash
.venv-btc5m/bin/python -m unittest discover -s tests -v
```

Tests cover pagination and resumption, atomic page commits, UTC date boundaries, shared-database compatibility, decimal rounding, coverage gaps, bot thresholds, and poster layout. When reporting a problem or proposing a change, include the command, expected behavior, and a reproducible example without local credentials or raw database files.
