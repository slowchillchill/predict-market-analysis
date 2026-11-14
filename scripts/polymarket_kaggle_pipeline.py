"""Single Kaggle producer: fresh UTC days, resumable historical days, analysis."""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import tempfile
import time
import uuid
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import polymarket_btc5m_daily as core
import polymarket_updown_daily as collector
import polymarket_kaggle_analysis as analysis
from polymarket_kaggle_store import KaggleError, KaggleStore, TITLE, OWNER, describe_exception, write_json
import polymarket_trade_groups as trade_groups


RAW_SCHEMA_VERSION = 3
VOLUME_METHOD = "wallet_group_first_row_v1"
CHECKPOINT_SECONDS = 30 * 60
STOP_AFTER_SECONDS = 11.5 * 60 * 60
SESSION_SECONDS = 12 * 60 * 60


class DiscoveryBlocked(RuntimeError):
    """An unsupported metadata response needs repair, rather than more empty days."""


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def month_of(day: date | str) -> str:
    return str(day)[:7]


def next_month(day: date) -> date:
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1)


def previous_month(day: date) -> date:
    return (day.replace(day=1) - timedelta(days=1)).replace(day=1)


def window_start(today: date) -> date:
    return today.replace(year=today.year - 3,
                         day=min(today.day, calendar.monthrange(today.year - 3, today.month)[1]))


DESCRIPTION = """# Polymarket Crypto Up/Down Data: Contents and Query Guide

Public, versioned research data for 38 Polymarket crypto Up/Down market series.
Use it to study market activity, wallet participation, BUY price distributions,
and rule-based high-frequency activity. Historical collection is still incomplete;
check the selected snapshot before drawing conclusions about a whole period.

**Start here:** [Runnable user guide and multi-month query examples](https://www.kaggle.com/code/qiuqiuslowchillchill/polymarket-updown-data-user-guide).
See also the [source, schema and methodology](https://github.com/slowchillchill/predict-market-analysis)
and [Chinese documentation](https://github.com/slowchillchill/predict-market-analysis/blob/main/README.zh-CN.md).

## Which dataset should I use?

All datasets below belong to `qiuqiuslowchillchill`.

| Dataset name | Contents | Best for |
| --- | --- | --- |
| `pm-updown-raw-YYYY-MM-DD` | One raw SQLite snapshot for markets ending on that UTC date | Market-level or wallet-level research and custom trade queries |
| `pm-updown-analysis-YYYY-MM` | Completed daily features and available report JSON files | Trends, wallet rankings, and statistics across days or months |

Each raw daily dataset contains:

- `data.sqlite3`: `markets` metadata, `collection_progress` pagination state,
  and `trades` wallet-perspective records; also `updown_*` coverage and summary views.
- `collection_status.json`: completeness, coverage, errors, schema and calculation method.
- `README.md`: the documentation shipped with that particular snapshot.
- Some versions also include `publication.json` or `migration_summary.json`.

Monthly analysis datasets can contain the following files. `features.sqlite3`,
`index.json`, and daily reports appear after completed daily results are available;
an initial snapshot may contain recovery state only.

- `features.sqlite3`: three tables. `days` stores completed daily feature/report JSON;
  `wallet_days` stores per-day wallet activity and BUY amounts; `daily_hits` records
  wallets that satisfy the behavior rule on that day.
- `index.json`: the reports available in this snapshot. Only completed report periods appear.
- `daily_YYYY-MM-DD.json`, and completed weekly/monthly report JSON when available.
- Some months also contain global `progress.json`, publication markers, and
  `social.archive.json` with generated text and generation state.

Historical features go into the month of the data date. Global progress can be
stored in the current operating month; it does not make that month's feature
database an archive of every historical month. A cross-month weekly report is
stored in the month containing its last included date.

## Query inside Kaggle

Create your own Notebook, choose **Add Input**, and attach the required datasets.
Select and record their versions. SQLite is queried inside the Notebook; this
project does not provide a remotely connectable SQL endpoint. Do not run the
collection Notebook just to read the data.

For one raw daily dataset, attach only that daily dataset and run:

```python
import json
import sqlite3
from pathlib import Path
from contextlib import closing

paths = list(Path('/kaggle/input').rglob('data.sqlite3'))
assert len(paths) == 1, 'Attach exactly one raw daily snapshot for this example'
db = paths[0].resolve()
status = json.loads((db.parent / 'collection_status.json').read_text())
assert status['status'] == 'complete', status.get('error', 'Incomplete snapshot')
with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as conn:
    print(conn.execute('SELECT * FROM updown_coverage').fetchall())
    print(conn.execute(
        'SELECT date_utc, market_volume_micro_usdc '
        'FROM updown_market_volume_daily'
    ).fetchall())
```

Inspect fields with `PRAGMA table_info(markets)` and `PRAGMA table_info(trades)`.
The raw `updown_wallet_daily` and `updown_wallet_ranked` views use BUY + SELL
wallet amounts. This differs from the BUY-only amount in analysis `wallet_days`.

For prepared statistics, read `index.json` and the selected report JSON.
For custom period statistics, query the monthly `features.sqlite3` tables:

```sql
SELECT date_utc,
       json_extract(features_json, '$.market_volume.volume_micro_usdc')
         AS market_volume_micro_usdc
FROM days
WHERE date_utc >= '2026-11-09' AND date_utc < '2026-11-13'
ORDER BY date_utc;
```

This example uses the half-open interval `[2026-11-09, 2026-11-13)`, including
November 9 through 12. It does not assert that any arbitrary range is complete.

## Query a quarter, a year, or a custom date range

1. Attach or download each required month's `features.sqlite3`, choosing exactly
   one snapshot version per month. Avoid scanning all raw trade files for metrics
   already available as daily features.
2. Select daily rows in `[start_date, end_date)`. Compare the returned dates with
   every expected date. Missing dates are unknown, not zero. The linked guide
   refuses to emit full-period totals if a date is missing or duplicated.
3. Sum daily market amounts and per-asset amounts. Group wallet BUY amounts by
   address, then rank the interval totals. Count distinct wallet addresses across
   the entire interval rather than adding daily wallet counts.
4. Recalculate shares and averages from combined numerators and denominators.
   Do not average daily percentages or sum daily Top 200 lists.
5. Keep input Dataset versions and each report's `source_versions` with the result.

`wallet_days.amount_micro_usdc` is an integer stored as TEXT and represents BUY
amount only. Use Python `int` for accumulation and decimal/integer conversion for
display. Its `trade_count`, first timestamp and last timestamp describe BUY + SELL
wallet records. These counts are not reconstructed fills. `daily_hits` is neither
the full historically classified bot population nor exclusively first-time hits.
Bot analysis must use consistent label rules and history across all input dates.

The standard report generator supports complete UTC weeks and calendar months.
The user guide supplies a separate read-only example for arbitrary date ranges.
Detailed series filters, individual markets and transaction-time analysis require
the relevant raw daily databases; aggregated wallet features cannot recover every
trade-level dimension.

## Download a fixed version

After configuring the official Kaggle CLI, this command downloads only the
feature database from a known snapshot, rather than all raw trade files:

```bash
kaggle datasets download \\
  qiuqiuslowchillchill/pm-updown-analysis-2026-11/383 \\
  -f features.sqlite3 -p ./analysis-2026-11-v383 --unzip
```

The final `/383` pins the Dataset version. Choose appropriate versions for your
research; omitting it requests the latest snapshot. Keep each month's files in a
separate folder. Kaggle Dataset versions and SQLite schema versions are different.

## Completeness and measurement rules

- Dates mean the UTC date of the upstream market `end_time`, not the trade's
  timestamp or market start date. All available trades of those markets are
  retained, including trades on other dates. For transaction-time research, filter
  `trades.timestamp` and include the necessary cross-date market partitions.
- Use raw `collection_status.json.status == 'complete'` for complete-day work.
  Pagination can be complete while statistical reconstruction still fails, so
  `coverage.is_complete == 1` alone is insufficient. Incomplete files are recovery
  snapshots. Check errors and coverage before using them as partial samples.
- Only completed daily results enter `days`. Missing days are not zero-activity
  days. Historical progress advancing does not mean all earlier days are complete.
- `coverage_policy` records the coverage rule. The current official-series rule
  requires complete directory pagination, planned slots and trade pagination for
  all discovered conditions. Older completed inputs can use a legacy rule.
  Completeness is an API/project check, not full on-chain reconciliation.
- Each `trades` row is a wallet-perspective record, not necessarily an independent
  fill. `group_id` is scoped to one market and one daily snapshot version; it is
  neither a transaction hash nor a global fill identifier. Do not deduplicate by
  price/size, sum wallet rows as single-sided market volume, or divide them by two.
- Project market volume uses `wallet_group_first_row_v1`: the earliest stored
  page/row amount in each `(market_slug, group_id)` group, then summed. It is a
  project method, not a guarantee of upstream ordering. Wallet BUY amounts and
  reconstructed BUY-event Up-equivalent notional are different measures.
- One USDC equals 1,000,000 micro USDC. Raw prices and sizes are decimal strings;
  row amounts use ROUND_HALF_UP before summation. Preserve integer/Decimal precision.
- Raw schema v3 permits unknown actual market start times/dates and settlement
  metadata to remain NULL. Check the selected file's schema before querying optional
  columns. Distinct real conditions sharing an interval are retained.

## Sources and attribution

Sources: https://gamma-api.polymarket.com and
https://data-api.polymarket.com/v2/trades, collected with `taker_only=false`.
Source terms and notices remain applicable: https://polymarket.com/tos .
The CC0 dedication applies to the contributor's rights in this arranged research
dataset. Code, definitions and issue reporting:
https://github.com/slowchillchill/predict-market-analysis .
When reporting a problem, include the dataset version, date, market identifier
and minimal query; never include credentials.
"""


