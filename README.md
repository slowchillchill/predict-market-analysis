# Polymarket Crypto Updown Markets Analysis

**English** | [简体中文](README.zh-CN.md)

Public, versioned datasets of Polymarket crypto Up/Down markets, with wallet-level trade records in SQLite and daily, weekly, and monthly analysis in JSON. Collection and analysis run on Kaggle. You can query the data in a Kaggle Notebook without downloading the database to your computer.

**Start here:** [open a completed daily dataset](https://www.kaggle.com/datasets/qiuqiuslowchillchill/pm-updown-raw-2026-11-09/versions/2), attach it to a Kaggle Notebook, and run the SQL examples below. For prepared statistics, use the [analysis dataset](https://www.kaggle.com/datasets/qiuqiuslowchillchill/pm-updown-analysis-2026-11/versions/3).

## Where to find the data

All datasets are published by **`qiuqiuslowchillchill`** on Kaggle.

| Resource | Purpose | Published example |
| --- | --- | --- |
| `pm-updown-raw-YYYY-MM-DD` | One SQLite snapshot for markets ending on that UTC date | [2026-11-09, version 2](https://www.kaggle.com/datasets/qiuqiuslowchillchill/pm-updown-raw-2026-11-09/versions/2) |
| `pm-updown-analysis-YYYY-MM` | Completed result JSON and mergeable daily features | [2026-11, version 3](https://www.kaggle.com/datasets/qiuqiuslowchillchill/pm-updown-analysis-2026-11/versions/3) |
| [Public collection Notebook](https://www.kaggle.com/code/qiuqiuslowchillchill/polymarket-crypto-updown-markets-analysis) | Collection code, runs, and execution logs | Polymarket Crypto Updown Markets Analysis |
| [Source repository](https://github.com/slowchillchill/predict-market-analysis) | Schema, metric definitions, collector, and tests | This repository |

The examples above are fixed versions, verified on **November 10, 2026**. Later versions may contain corrections or more completed dates. A monthly dataset is a container; its existence does **not** mean the whole month is complete. Historical backfill is still in progress. Check the files described below for actual coverage.

**Recovery update in this branch:** raw schema v3 allows an unknown real market start to remain `NULL` and adds collection/publication recovery behavior. Deployment and validation of that update are pending. The published November 9 v2 example remains readable and is not automatically republished merely to change the schema number. Report JSON stays at version 1; grouping, wallet, and BUY metric definitions are unchanged.

## Query on Kaggle without a local database

Kaggle hosts SQLite files; this project does not expose a remote SQL endpoint. SQL runs inside your Kaggle Notebook against an attached dataset. The database stays in the Kaggle environment.

1. Open the daily dataset linked above and create a Notebook, or add the dataset through **Add Input** in an existing Notebook.
2. Attach **one** daily raw dataset for this example. Select version 2 to reproduce the example snapshot.
3. Run this Python cell. `sqlite3` is part of Python; Pandas is available in the Kaggle environment.

```python
from pathlib import Path
import sqlite3
import pandas as pd

# This example assumes exactly one daily raw dataset is attached.
db_path = next(Path('/kaggle/input').rglob('data.sqlite3'))
conn = sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True)

# List tables and SQL views.
display(pd.read_sql_query("""
    SELECT name, type FROM sqlite_schema
    WHERE type IN ('table', 'view') ORDER BY type, name
""", conn))

# Inspect column names, types, and primary-key positions.
display(pd.read_sql_query('PRAGMA table_info(trades)', conn))

# Check coverage before interpreting totals.
display(pd.read_sql_query('SELECT * FROM updown_coverage', conn))
```

Then query market volume and wallet rankings:

```python
display(pd.read_sql_query("""
    SELECT date_utc, market_volume_micro_usdc
    FROM updown_market_volume_daily ORDER BY date_utc
""", conn))

display(pd.read_sql_query("""
    SELECT wallet_rank, wallet, amount_micro_usdc
    FROM updown_wallet_ranked
    WHERE date_utc = '2026-11-09' AND wallet_rank <= 10
    ORDER BY wallet_rank
""", conn))
```

Amounts above are integer **micro-USDC**: divide by 1,000,000 for USDC. The wallet ranking sums BUY expenditure and SELL proceeds; it is not market volume or the BUY-only wallet contribution used in posters. Use Python `Decimal` when converting amounts for exact arithmetic.

For another date, attach its dataset and change the SQL date. If you attach several daily datasets, select the intended database path explicitly. Run `conn.close()` when finished. The published SQLite file may have no browser preview; the Notebook queries expose its tables and fields directly.

## Download files instead

Use the dataset page's **Download** button or the [official Kaggle CLI](https://github.com/Kaggle/kaggle-cli). The CLI requires Python 3.11+; install it in a virtual environment and follow its [authentication instructions](https://github.com/Kaggle/kaggle-cli/blob/main/docs/README.md#authentication).

```bash
python -m pip install kaggle

# List the files in the fixed raw version.
kaggle datasets files qiuqiuslowchillchill/pm-updown-raw-2026-11-09/2

# Download and extract the daily snapshot and its metadata.
kaggle datasets download \
  qiuqiuslowchillchill/pm-updown-raw-2026-11-09/2 \
  -p ./polymarket-2026-11-09 --unzip

# Download only a prepared daily result, without raw trade records.
kaggle datasets download \
  qiuqiuslowchillchill/pm-updown-analysis-2026-11/3 \
  -f daily_2026-11-09.json -p ./polymarket-results --unzip
```

The final `/2` and `/3` pin Kaggle dataset versions. Omit that suffix to request the latest version. To query a downloaded database, use the same Python examples with `db_path = Path('./polymarket-2026-11-09/data.sqlite3').resolve()`, or use a SQLite client.

To use the downloaded report without loading raw trades:

```python
import json
from decimal import Decimal
from pathlib import Path

report = json.loads(Path('./polymarket-results/daily_2026-11-09.json').read_text())
current = report['payload']['current']
print(report['status'], report['source_versions'])
print(Decimal(current['volume_micro_usdc']) / Decimal(1_000_000))
```

For this fixed version, the market volume is `9786444.035628` USDC. For other available reports, read `index.json` from the same analysis dataset version and use the filename under its `results` entry, such as `daily:2026-11-09`.

## Coverage, dates, and updates

| Assets | Market timeframes | Series |
| --- | --- | ---: |
| BTC, ETH, SOL, XRP, DOGE, BNB, HYPE | 5 minutes, 15 minutes, 1 hour, 4 hours, daily | 35 |
| ZEC | 5 minutes, 15 minutes, 4 hours | 3 |

The [series catalog](scripts/polymarket_updown_daily.py) defines 38 series. Once all series are operating, the configured schedule expects **3,295 markets per UTC day**. Historical expected counts use the configured first-market-end boundaries.

A daily dataset contains markets whose **API-provided `end_time` falls on that UTC date**. It includes all returned trade records for those markets, including trades before that date. It is not a calendar-day feed filtered by each trade's timestamp. **`markets.date_utc` is the real market start date when known**; v3 permits `NULL` when it is unknown. `event_start_time` was already nullable. A creation-related `startDate` or `end_time - 24 hours` must not be used to invent a market start. Use `date(markets.end_time)` or the `updown_*` views for the dataset's day.

Collection is scheduled daily on Kaggle. Recent closed dates take priority, followed by historical backfill. Reports become available after collection and analysis finish; the schedule is not a guarantee of publication at a fixed time. Backfill or corrections can publish new versions, including revisions to behavior-based wallet labels.

The recovery update processes fresh dates first, then repairs the **19 existing incomplete dates from 2025-03-15 through 2025-04-02**, and then continues historical backfill. Genuinely unavailable markets remain explicit gaps rather than being marked complete or filled with zero.

Its engineering defaults check every **30 minutes** for changed, committed collection state and publish an `incomplete` checkpoint only when there is a change. Collection stops after **11 hours 30 minutes** to leave publishing time before the 12-hour session limit. These are adjustable operating parameters, not Kaggle usage rules or account-protection thresholds. Uploads do not happen after every page. An interrupted run resumes from the last **successfully confirmed** dataset version; failed uploads or a forced stop can lose more than 30 minutes of unpublished progress. Unchanged monthly snapshots are not repeatedly published. See the [recovery operations](docs/kaggle_migration.md) for details.

| What you see | How to interpret it |
| --- | --- |
| `collection_status.json`: `status: complete`, with complete coverage | Finished daily snapshot under the project's collection and analysis checks |
| `status: incomplete` | Recovery checkpoint; some markets or pages still need collection, or analysis has an error |
| Completed market with no returned records | Zero recorded activity for that market |
| Missing/incomplete market, date, or comparison period | Unknown or unavailable, not zero |
| Analysis `index.json`: result entry with `status: complete` | Prepared report available in the named file |

Coverage measures expected markets, discovered markets, and completed trade pagination. It does not establish full on-chain completeness or verify every record against the blockchain. Weekly and monthly results require all target dates to be complete. Missing or zero comparison baselines appear as `N/A`.

## Files and database schema

### Daily raw dataset

| File | Contents |
| --- | --- |
| `data.sqlite3` | Market metadata, wallet trade records, collection progress, and SQL views |
| `collection_status.json` | Date, completeness, coverage counts, errors, raw schema version, group key, and volume method |
| `publication.json` | Small marker used by the recovery update to identify a publication after an uncertain response, such as HTTP 499; not a whole-database checksum requirement |
| `README.md` | Dataset description and collection definitions |
| `migration_summary.json` | Present in the converted example version; counts and comparison results for the v1-to-v2 migration |

A **completed raw schema v2 or v3** database has the three entity tables below and SQL views, including the series catalog view. The table descriptions cover v3; the fixed published example is v2, where `markets.date_utc` is `NOT NULL`. V3 makes that field nullable without changing the trade fields or grouping rules. See the [schema source](sql/btc5m_schema.sql) for exact constraints. `publication.json` belongs to the recovery update and is not implied to exist in the earlier example.

**`markets` — one row per market or expected market slot**

| Column | SQLite type / constraint | Meaning |
| --- | --- | --- |
| `slug` | TEXT, primary key | Market slug; join key for the other tables |
| `date_utc` | TEXT, nullable in v3 | Real UTC market **start** date (`YYYY-MM-DD`), or `NULL` if unknown; v2 used `NOT NULL` |
| `market_id` | TEXT | Upstream market identifier |
| `condition_id` | TEXT, UNIQUE | Upstream condition identifier; absent for an undiscovered slot |
| `event_id` | TEXT | Upstream event identifier |
| `title` | TEXT | Market question/title |
| `series_slug` | TEXT | Series identifier in the catalog |
| `event_start_time` | TEXT, nullable | Real upstream market start time, or `NULL` if unknown; never synthesized from creation time or market end |
| `end_time` | TEXT | Market end time; its UTC date selects the daily dataset |

**`collection_progress` — one progress row per collected market**

| Column | SQLite type / constraint | Meaning |
| --- | --- | --- |
| `market_slug` | TEXT, primary key, foreign key → `markets.slug` | Market being collected |
| `query_params` | TEXT | Serialized collection request parameters |
| `next_cursor` | TEXT | Cursor for the next page; `NULL` can mean not started or exhausted, so check `completed_at` |
| `committed_page` | INTEGER, NOT NULL, default 0 | Number of the last committed page |
| `completed_at` | TEXT | Collection completion time; `NULL` while unfinished |
| `last_error` | TEXT | Last recorded collection error, if any |

**`trades` — one wallet-perspective record per saved API row**

| Column | SQLite type / constraint | Meaning |
| --- | --- | --- |
| `market_slug` | TEXT, NOT NULL, foreign key → `markets.slug` | Market join key; part of primary key |
| `page_number` | INTEGER, NOT NULL | Saved API page number; part of primary key |
| `row_number` | INTEGER, NOT NULL | Position within that page; part of primary key |
| `wallet` | TEXT, NOT NULL | Lowercase wallet address reported by the API |
| `group_id` | INTEGER, NOT NULL, >0 | Market-local group of rows sharing an upstream transaction hash |
| `timestamp` | INTEGER, NOT NULL | Trade timestamp, Unix seconds |
| `side` | TEXT, NOT NULL | `BUY` or `SELL` from this wallet's perspective |
| `outcome` | TEXT, NOT NULL | Outcome label, normally `Up` or `Down` |
| `outcome_index` | INTEGER, NOT NULL | Upstream outcome index |
| `token_id` | TEXT, NOT NULL | Upstream outcome-token identifier |
| `raw_price` | TEXT, NOT NULL | Original decimal price, stored as text to preserve precision |
| `raw_size` | TEXT, NOT NULL | Original decimal share quantity |
| `amount_micro_usdc` | INTEGER, NOT NULL | `price × size`, rounded per row to micro-USDC using `ROUND_HALF_UP` |

The composite trade primary key is **`(market_slug, page_number, row_number)`**. A group can contain multiple wallet rows and multiple counterparty matches; `group_id` is not an independent fill identifier. Use **`(market_slug, group_id)`** together. Group numbers are internal to a snapshot, not stable identifiers across re-collection or dataset versions. Completed v2/v3 files do not retain transaction hashes, so those files cannot directly link a row to a chain transaction.

An incomplete snapshot may also contain `pending_group_map(market_slug, transaction_hash, group_id)` to resume grouping across pages. The collector removes each completed market's mapping and drops the empty table when finishing the day. The former `market_volume` entity table is removed in v2; `updown_market_volume_daily` below is a calculated view.

**All-series SQL views**

| View | Columns / purpose |
| --- | --- |
| `updown_target_series` | `series_slug`, `expected_markets`, `first_end_time` — catalog and configured first-market-end boundary |
| `updown_series_coverage` | `date_utc`, `series_slug`, `expected_markets`, `discovered_markets`, `completed_markets`, `committed_pages`, `is_complete` |
| `updown_coverage` | Same coverage counts aggregated by `date_utc`, without `series_slug` |
| `updown_market_volume_daily` | `date_utc`, `market_volume_micro_usdc`; `NULL` if coverage is incomplete |
| `updown_wallet_daily` | `date_utc`, `wallet`, `amount_micro_usdc` — BUY expenditure + SELL proceeds, complete days only |
| `updown_wallet_ranked` | Wallet daily fields plus `wallet_rank`, ordered by amount descending, then address ascending |
| `updown_daily_summary` | `date_utc`, `unique_wallets`, `wallet_total_micro_usdc`, `top200_micro_usdc`, `top200_share` (0–1 ratio), `market_volume_micro_usdc` |

See [view definitions](sql/updown_daily.sql). Some databases also include legacy `btc5m_*` views with different date and ranking rules; use the `updown_*` views for this dataset.

### Monthly analysis dataset

| File | Contents / usage |
| --- | --- |
| `index.json` | Available result keys, filenames, date ranges, and completion status |
| `daily_YYYY-MM-DD.json` | Finished daily report and BUY distribution |
| `weekly_YYYY-MM-DD.json` | Finished Monday-to-Sunday report; filename uses the Monday date |
| `monthly_YYYY-MM.json` | Finished calendar-month report |
| `features.sqlite3` | Daily features for aggregation and label recalculation; no individual raw trade rows |
| `progress.json` | Producer recovery state, when present; use `index.json` to discover ready reports |
| `publication.json` | Publication identity marker in snapshots written by the recovery update |

A weekly result is stored in the monthly dataset containing its **last included UTC date**. Only completed results are added to the index; future weeks or months are not implied by the filename of the container.

Report JSON uses schema version 1, separate from raw schema versions 2 and 3. Each report includes `kind`, `target_utc`, `start_date_utc`, `end_date_utc_exclusive`, `status`, `source_versions`, `payload`, and `distribution`. `source_versions` identifies the raw dataset and version for each date. Daily reports use `raw_schema_version` and `volume_method`; weekly/monthly reports use the per-date maps `raw_schema_versions` and `volume_methods`. Older completed v2 inputs may remain in these references. Read `payload` for overview statistics and `distribution` for reconstructed BUY statistics.

The [feature database](scripts/polymarket_kaggle_analysis.py) has three tables:

| Table | Columns |
| --- | --- |
| `days` | `date_utc` TEXT primary key; `features_json`, `result_json` TEXT |
| `wallet_days` | `(date_utc, wallet)` TEXT composite primary key; `trade_count`, `first_timestamp`, `last_timestamp` INTEGER; `amount_micro_usdc` TEXT containing an integer BUY-only amount |
| `daily_hits` | `(date_utc, wallet)` TEXT composite primary key for wallets meeting the daily behavior rule |

`wallet_days.trade_count` counts BUY and SELL wallet records for the behavior rule. Its amount field and the report's `payload.current.wallet_volume_micro_usdc` sum **BUY only**, unlike `updown_wallet_daily`. `daily_hits` records wallets qualifying on that date, not all wallets carrying a label from an earlier date. These tables support reports; they do not replace the raw database for trade-level research.

## Metric definitions and limits

- **Records are wallet perspectives.** Collection uses public `/v2/trades` with `taker_only=false` and preserves the returned rows. Buyer, seller, aggregate, and counterparty rows can represent related economic activity. `COUNT(*)` is a wallet-record count, not an independent matched-trade count. Summing every row's amount is not single-sided market volume, and dividing by two is not a general reconstruction rule.
- **Market volume uses a declared project method.** `wallet_group_first_row_v1` takes the earliest saved page/row in each `(market_slug, group_id)` and sums its micro-USDC amount. This preserved the earlier taker-side cash metric in the verified snapshot below. It depends on observed API row structure; it is not a documented guarantee of future API ordering or a reconciliation to Polymarket's displayed volume. See the [volume query](scripts/polymarket_market_volume_query.py).
- **The report keeps a compatibility label.** JSON `volume_basis: taker_only` names that retained cash metric. Schema v2/v3 collection uses `taker_only=false` once; the label does not mean a separate `taker_only=true` request was made.
- **BUY distribution is a separate metric.** The [reconstruction code](scripts/polymarket_updown_price_distribution.py) derives counterparty matches and normalizes prices to the Up outcome. Its notional is Up-equivalent price × shares, not wallet cash expenditure or holdings. The bot participation share measures events covered by at least one labelled BUY participant, rather than those wallets' own cash contribution.
- **Bot labels are behavior estimates.** A wallet qualifies when its observed daily records span at least 90 minutes and average at most 60 seconds apart across the covered series. Labels apply from the first qualifying observed UTC market-end date onward, never retroactively from a future date. Incomplete history makes labels provisional; discovering an earlier hit can revise reports. This does not verify who controls a wallet.
- **Wallet counts are deduplicated within the requested period.** Weekly/monthly unique wallets are not sums of daily unique-wallet counts. A wallet address is not necessarily a person.

## Historical v2 evidence you can inspect

The [2026-11-09 raw version 2](https://www.kaggle.com/datasets/qiuqiuslowchillchill/pm-updown-raw-2026-11-09/versions/2) includes `migration_summary.json`. It records the comparison with raw version 1:

| Check | Recorded result |
| --- | ---: |
| Wallet rows preserved | 1,916,447 |
| Market/transaction groups | 686,988 |
| Markets compared | 3,295 |
| Markets whose derived amount differs from the prior API summary | **0** |
| Derived market volume | 9,786,444.035628 USDC |
| SQLite file size, before → after | 547,946,496 → 378,290,176 bytes (**30.96% smaller**) |
| Existing wallet features, daily behavior hits, and BUY statistics | Unchanged |

The v2 implementation at [`c16edb8`](https://github.com/slowchillchill/predict-market-analysis/tree/c16edb8353aa78fe6229bc3e164f13788d1e1296) passed **340 tests**. A real two-session collection also resumed 3,209 wallet records across four pages and retained existing rows and group IDs. These are historical v2 results, not acceptance results for the v3 recovery update. Its final code commit, test count, and deployment evidence will be recorded after validation. See the [recorded validation](docs/kaggle_migration.md), [grouping tests](tests/test_polymarket_trade_groups.py), and [migration tests](tests/test_polymarket_kaggle_migration.py). These are project checks, not an independent audit or proof of complete upstream history.

For reproducible analysis, record the **raw and analysis dataset versions**, the raw schema version, and the volume method. Read the completion metadata and report source references with your results. Recheck those references when changing versions; internal group numbers are not cross-version join keys.

Market discovery comes from the [Polymarket Gamma API](https://gamma-api.polymarket.com); trade records come from the [public trade API](https://docs.polymarket.com/api-reference/feeds/list-trades). Coverage can be affected by upstream availability and API changes. If you find an issue, [open an issue](https://github.com/slowchillchill/predict-market-analysis/issues) with the dataset/version, date, affected market or query, and expected versus observed result.

## License and further documentation

Repository code is [MIT licensed](LICENSE). Published dataset metadata declares **CC0-1.0** for the contributed compilation. This does not remove upstream rights, notices, or applicable [Polymarket terms](https://polymarket.com/tos); see [CC0](https://creativecommons.org/publicdomain/zero/1.0/).

| Documentation | Use it for |
| --- | --- |
| [Daily analysis](docs/updown_daily.md) | Detailed discovery, coverage, and reporting rules |
| [BUY distribution definitions](docs/updown_bot_price_poster.md) | Reconstruction and distribution denominators |
| [Daily](docs/updown_poster.md), [weekly](docs/updown_weekly_poster.md), [monthly](docs/updown_monthly_poster.md) poster guides | Rendering reports and post text from project tools |
| [Kaggle operations](docs/kaggle_migration.md) | Running your own collector, scheduling, and report consumer |
| [Publishing guide](docs/updown_publishing.md) | Generated image and caption packages |
| [Legacy BTC 5-minute workflow](README_BTC5M.md) | Earlier start-date-based summaries; different from the all-series dataset |