class Pipeline:
    def __init__(self, store: KaggleStore, work_dir: Path, *, today=utc_today, client=None,
                 clock=None, started_at: float | None = None,
                 checkpoint_seconds: float = CHECKPOINT_SECONDS,
                 stop_after_seconds: float = STOP_AFTER_SECONDS,
                 session_seconds: float = SESSION_SECONDS):
        self.store, self.work_dir, self.today = store, Path(work_dir), today
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.clock = clock or time.monotonic
        self.started_at = self.clock() if started_at is None else started_at
        self.stop_at = self.started_at + stop_after_seconds
        self.deadline = self.started_at + session_seconds
        self.checkpoint_seconds = checkpoint_seconds
        self._last_checkpoint = self.clock()
        self._active_day = None
        self._restored_versions: dict[str, int | None] = {}
        self._restored_snapshots: dict[str, tuple] = {}
        self.store.deadline = self.deadline
        self.client = client or core.Client(None)
        self.client.deadline = self.stop_at
        self.current_month = month_of(today())
        self.months: dict[str, Path] = {}
        self._month_versions: dict[str, int | None] = {}
        self.changed_months: set[str] = set()
        self.attempted: set[str] = set()
        self.stopping = False
        folder = self.month(self.current_month)
        state_path = folder / "progress.json"
        had_current_progress = state_path.exists()
        if not state_path.exists():
            previous = [slug for slug in self.store.list_slugs("pm-updown-analysis-")
                        if slug.removeprefix("pm-updown-analysis-") < self.current_month]
            if previous:
                prior = previous[-1]
                version = self.store.version(prior)
                if version:
                    self.store.download_file(prior, "progress.json", version, state_path)
        self.state = json.loads(state_path.read_text()) if state_path.exists() else {
            "schema_version": 1, "rollout_date_utc": str(today()), "raw_days": {},
            "first_seen": {}, "history": None, "dirty_dates": [], "history_metadata": {},
        }
        self._saved_state = self._state_signature() if had_current_progress else None
        self._saved_month = self.current_month if had_current_progress else None
        self.state.setdefault("dirty_dates", [])
        if self.state.get("recovery_schema_version", 0) < RAW_SCHEMA_VERSION:
            history = self.state.get("history")
            first_fresh = str(date.fromisoformat(self.state["rollout_date_utc"]) - timedelta(days=1))
            pending = {stamp for stamp, raw in self.state["raw_days"].items()
                       if stamp < first_fresh and raw.get("status") != "complete"}
            if history:
                cursor = history["next_date_utc"]
                pending.update(stamp for stamp in history.get("gaps", {}) if stamp <= cursor)
                if (cursor <= history["target_end_date_utc"] and cursor < first_fresh
                        and self.state["raw_days"].get(cursor, {}).get("status") != "complete"):
                    pending.add(cursor)
                pending = {stamp for stamp in pending if stamp <= cursor}
            self.state["repair_queue"] = sorted(pending)
            self.state["recovery_schema_version"] = RAW_SCHEMA_VERSION

    def _state_signature(self) -> str:
        return json.dumps({key: value for key, value in self.state.items() if key != "updated_at_utc"},
                          sort_keys=True, separators=(",", ":"), allow_nan=False)

    def _stop_due(self) -> bool:
        if self.clock() >= self.stop_at:
            self.stopping = True
        return self.stopping

    def _fresh_due(self) -> bool:
        return str(self.today() - timedelta(days=1)) not in self.attempted

    def _before_statistics(self) -> None:
        if self._stop_due():
            raise core.CollectionPaused("Session stopping; statistics will resume from the raw checkpoint")

    def month(self, stamp: str) -> Path:
        if stamp not in self.months:
            folder = self.work_dir / "analysis" / stamp
            self._month_versions[stamp] = self.store.restore(f"pm-updown-analysis-{stamp}", folder)
            from polymarket_kaggle_social import restore_cloud_social
            restore_cloud_social(folder)
            self.months[stamp] = folder
        return self.months[stamp]

    def prepare_cloud_text(self) -> None:
        """Import the fixed migration seed and restore all due text periods."""
        from polymarket_kaggle_reports import due_periods
        from polymarket_kaggle_social import SEED_ARCHIVE_FILENAME, import_cloud_text_state
        rollout = date.fromisoformat(self.state["rollout_date_utc"])
        for period in due_periods(rollout, datetime.now(timezone.utc)):
            self.month(period.month)
        config = Path(__file__).resolve().parents[1] / "config/kaggle-social-seed.json"
        if config.exists() and not self.state.get("cloud_social_seed"):
            source = json.loads(config.read_text())
            if (set(source) != {"dataset", "version"} or not isinstance(source["dataset"], str)
                    or source["dataset"].split("/")[0] != self.store.owner
                    or type(source["version"]) is not int or source["version"] < 1):
                raise ValueError("Invalid fixed cloud social migration source")
            folder = self.work_dir / "social-seed"
            for filename in ("seed.json", SEED_ARCHIVE_FILENAME):
                self.store.download_file(source["dataset"], filename, source["version"], folder / filename)
            seed = json.loads((folder / "seed.json").read_text())
            for stamp in seed["months"]:
                result = import_cloud_text_state(self.month(stamp), folder, month=stamp)
                if result["changed"]:
                    self.changed_months.add(stamp)
            # Publish every affected month before recording adoption globally.
            self.save()
            self.state["cloud_social_seed"] = source
        self.save()

    def sources_for(self, start: date, end: date) -> list[Path]:
        paths = []
        month = start.replace(day=1)
        while month < end:
            path = self.month(month_of(month)) / "features.sqlite3"
            if path.exists():
                paths.append(path)
            month = next_month(month)
        return paths

    def history_metadata(self) -> dict:
        history = self.state.get("history")
        observed = sorted(day for day, raw in self.state["raw_days"].items() if raw["status"] == "complete")
        return {
            "requested_start_date_utc": history["start_date_utc"] if history else None,
            "requested_end_date_utc": history["target_end_date_utc"] if history else None,
            "observed_dates": observed,
            "observed_start_date_utc": observed[0] if observed else None,
            "observed_end_date_utc": observed[-1] if observed else None,
            "history_pending": history is None or not history.get("finished", False)
                               or bool(history.get("gaps")),
        }

    def save(self) -> None:
        from polymarket_kaggle_social import pack_cloud_social, render_cloud_month
        self.current_month = month_of(self.today())
        current = self.month(self.current_month)
        signature = self._state_signature()
        if signature != self._saved_state or self._saved_month != self.current_month:
            self.state["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
            write_json(current / "progress.json", self.state)
            self.changed_months.add(self.current_month)
        for stamp in sorted(self.changed_months, key=lambda s: (s == self.current_month, s)):
            pack_cloud_social(self.month(stamp))
            self._month_versions[stamp] = self.store.publish(f"pm-updown-analysis-{stamp}", self.month(stamp),
                title=f"Polymarket Updown Analysis {stamp}", description=DESCRIPTION,
                message="Update completed research results and resumable collection progress")
            self.changed_months.discard(stamp)
        for stamp, folder in sorted(self.months.items()):
            version = self._month_versions.get(stamp)
            if not version or not (folder / "index.json").exists():
                continue
            slug = f"pm-updown-analysis-{stamp}"
            result = render_cloud_month(folder, month=stamp,
                source_reference={"dataset": self.store.ref(slug), "version": version},
                rollout_date=date.fromisoformat(self.state["rollout_date_utc"]))
            if result["changed"]:
                self.changed_months.add(stamp)
                pack_cloud_social(folder)
                self._month_versions[stamp] = self.store.publish(slug, folder,
                    title=f"Polymarket Updown Analysis {stamp}", description=DESCRIPTION,
                    message="Save cloud-generated text and resumable publication state")
                self.changed_months.discard(stamp)
        self._saved_state, self._saved_month = signature, self.current_month

    def put_bundle(self, bundle: dict) -> None:
        # Weekly results live in the month of their final included UTC date.
        final_day = date.fromisoformat(bundle["end_date_utc_exclusive"]) - timedelta(days=1)
        stamp = month_of(final_day)
        folder = self.month(stamp)
        key = f"{bundle['kind']}:{bundle['target_utc']}"
        filename = f"{bundle['kind']}_{bundle['target_utc']}.json"
        bundle_path = folder / filename
        bundle_changed = not bundle_path.exists() or json.loads(bundle_path.read_text()) != bundle
        if bundle_changed:
            write_json(bundle_path, bundle)
        index_path = folder / "index.json"
        index = json.loads(index_path.read_text()) if index_path.exists() else {"schema_version": 1, "results": {}}
        entry = {"filename": filename, "status": "complete",
                 "start_date_utc": bundle["start_date_utc"],
                 "end_date_utc_exclusive": bundle["end_date_utc_exclusive"]}
        index_changed = index["results"].get(key) != entry
        if index_changed:
            index["results"][key] = entry
            write_json(index_path, index)
        if bundle_changed or index_changed:
            self.changed_months.add(stamp)

    def _previous_day(self, day: date) -> dict | None:
        previous = day - timedelta(days=1)
        bundle = analysis.load_day(self.sources_for(previous, day), previous)
        return bundle["payload"]["current"] if bundle else None

    def _raw_path(self, day: date, reference: dict | None, *, latest: bool = False) -> Path:
        folder = self.work_dir / "raw" / str(day)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "data.sqlite3"
        if not path.exists():
            if latest:
                self._restored_snapshots.pop(str(day), None)
                # Raw publication can succeed before the monthly progress
                # publication. Resume its latest durable version, not the
                # possibly stale reference in progress.json. restore waits
                # for a processing Dataset version before downloading it.
                version = self.store.restore(f"pm-updown-raw-{day}", folder)
                self._restored_versions[str(day)] = version
                if reference and (version is None or version < reference["version"]):
                    path.unlink(missing_ok=True)
                    raise KaggleError(f"Cannot resume {day} from an older or missing raw Dataset version")
                if version and path.exists():
                    metadata_path = folder / "collection_status.json"
                    marker_path = folder / "publication.json"
                    if metadata_path.exists() and marker_path.exists():
                        metadata = json.loads(metadata_path.read_text())
                        marker = json.loads(marker_path.read_text())
                        if (metadata.get("date_utc") == str(day)
                                and metadata.get("schema_version") == RAW_SCHEMA_VERSION
                                and marker.get("dataset") == self.store.ref(f"pm-updown-raw-{day}")
                                and marker.get("publication_id")):
                            self._restored_snapshots[str(day)] = (version, metadata, self._database_digest(path))
            elif reference:
                self.store.download_file(reference["slug"], "data.sqlite3", reference["version"], path)
        return path

    @staticmethod
    def _database_digest(path: Path) -> str:
        """Compare actual persisted values, independent of SQLite page layout.

        A restored snapshot may have reached Kaggle before progress.json did.
        Directory proofs must still be refreshed; only identical schema, rows,
        and collection metadata permit reuse of that confirmed version.
        """
        digest = hashlib.sha256()
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
            digest.update(str(db.execute("PRAGMA user_version").fetchone()[0]).encode())
            objects = db.execute("SELECT type,name,sql FROM sqlite_master "
                                 "WHERE sql IS NOT NULL ORDER BY type,name").fetchall()
            for kind, name, sql in objects:
                digest.update(repr((kind, name, sql)).encode())
                if kind != "table":
                    continue
                quoted = '"' + name.replace('"', '""') + '"'
                columns = db.execute(f"PRAGMA table_info({quoted})").fetchall()
                keys = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
                order = ','.join('"' + key.replace('"', '""') + '"' for key in keys) or "rowid"
                for row in db.execute(f"SELECT * FROM {quoted} ORDER BY {order}"):
                    digest.update(repr(row).encode())
                    digest.update(b"\n")
        return digest.hexdigest()

    def _first_ends(self) -> dict | None:
        history = self.state.get("history")
        return history["series_first_end"] if history else None

    def _should_yield(self) -> bool:
        return self._stop_due() or self._fresh_due()

    @staticmethod
    def _raw_metadata(day: date, coverage: dict, status: str, error: str | None,
                      result: dict) -> dict:
        return {
            "date_utc": str(day), "status": status, "coverage": coverage, "error": error,
            "date_basis": "market_end_utc", "schema_version": RAW_SCHEMA_VERSION,
            "group_key": ["market_slug", "group_id"], "volume_method": VOLUME_METHOD,
            "coverage_policy": result.get("coverage_policy", "legacy_nominal_v1"),
            "discovery_errors": result.get("discovery_errors", []),
            "structural_errors": result.get("structural_errors", []),
        }

    def _record_raw(self, day: date, version: int, metadata: dict) -> dict:
        reference = {
            "slug": f"pm-updown-raw-{day}", "version": version,
            "status": metadata["status"], "coverage": metadata["coverage"],
            "raw_schema_version": RAW_SCHEMA_VERSION, "volume_method": VOLUME_METHOD,
            "coverage_policy": metadata.get("coverage_policy", "legacy_nominal_v1"),
            "error": metadata["error"],
            "discovery_errors": metadata.get("discovery_errors", []),
            "structural_errors": metadata.get("structural_errors", []),
        }
        self.state["raw_days"][str(day)] = reference
        self._restored_versions[str(day)] = version
        return reference

    @staticmethod
    def _checkpoint_signature(db: sqlite3.Connection, result: dict) -> tuple:
        # Directory and slot proofs are persistent views; DDL changes do not
        # increment total_changes, but must still trigger a changed checkpoint.
        return (db.total_changes, db.execute("PRAGMA schema_version").fetchone()[0],
                json.dumps(result, sort_keys=True))

    def _checkpoint_active(self) -> bool:
        active = self._active_day
        if active is None:
            return False
        db = active["db"]
        if db.in_transaction:
            raise RuntimeError("A checkpoint requires a committed page, not an active transaction")
        signature = self._checkpoint_signature(db, active["result"])
        if signature == active["last_signature"]:
            return False
        day = active["day"]
        coverage = collector.coverage(db, str(day))
        errors = active["result"].get("structural_errors", []) or active["result"].get("discovery_errors", [])
        error = errors[0]["message"] if errors else None
        metadata = self._raw_metadata(day, coverage, "incomplete", error, active["result"])
        # A checkpoint has its own immutable copy. Do not finalize, clear group
        # mappings, remove the active work database, or advance the history date.
        checkpoint_root = self.work_dir / "checkpoints"
        checkpoint_root.mkdir(exist_ok=True)
        # Keep this exact snapshot and its publication marker if either upload
        # confirmation or the monthly progress publication fails. A fresh
        # session still restores cloud Datasets; retained output is evidence,
        # not an instruction to repeat an uncertain upload.
        folder = Path(tempfile.mkdtemp(prefix=f"{day}-", dir=checkpoint_root))
        try:
            with closing(sqlite3.connect(folder / "data.sqlite3")) as target:
                db.backup(target)
            write_json(folder / "collection_status.json", metadata)
            (folder / "README.md").write_text(DESCRIPTION)
            version = self.store.publish(f"pm-updown-raw-{day}", folder,
                title=f"Polymarket Updown Raw {day}", description=DESCRIPTION,
                message=f"{day}: recoverable collection checkpoint")
            self._record_raw(day, version, metadata)
            self.save()
        except BaseException:
            print(f"Retained checkpoint for publication recovery: {folder}", flush=True)
            raise
        shutil.rmtree(folder)
        active["last_signature"] = signature
        print(f"Checkpointed {day}: version {version}, {coverage['committed_pages']} committed pages", flush=True)
        return True

    def _collection_should_yield(self, *, historical: bool) -> bool:
        if self._stop_due() or (historical and self._fresh_due()):
            return True
        if self.clock() - self._last_checkpoint >= self.checkpoint_seconds:
            self._checkpoint_active()
            self._last_checkpoint = self.clock()
        return False

    def process_day(self, day: date, *, historical: bool = False) -> None:
        stamp = str(day)
        self.attempted.add(stamp)
        print(f"Collecting {stamp} ({'historical' if historical else 'fresh'})", flush=True)
        slug = f"pm-updown-raw-{stamp}"
        reference = self.state["raw_days"].get(stamp)
        path = self._raw_path(day, reference, latest=True)
        db = collector.connect_database(path, series_first_end=self._first_ends())
        paused = None
        collection_error = None
        result = {"discovery_errors": [], "structural_errors": [],
                  "coverage_policy": collector.coverage_policy(db)}
        self._active_day = {"day": day, "db": db, "result": result,
                            "last_signature": self._checkpoint_signature(db, result)}
        try:
            collector.collect(db, self.client, day, day + timedelta(days=1),
                should_yield=lambda: self._collection_should_yield(historical=historical), result=result)
        except (core.CollectionPaused, core.RateLimitError, KeyboardInterrupt) as exc:
            paused = exc
            self._stop_due()
        except KaggleError:
            # An uncertain checkpoint publication is reconciled by the store.
            # If reconciliation failed, do not blindly publish it a second time.
            raise
        except Exception as exc:
            collection_error = f"{type(exc).__name__}: {exc}"
        finally:
            coverage = collector.coverage(db, stamp)
            db.close()
            self._active_day = None
        structural_errors = result.get("structural_errors", [])
        errors = structural_errors or (result.get("discovery_errors", []) if not coverage["is_complete"] else [])
        if errors and not collection_error:
            collection_error = "; ".join(error["message"] for error in errors[:5])
        complete = bool(coverage["is_complete"])
        features = bundle = labels = None
        changed = {}
        if complete and not paused and not collection_error:
            try:
                self._before_statistics()
                features = analysis.extract_day_features(path, day)
                self._before_statistics()
                labels, changed = analysis.update_first_seen(self.state["first_seen"], features,
                    history_metadata=self.history_metadata())
                bundle = analysis.export_day(path, day, labels, history_metadata=labels.metadata,
                                              previous=self._previous_day(day))
                self._before_statistics()
                with closing(sqlite3.connect(path)) as final_db:
                    trade_groups.finalize_database(final_db)
            except core.CollectionPaused as exc:
                paused = exc
                complete = False
            except Exception as exc:
                collection_error = f"Statistics: {type(exc).__name__}: {exc}"
                complete = False
        else:
            complete = False
        status = "complete" if complete else "incomplete"
        metadata = self._raw_metadata(day, coverage, status,
                                     collection_error or (str(paused) if paused else None), result)
        write_json(path.parent / "collection_status.json", metadata)
        (path.parent / "README.md").write_text(DESCRIPTION)
        restored = self._restored_snapshots.get(stamp)
        if (restored and restored[0] >= (reference or {}).get("version", 0)
                and restored[1] == metadata and restored[2] == self._database_digest(path)):
            version = restored[0]
            print(f"Reusing confirmed raw {stamp}: version {version}; snapshot unchanged", flush=True)
        else:
            version = self.store.publish(slug, path.parent, title=f"Polymarket Updown Raw {stamp}",
                                         description=DESCRIPTION, message=f"{stamp}: {status}")
        self._record_raw(day, version, metadata)
        if complete:
            self.state["first_seen"] = dict(labels)
            self.state["history_metadata"] = labels.metadata
            bundle["source_versions"] = {stamp: {"dataset": self.store.ref(slug), "version": version}}
            bundle["raw_schema_version"] = RAW_SCHEMA_VERSION
            bundle["volume_method"] = VOLUME_METHOD
            bundle["coverage_policy"] = metadata["coverage_policy"]
            analysis.save_day(self.month(month_of(day)) / "features.sqlite3", features, bundle, first_seen=labels)
            self.put_bundle(bundle)
            if changed:
                dirty = set(self.state["dirty_dates"])
                stores = [self.month(month) / "features.sqlite3" for month in
                          sorted({month_of(other) for other in self.state["raw_days"]})]
                dirty.update(other for other in analysis.affected_dates(stores, changed) if other != stamp)
                self.state["dirty_dates"] = sorted(dirty)
        history = self.state.get("history")
        structural = bool(result.get("structural_errors"))
        if historical and history and not paused:
            if complete:
                history["gaps"].pop(stamp, None)
            else:
                history["gaps"][stamp] = collection_error or "Missing markets or unfinished trade pages"
            # The raw snapshot is confirmed. Preserve incomplete work as a
            # gap, but let the scan reach other dates and markets. Each gap is
            # retried at most once in this session, including repair entries.
            history["next_date_utc"] = max(history["next_date_utc"], str(day + timedelta(days=1)))
            self.state["repair_queue"] = [other for other in self.state.get("repair_queue", [])
                                          if other != stamp]
        # Published raw files are the durable copy; do not accumulate daily raw
        # databases in the Notebook output or the local consumer's filesystem.
        if paused is not None:
            self.save()
            shutil.rmtree(path.parent)
            if historical and not self.stopping and isinstance(paused, core.CollectionPaused):
                self.attempted.discard(stamp)
            raise paused
        if structural:
            self.save()
            shutil.rmtree(path.parent)
            kind = "Historical" if historical else "Fresh"
            print(f"{kind} day {stamp} remains incomplete: {collection_error}. "
                  "Keeping its errors for retry; collection of other dates will continue.", flush=True)
            return
        # Save once after derived results have been updated. If fresh work is
        # due, keep the completed raw reference durable before yielding.
        if self._should_yield():
            self.save()
            shutil.rmtree(path.parent)
            raise core.CollectionPaused("Fresh UTC day is due or producer is stopping")
        self.recompute_dirty()
        if complete:
            self._before_statistics()
            self.refresh_periods(day)
            self.refresh_daily_comparison(day + timedelta(days=1))
            self.recompute_dirty()
        self.save()
        shutil.rmtree(path.parent)

    def refresh_daily_comparison(self, day: date) -> None:
        path = self.month(month_of(day)) / "features.sqlite3"
        bundle = analysis.update_day_previous(path, day, self._previous_day(day))
        if bundle:
            self.put_bundle(bundle)

    def recompute_dirty(self) -> None:
        while self.state["dirty_dates"]:
            if self._should_yield():
                raise core.CollectionPaused("Fresh UTC day is due or producer is stopping")
            stamp = self.state["dirty_dates"][0]
            reference = self.state["raw_days"].get(stamp)
            if not reference or reference["status"] != "complete":
                self.state["dirty_dates"].pop(0)
                continue
            day = date.fromisoformat(stamp)
            path = self._raw_path(day, reference)
            self._before_statistics()
            features = analysis.extract_day_features(path, day)
            self._before_statistics()
            bundle = analysis.export_day(path, day, self.state["first_seen"],
                history_metadata=self.history_metadata(), previous=self._previous_day(day),
                source_reference={"dataset": self.store.ref(reference["slug"]), "version": reference["version"]})
            bundle["raw_schema_version"] = reference.get("raw_schema_version", 1)
            bundle["volume_method"] = reference.get("volume_method", "legacy_taker_api")
            bundle["coverage_policy"] = reference.get("coverage_policy", "legacy_nominal_v1")
            analysis.save_day(self.month(month_of(day)) / "features.sqlite3", features, bundle,
                              first_seen=self.state["first_seen"])
            self.put_bundle(bundle)
            self.state["dirty_dates"].pop(0)
            self.refresh_daily_comparison(day + timedelta(days=1))
            self.refresh_periods(day)
            self.save()
            shutil.rmtree(path.parent)

    def refresh_periods(self, day: date, *, following: bool = True) -> None:
        week = day - timedelta(days=day.weekday())
        month = day.replace(day=1)
        for kind, start, end in (("weekly", week, week + timedelta(days=7)),
                                 ("monthly", month, next_month(month))):
            if end > self.today():
                continue
            previous_start = start - timedelta(days=7) if kind == "weekly" else previous_month(start)
            if any(str(previous_start) <= stamp < str(end) for stamp in self.state["dirty_dates"]):
                continue
            paths = self.sources_for(start, end)
            prior = None
            try:
                self._before_statistics()
                prior = analysis.export_period(self.sources_for(previous_start, start), previous_start,
                    start, kind, self.state["first_seen"], history_metadata=self.history_metadata())["payload"]["current"]
            except analysis.IncompleteDay:
                pass
            try:
                self._before_statistics()
                bundle = analysis.export_period(paths, start, end, kind, self.state["first_seen"],
                                               history_metadata=self.history_metadata(), previous=prior)
            except analysis.IncompleteDay:
                continue
            self.put_bundle(bundle)
        if following:
            # Filling a previous period must also refresh an already available
            # next period's comparison, even when its own raw days did not change.
            self.refresh_periods(week + timedelta(days=7), following=False)
            self.refresh_periods(next_month(month), following=False)

    def discover_history(self) -> None:
        """Freeze earliest discovered market-end bounds, without local data."""
        if self.state.get("history"):
            return
        discovery = self.state.setdefault("history_discovery", {
            "window_start_utc": str(window_start(self.today())),
            "end_date_utc_exclusive": str(self.today()), "series_first_end": {},
        })
        lower = date.fromisoformat(discovery["window_start_utc"])
        upper = date.fromisoformat(discovery["end_date_utc_exclusive"])
        first_ends = discovery["series_first_end"]
        errors = discovery.setdefault("errors", {})
        legacy_error = discovery.pop("error", None)
        if legacy_error:
            errors[legacy_error["series_slug"]] = legacy_error
        for series in collector.TARGET_SERIES:
            if series in first_ends:
                errors.pop(series, None)
                continue
            if self._should_yield():
                raise core.CollectionPaused("Fresh UTC day is due")
            # A failed page may contain the earliest contract. Restart that
            # series from its first page rather than resume past bad metadata.
            if series in errors:
                discovery.pop("in_progress", None)
            resumed = discovery.get("in_progress", {})
            earliest = (collector.utc_time(resumed["earliest_end_time"])
                        if resumed.get("series_slug") == series and resumed.get("earliest_end_time") else None)
            boundary_error = None
            seen_cursors = set()
            try:
                rows = self.client.get(collector.GAMMA_SERIES, {"slug": series})
                series_id = next(row["id"] for row in rows if row["slug"] == series)
                params = {
                    "series_id": series_id, "end_date_min": collector.midnight(lower).isoformat(),
                    "end_date_max": collector.midnight(upper).isoformat(),
                    "limit": collector.EVENT_PAGE_SIZE, "order": "endDate", "ascending": "true",
                }
                if resumed.get("series_slug") == series and resumed.get("next_cursor"):
                    params["after_cursor"] = resumed["next_cursor"]
                    seen_cursors.add(resumed["next_cursor"])
            except core.RateLimitError:
                raise
            except (core.FetchError, KeyError, TypeError, ValueError, StopIteration) as exc:
                boundary_error = exc
            while boundary_error is None:
                if self._should_yield():
                    raise core.CollectionPaused("Fresh UTC day is due")
                try:
                    page = self.client.get(collector.GAMMA_EVENTS_KEYSET, params)
                    for event in page["events"]:
                        for market in collector.iter_target_markets(event, series, lower, upper):
                            ended = collector.utc_time(market["endDate"])
                            earliest = min(earliest, ended) if earliest else ended
                    cursor = page.get("next_cursor")
                    if cursor:
                        if cursor in seen_cursors:
                            raise collector.DiscoveryError("Historical boundary pagination repeated a cursor")
                        seen_cursors.add(cursor)
                except core.RateLimitError:
                    raise
                except (core.FetchError, KeyError, TypeError, ValueError, StopIteration) as exc:
                    boundary_error = exc
                    break
                discovery["in_progress"] = {
                    "series_slug": series, "next_cursor": cursor,
                    "earliest_end_time": earliest.isoformat() if earliest else None,
                }
                if self.clock() - self._last_checkpoint >= self.checkpoint_seconds:
                    self.save()
                    self._last_checkpoint = self.clock()
                if not cursor:
                    break
                params["after_cursor"] = cursor
            if boundary_error is None and earliest is None:
                boundary_error = collector.DiscoveryError(f"Could not establish historical boundary for {series}")
            if boundary_error is not None:
                errors[series] = {"series_slug": series, "type": type(boundary_error).__name__,
                                  "message": str(boundary_error)}
                if isinstance(boundary_error, core.FetchError):
                    errors[series]["http_status"] = boundary_error.status_code
                discovery.pop("in_progress", None)
                self.save()
                print(f"Historical boundary {series} remains unresolved: {describe_exception(boundary_error)}. "
                      "Continuing other series; this boundary will be retried next session.", flush=True)
                continue
            errors.pop(series, None)
            first_ends[series] = earliest.isoformat()
            discovery.pop("in_progress", None)
            print(f"Historical boundary {series}: {first_ends[series]}", flush=True)
            if self.clock() - self._last_checkpoint >= self.checkpoint_seconds:
                self.save()
                self._last_checkpoint = self.clock()
        if any(series not in first_ends for series in collector.TARGET_SERIES):
            # Do not claim a complete historical range from a partial catalog.
            # Fresh dates remain available; missing boundaries retry next run.
            self.save()
            return
        start = min(collector.utc_time(value).date() for value in first_ends.values())
        self.state["history"] = {
            "frozen_at_utc": datetime.now(timezone.utc).isoformat(), "window_start_utc": str(lower),
            "start_date_utc": str(start), "target_end_date_utc": str(upper - timedelta(days=1)),
            "series_first_end": first_ends, "next_date_utc": str(start), "gaps": {}, "finished": False,
        }
        self.save()

    def run(self, *, max_days: int | None = None) -> None:
        self.processed = 0
        while not self._stop_due():
            try:
                self._run(max_days=max_days)
                return
            except core.CollectionPaused:
                self.save()
                if self._stop_due():
                    return
        self.save()

    def _fresh_days(self) -> list[date]:
        first = date.fromisoformat(self.state["rollout_date_utc"]) - timedelta(days=1)
        day = self.today() - timedelta(days=1)
        days = []
        while day >= first:
            stamp = str(day)
            if stamp not in self.attempted and self.state["raw_days"].get(stamp, {}).get("status") != "complete":
                days.append(day)
            day -= timedelta(days=1)
        return days

    def _run(self, *, max_days: int | None = None) -> None:
        while not self._stop_due():
            yesterday = self.today() - timedelta(days=1)
            fresh = self._fresh_days()
            if fresh:
                for day in fresh:
                    self.process_day(day)
                    self.processed += 1
                    if max_days and self.processed >= max_days:
                        return
                    if self._fresh_due():
                        break
                continue
            self.attempted.add(str(yesterday))
            self.recompute_dirty()
            self.discover_history()
            if self._fresh_due():
                continue
            if not self.state.get("history"):
                return
            history = self.state["history"]
            day = date.fromisoformat(history["next_date_utc"])
            end = date.fromisoformat(history["target_end_date_utc"])
            repairs = [date.fromisoformat(stamp) for stamp in self.state.get("repair_queue", [])
                       if stamp not in self.attempted]
            if repairs:
                day = repairs[0]
            elif day > end:
                history["finished"] = True
                # Retry each gap once per session, then end. Permanent gaps
                # remain visible and cannot starve new daily collection.
                gaps = [date.fromisoformat(stamp) for stamp in sorted(history["gaps"])
                        if stamp not in self.attempted]
                if not gaps:
                    self.save()
                    if self._fresh_due():
                        continue
                    return
                day = gaps[0]
            if day < window_start(self.today()):
                history["gaps"][str(day)] = "Unavailable: outside the API's current three-year trade window"
                history["next_date_utc"] = max(history["next_date_utc"], str(day + timedelta(days=1)))
                self.attempted.add(str(day))
                self.state["repair_queue"] = [stamp for stamp in self.state.get("repair_queue", [])
                                              if stamp != str(day)]
                self.save()
                continue
            try:
                self.process_day(day, historical=True)
                self.processed += 1
            except core.CollectionPaused:
                if self.stopping:
                    return
                continue
            if max_days and self.processed >= max_days:
                return
        self.save()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", default=OWNER)
    parser.add_argument("--work-dir", type=Path, default=Path("/kaggle/working/polymarket-pipeline"),
                        help="Working files and retained failure evidence; fresh sessions restore cloud Datasets")
    parser.add_argument("--max-days", type=int, help="Explicit finite acceptance run; production has no day cap")
    parser.add_argument("--migrate-only", action="store_true", help="Upgrade completed cloud snapshots without collecting trades")
    parser.add_argument("--checkpoint-minutes", type=float, default=CHECKPOINT_SECONDS / 60,
                        help="Publish changed recovery work at this interval (default: 30)")
    parser.add_argument("--stop-after-hours", type=float, default=STOP_AFTER_SECONDS / 3600,
                        help="Stop collection and publish progress before the session limit (default: 11.5)")
    parser.add_argument("--session-hours", type=float, default=SESSION_SECONDS / 3600,
                        help="Absolute deadline including setup and publication (default: 12)")
    args = parser.parse_args(argv)
    if not (args.checkpoint_minutes > 0 and 0 < args.stop_after_hours < args.session_hours):
        parser.error("Checkpoint interval must be positive and collection must stop before the session deadline")
    if not Path("/kaggle/working").is_dir():
        parser.error("The producer runs only on Kaggle; local machines run polymarket_kaggle_reports.py")
    started_at = float(os.environ.get("POLYMARKET_RUN_STARTED_MONOTONIC", time.monotonic()))
    client = core.Client(None)
    pipeline = None
    summary = {"schema_version": 1, "run_id": uuid.uuid4().hex,
               "started_at_utc": datetime.now(timezone.utc).isoformat(), "status": "starting",
               "source_commit": os.environ.get("POLYMARKET_SOURCE_COMMIT"),
               "authentication_source": os.environ.get("POLYMARKET_KAGGLE_AUTH_SOURCE"),
               "trigger": os.environ.get("KAGGLE_KERNEL_RUN_TYPE")}
    try:
        pipeline = Pipeline(KaggleStore(args.owner, args.work_dir / "downloads"), args.work_dir,
            client=client, started_at=started_at, checkpoint_seconds=args.checkpoint_minutes * 60,
            stop_after_seconds=args.stop_after_hours * 3600, session_seconds=args.session_hours * 3600)
        summary["resume_history_date"] = (pipeline.state.get("history") or {}).get("next_date_utc")
        summary["status"] = "running"
        pipeline.state["last_run"] = dict(summary)
        pipeline.prepare_cloud_text()
        signal.signal(signal.SIGTERM, lambda *_: setattr(pipeline, "stopping", True))
        try:
            from polymarket_kaggle_migration import migrate_existing
            migrate_existing(pipeline)
            if not args.migrate_only:
                pipeline.run(max_days=args.max_days)
        except (KeyboardInterrupt, core.CollectionPaused):
            pipeline.save()
        summary["status"] = "complete"
        summary["history_next_date"] = (pipeline.state.get("history") or {}).get("next_date_utc")
        summary["ended_at_utc"] = datetime.now(timezone.utc).isoformat()
        pipeline.state["last_run"] = dict(summary)
        pipeline.save()
    except core.RateLimitError as exc:
        summary.update(status="rate_limited", error=describe_exception(exc))
        print(f"Polymarket collection paused after sustained rate limiting: {exc}", flush=True)
        return 0
    except (KaggleError, DiscoveryBlocked) as exc:
        summary.update(status="failed", error=describe_exception(exc))
        print(f"Producer stopped without advancing unconfirmed work: {describe_exception(exc)}", flush=True)
        return 1
    except Exception as exc:
        summary.update(status="failed", error=describe_exception(exc))
        raise
    finally:
        client.close()
        summary.setdefault("ended_at_utc", datetime.now(timezone.utc).isoformat())
        try:
            write_json(args.work_dir / "run-summary.json", summary)
        except OSError as exc:
            print(f"Cannot retain run summary: {type(exc).__name__}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
