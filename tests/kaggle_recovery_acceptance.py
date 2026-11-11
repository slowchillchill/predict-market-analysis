"""Opt-in, PRIVATE Kaggle integration acceptance; never run by unittest discovery.

The bootstrap supplies the checkout, dependencies and existing Kaggle credentials.
Run in two separate Kaggle processes with POLYMARKET_QA_PHASE=1, then =2.
Use POLYMARKET_QA_PHASE=audit to recheck the existing cloud versions read-only.
Only the fixed QA Dataset namespace is writable. Production access is a single
read of the pre-existing 2025-04-02 v1 database during phase 1.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path


WORKING = Path("/kaggle/working")
DAY = date(2025, 4, 2)
TODAY = date(2026, 11, 11)
SERIES = "btc-up-or-down-daily"
QA_PREFIX = "qa-v3-recovery-20261111-"
RAW_SLUG = f"pm-updown-raw-{DAY}"
EXPECTED_END = "2025-04-02T12:00:00+00:00"


class CannotVerify(BaseException):
    """The real sample cannot exercise this acceptance scenario."""


class PowerLoss(BaseException):
    """Bypass ordinary exception handlers after the first durable checkpoint."""


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    if not WORKING.is_dir() or not Path.cwd().resolve().is_relative_to(WORKING):
        raise RuntimeError("This acceptance script requires /kaggle/working as its working directory")
    phase_text = os.environ.get("POLYMARKET_QA_PHASE")
    if phase_text not in {"1", "2", "audit"}:
        raise RuntimeError("Set POLYMARKET_QA_PHASE to 1, 2 or audit")
    phase = phase_text if phase_text == "audit" else int(phase_text)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import polymarket_kaggle_pipeline as producer
    import polymarket_kaggle_store as cloud

    producer.collector.TARGET_SERIES = {SERIES: 1}
    trade_params = producer.core.trade_params

    def small_page_params(condition):
        return {**trade_params(condition), "limit": 10}

    # Store and send the same query in both processes; a real API cursor may
    # depend on query parameters, including its page size.
    producer.core.trade_params = small_page_params
    root = WORKING / "qa-v3-recovery-20261111" / f"phase{phase}"
    # A reused work folder would no longer prove recovery in a clean process.
    if root.exists():
        raise RuntimeError("Use a fresh Kaggle process; the phase working directory already exists")
    root.mkdir(parents=True)
    summary = {"phase": phase, "status": "running", "date_utc": str(DAY),
               "qa_prefix": QA_PREFIX, "production_writes": 0}
    destination = WORKING / ("acceptance_audit.json" if phase == "audit" else f"acceptance_phase{phase}.json")

    class PrivateRunner:
        def __init__(self):
            self.mutations = []
            self.executions = {}
            self.injected = None
            self.confirmed = False

        def __call__(self, command, **kwargs):
            command = list(command)
            mutation = tuple(command[1:3]) in {("datasets", "create"), ("datasets", "version")}
            marker = metadata = None
            if mutation:
                require(phase != "audit", "Read-only audit attempted a Dataset mutation")
                folder = Path(command[command.index("-p") + 1])
                metadata = json.loads((folder / "dataset-metadata.json").read_text())
                marker = json.loads((folder / cloud.PUBLICATION_FILE).read_text())
                expected = f"{cloud.OWNER}/{QA_PREFIX}pm-updown-"
                require(metadata["id"].startswith(expected), "A write escaped the private QA namespace")
                require(marker["dataset"] == metadata["id"], "Publication marker has a different Dataset")
                if self.injected is None:
                    require(metadata["id"].endswith(QA_PREFIX + RAW_SLUG), "The first write must be the raw QA checkpoint")
                    require(command[2] == ("create" if phase == 1 else "version"), "Unexpected initial write operation")
                command = [argument for argument in command if argument != "--public"]
                publication_id = marker["publication_id"]
                # A repeat would stop this acceptance run before a second real
                # upload. Only the server's matching marker can prove success.
                require(not self.executions.get(publication_id), "The same publication attempted a second real upload")
                self.executions[publication_id] = 1
                self.mutations.append({"operation": command[2], "dataset": metadata["id"],
                                       "publication_id": publication_id})
            result = subprocess.run(command, **kwargs)
            output = (result.stdout or "") + (result.stderr or "")
            successful = (result.returncode == 0 and "Dataset creation error:" not in output
                          and "Dataset version creation error:" not in output)
            if mutation and successful and self.injected is None:
                self.injected = marker
                # The upload really succeeded. Hide the successful response to
                # exercise status + pinned publication marker reconciliation.
                return subprocess.CompletedProcess(command, 1, "", "HTTP 499 simulated after successful QA upload")
            return result

    runner = PrivateRunner()

    class PrivateStore(cloud.KaggleStore):
        def ref(self, slug):
            if "/" in slug:
                owner, slug = slug.split("/", 1)
                require(owner == self.owner, "Unexpected Dataset owner")
            if slug.startswith("pm-updown-"):
                slug = QA_PREFIX + slug
            require(slug.startswith(QA_PREFIX + "pm-updown-"), "Dataset reference escaped the QA namespace")
            return f"{self.owner}/{slug}"

        def list_slugs(self, prefix):
            require(prefix.startswith("pm-updown-"), "Unexpected Dataset list prefix")
            return [slug.removeprefix(QA_PREFIX) for slug in super().list_slugs(QA_PREFIX + prefix)]

        def download_file(self, slug, filename, version, destination):
            result = super().download_file(slug, filename, version, destination)
            if filename == cloud.PUBLICATION_FILE and runner.injected is not None:
                if json.loads(result.read_text()) == runner.injected:
                    runner.confirmed = True
            return result

    store = PrivateStore(work_dir=root / "kaggle", runner=runner)
    client = None

    def open_snapshot(path):
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        db.execute("PRAGMA query_only=ON")
        return closing(db)

    def snapshot_shape(db, status):
        require(db.execute("PRAGMA user_version").fetchone()[0] == 3, "Cloud snapshot is not real schema v3")
        require(db.execute("SELECT date_utc,event_start_time,end_time FROM markets").fetchall()
                == [(None, None, EXPECTED_END)], "Unknown start or original 12Z source end changed")
        require(not status.get("structural_errors"), "Snapshot has unresolved structural discovery errors")
        return db.execute("SELECT committed_page,next_cursor,completed_at FROM collection_progress").fetchone()

    def final_outcome(db, status, state_error):
        _, cursor, completed = snapshot_shape(db, status)
        require(completed is not None and cursor is None, "Resumed trade pagination did not finish")
        require(bool(status["coverage"]["is_complete"]), "Finished raw coverage is not complete")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "pending_group_map" in tables:
            require(db.execute("SELECT COUNT(*) FROM pending_group_map").fetchone()[0] == 0,
                    "Finished trade pagination retained a nonempty group map")
        error = status.get("error")
        require(state_error == error, "Monthly progress and raw status disagree about the statistics error")
        if status["status"] == "complete":
            require(error is None, "A complete Dataset still has a statistics error")
            require(tables == {"markets", "collection_progress", "trades"}, "Completed final raw snapshot retained temporary tables")
            return "passed", None
        require(status["status"] == "incomplete" and isinstance(error, str)
                and error.startswith("Statistics: InvalidReconstruction:"),
                "Incomplete final raw is not the identified existing reconstruction gap")
        require(tables == {"markets", "collection_progress", "trades", "pending_group_map"},
                "Statistics rejection unexpectedly finalized or altered the work schema")
        return "passed_with_existing_reconstruction_gap", error

    def replay_reconstruction(db, expected_error):
        import polymarket_updown_price_distribution as distribution

        def rejection(connection):
            try:
                distribution.load_data(connection, DAY, bot_first_seen={})
            except distribution.InvalidReconstruction as exc:
                return str(exc)
            raise AssertionError("The existing reconstruction gap could not be replayed")

        actual = rejection(db)
        require("/ 59 " in actual, "The fixed historical sample no longer rejects the observed group 59")
        require(expected_error == "Statistics: InvalidReconstruction: " + actual,
                "Replayed grouped rows do not reproduce the published statistics error")
        with closing(sqlite3.connect(":memory:")) as legacy:
            db.backup(legacy)
            before = legacy.execute("SELECT * FROM trades ORDER BY market_slug,page_number,row_number").fetchall()
            converted = [row[:4] + ("qa-legacy-" + str(row[4]),) + row[5:] for row in before]
            for (name,) in legacy.execute("SELECT name FROM sqlite_master WHERE type='view' "
                                         "AND name<>'updown_target_series'").fetchall():
                legacy.execute('DROP VIEW "' + name.replace('"', '""') + '"')
            legacy.execute("DROP TABLE trades")
            legacy.execute("CREATE TABLE trades(market_slug TEXT,page_number INTEGER,row_number INTEGER,"
                           "wallet TEXT,transaction_hash TEXT,timestamp INTEGER,side TEXT,outcome TEXT,"
                           "outcome_index INTEGER,token_id TEXT,raw_price TEXT,raw_size TEXT,amount_micro_usdc INTEGER)")
            legacy.executemany("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", converted)
            legacy.commit()
            after = legacy.execute("SELECT * FROM trades ORDER BY market_slug,page_number,row_number").fetchall()
            require([row[:4]+row[5:] for row in before] == [row[:4]+row[5:] for row in after],
                    "The in-memory legacy replay changed non-key trade fields")
            old_error = rejection(legacy)
        require(old_error.replace("qa-legacy-", "") == actual,
                "A one-to-one legacy grouping key did not reproduce the identical reconstruction rejection")
        return {"group_id_rejected": True, "one_to_one_legacy_key_rejected": True,
                "identical_error_after_key_normalization": True, "non_key_trade_fields_unchanged": True,
                "rows_compared": len(before), "error": actual}

    def modern_positive():
        import polymarket_updown_price_distribution as distribution

        folder = root / "modern-v2-positive"
        folder.mkdir()
        store._run("kernels", "output",
                   f"{cloud.OWNER}/polymarket-group-id-resume-phase2-20261110/1",
                   "-p", str(folder), "--file-pattern", "^data[.]sqlite3$", "--quiet")
        path = folder / "data.sqlite3"
        require(path.is_file(), "Pinned modern Notebook v1 did not contain data.sqlite3")
        modern_day = date(2026, 11, 9)
        with open_snapshot(path) as db:
            require(db.execute("PRAGMA user_version").fetchone()[0] == 2, "Pinned modern positive input is not schema v2")
            require(db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 3209,
                    "Pinned modern positive input has a different row count")
            require(db.execute("SELECT COUNT(DISTINCT group_id) FROM trades").fetchone()[0] == 1200,
                    "Pinned modern positive input has a different group count")
            require(db.execute("SELECT slug FROM markets").fetchall() == [("btc-updown-5m-1794186600",)],
                    "Pinned modern positive input has a different market scope")
            catalog = db.execute("SELECT * FROM updown_target_series").fetchall()
            require(len(catalog) == 1 and catalog[0][:2] == ("btc-up-or-down-5m", 1),
                    "Pinned modern positive catalog is not its original single-market scope")
        with closing(producer.core.connect_database(path)) as db:
            # Rebuild derived views while preserving the pinned one-market
            # catalog; collector.connect_database would install 38 series.
            db.executescript((producer.core.ROOT / "sql/updown_daily.sql").read_text())
            require([tuple(row) for row in db.execute("SELECT * FROM updown_target_series")] == catalog,
                    "Modern positive migration changed its original catalog")
            require(db.execute("PRAGMA user_version").fetchone()[0] == 3,
                    "Modern positive working copy did not upgrade to v3")
            prices = distribution.load_data(db, modern_day, bot_first_seen={})
            features = producer.analysis.extract_day_features(path, modern_day)
            bundle = producer.analysis.export_day(path, modern_day, {})
            require(bundle["status"] == "complete" and features["coverage"]["is_complete"]
                    and sum(wallet["trade_count"] for wallet in features["wallets"]) == 3209
                    and prices["validation"]["transaction_groups"] == 1200
                    and bundle["distribution"]["validation"]["source_row_count"] == 3209,
                    "Modern v3 formal export did not preserve the expected rows/groups or completeness")
            producer.trade_groups.finalize_database(db)
            require({row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    == {"markets", "collection_progress", "trades"}, "Modern positive finalization did not retain exactly three tables")
        return {"input_schema_version": 2, "output_schema_version": 3, "rows": 3209,
                "groups": 1200, "original_single_market_catalog_preserved": True,
                "formal_reconstruction_passed": True, "formal_features_and_export_complete": True,
                "final_three_tables": True,
                "polymarket_requests": 0, "publications": 0}

    def audit():
        # Downloads remain inside Kaggle. This branch never creates a Client,
        # Pipeline, request to Polymarket, publication or simulated HTTP error.
        require(store.version(RAW_SLUG) == 3, "Audit requires the existing QA raw v1/v2/v3 sequence")
        month = root / "analysis-evidence"
        month_version = store.restore(f"pm-updown-analysis-{str(TODAY)[:7]}", month)
        phase1 = json.loads((month / "qa_checkpoint.json").read_text())
        progress = json.loads((month / "progress.json").read_text())
        require(phase1["phase"] == 1 and phase1["raw_version"] == 1 and phase1["schema_version"] == 3,
                "Audit has no valid phase-1 cloud evidence")
        versions, markers = {}, []
        for version in (1, 2, 3):
            folder = root / f"raw-v{version}"
            for filename in ("data.sqlite3", "collection_status.json", cloud.PUBLICATION_FILE):
                store.download_file(RAW_SLUG, filename, version, folder / filename)
            status = json.loads((folder / "collection_status.json").read_text())
            marker = json.loads((folder / cloud.PUBLICATION_FILE).read_text())
            require(marker["dataset"] == store.ref(RAW_SLUG), "Pinned version marker identifies another Dataset")
            require(isinstance(marker.get("publication_id"), str) and len(marker["publication_id"]) == 32,
                    "Pinned version lacks a publication UUID")
            markers.append(marker["publication_id"])
            with open_snapshot(folder / "data.sqlite3") as db:
                pages, cursor, completed = snapshot_shape(db, status)
                rows = db.execute("SELECT * FROM trades WHERE page_number<=2 ORDER BY market_slug,page_number,row_number").fetchall()
                mapping = (db.execute("SELECT market_slug,transaction_hash,group_id FROM pending_group_map "
                                      "ORDER BY market_slug,transaction_hash").fetchall()
                           if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pending_group_map'").fetchone()
                           else [])
                count = db.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
                if version < 3:
                    require(status["status"] == "incomplete" and pages == version and cursor and completed is None,
                            "A pinned checkpoint does not match its expected resumable page")
                    require(rows and mapping, "A pinned checkpoint lost its page or persistent group map")
                    require(json.loads(db.execute("SELECT query_params FROM collection_progress").fetchone()[0])["limit"] == 10,
                            "Pinned checkpoint query changed its persisted page size")
                else:
                    reference = progress["raw_days"][str(DAY)]
                    require(reference["version"] == 3 and reference["status"] == status["status"],
                            "Monthly progress does not reference the audited final raw version")
                    outcome, error = final_outcome(db, status, reference.get("error"))
                    replay = replay_reconstruction(db, error) if error else None
            versions[version] = {"rows": rows, "mapping": mapping, "count": count, "pages": pages}
        require(len(set(markers)) == 3, "Different Dataset versions reused the same publication UUID")
        first, second, final = (versions[version] for version in (1, 2, 3))
        require(final["count"] >= second["count"] and final["pages"] >= second["pages"],
                "The final snapshot lost committed checkpoint records or pages")
        require(first["rows"] == [row for row in second["rows"] if row[1] == 1],
                "Checkpoint v2 changed original page-1 rows or groups")
        require(second["rows"] == final["rows"], "Final raw v3 changed the first two committed pages/groups")
        require(set(first["mapping"]) <= set(second["mapping"]), "Checkpoint v2 changed a page-1 group mapping")
        require(phase1["rows"] == first["count"] and phase1["pending_group_count"] == len(first["mapping"])
                and phase1["committed_pages"] == first["pages"], "Phase-1 cloud evidence differs from pinned raw v1")
        require(not runner.mutations, "Read-only audit uploaded data")
        result = {"status": outcome, "raw_version": 3, "raw_status": "incomplete" if error else "complete",
                  "schema_version": 3, "committed_pages": final["pages"], "rows": final["count"],
                  "analysis_evidence_version": month_version,
                  "existing_reconstruction_gap": error, "real_trade_requests": 0, "actual_uploads": 0,
                  "audit_checks": {"phase1_evidence": True, "pinned_versions": [1, 2, 3],
                                   "first_two_pages_preserved": True, "mapping_continuity": True,
                                   "distinct_publication_uuids": True, "no_mutation_or_recollection": True},
                  "historical_499_live_checks_repeated": False}
        result["existing_reconstruction_replay"] = replay
        result["modern_positive"] = modern_positive()
        return result

    try:
        if phase == "audit":
            summary.update(audit())
            return 0
        if phase == 1:
            if store.status(RAW_SLUG) is not None:
                raise CannotVerify("The QA raw Dataset already exists; phase 1 requires an unused namespace")
            baseline = root / "work" / "raw" / str(DAY) / "data.sqlite3"
            source = cloud.KaggleStore(work_dir=root / "production-read")
            source.download_file(RAW_SLUG, "data.sqlite3", 1, baseline)
            with closing(sqlite3.connect(baseline)) as db:
                require(db.execute("PRAGMA user_version").fetchone()[0] == 2, "Production Apr2 v1 is not schema v2")
                require(db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0, "Production Apr2 v1 is not the expected empty baseline")
            summary["source_schema_version"] = 2

        pipeline_holder = []
        original_rows, original_map, original_cursor = [], [], None
        checks = {"unchanged_checkpoint": False, "restored_rows_and_map": False,
                  "original_page_preserved": False, "mapping_continuity": False}

        class QAClient(producer.core.Client):
            def __init__(self):
                super().__init__(None)
                self.trade_requests = 0
                self.http_trade_requests = 0

            def get(self, url, params):
                if url != producer.core.TRADES_URL:
                    return super().get(url, params)
                self.trade_requests += 1
                if phase == 1 and self.trade_requests > 1:
                    raise PowerLoss()
                if phase == 2 and self.trade_requests == 1:
                    require(params.get("cursor") == original_cursor, "The first resumed request did not use the durable cursor")
                    with closing(sqlite3.connect(raw_path)) as db:
                        require(read_page(db) == original_rows, "Restoration changed the original committed page")
                        require(read_map(db) == original_map, "Restoration changed the durable hash/group map")
                    checks["restored_rows_and_map"] = True
                require(params.get("limit") == 10, "The persisted trade query changed its page size")
                self.http_trade_requests += 1
                payload = super().get(url, dict(params))
                if phase == 1:
                    if not payload.get("data") or not payload.get("pagination", {}).get("next_cursor"):
                        raise CannotVerify("The real April2 market has no nonempty resumable first page at limit=10")
                if self.trade_requests == 1:
                    pipeline_holder[0]._last_checkpoint = 0
                return payload

        def read_page(db):
            return db.execute("SELECT * FROM trades WHERE page_number=1 ORDER BY market_slug,row_number").fetchall()

        def read_map(db):
            return db.execute("SELECT market_slug,transaction_hash,group_id FROM pending_group_map "
                              "ORDER BY market_slug,transaction_hash").fetchall()

        client = QAClient()
        pipeline = producer.Pipeline(store, root / "work", today=lambda: TODAY, client=client,
                                     checkpoint_seconds=0)
        pipeline_holder.append(pipeline)
        pipeline.attempted.add(str(TODAY - timedelta(days=1)))
        # Metadata discovery alone must not trigger this page-specific probe.
        pipeline._last_checkpoint = time.monotonic() + 24 * 60 * 60
        raw_path = root / "work" / "raw" / str(DAY) / "data.sqlite3"

        if phase == 2:
            evidence = pipeline.month(str(TODAY)[:7]) / "qa_checkpoint.json"
            require(evidence.is_file(), "Phase 1 did not leave its durable QA evidence")
            phase1 = json.loads(evidence.read_text())
            require(phase1["raw_version"] == 1, "Phase 1 checkpoint is not QA raw v1")
            pipeline._raw_path(DAY, pipeline.state["raw_days"].get(str(DAY)), latest=True)
            require(pipeline._restored_versions.get(str(DAY)) == 1, "Phase 2 must begin from the untouched phase-1 raw v1")
            with closing(sqlite3.connect(raw_path)) as db:
                original_rows, original_map = read_page(db), read_map(db)
                original_cursor = db.execute("SELECT next_cursor FROM collection_progress").fetchone()[0]
                require(original_rows and original_map and original_cursor, "Phase 2 has no recoverable page and map")

        checkpoint = pipeline._checkpoint_active

        def checked_checkpoint():
            active = pipeline._active_day
            if phase == 2 and active is not None:
                db = active["db"]
                require([tuple(row) for row in read_page(db)] == original_rows, "Resume changed the original page/row/group values")
                checks["original_page_preserved"] = True
                completed = db.execute("SELECT completed_at FROM collection_progress").fetchone()[0]
                current = {tuple(row) for row in read_map(db)}
                require((not current) if completed else set(original_map) <= current,
                        "Resume changed a persistent group mapping before completion")
                checks["mapping_continuity"] = True
            changed = checkpoint()
            if changed:
                before = len(runner.mutations)
                require(not checkpoint(), "An unchanged checkpoint was published again")
                require(len(runner.mutations) == before, "An unchanged checkpoint uploaded again")
                checks["unchanged_checkpoint"] = True
                # This probe needs exactly one timed checkpoint per phase.
                # Subsequent pages continue without uploading every small page.
                pipeline.checkpoint_seconds = 24 * 60 * 60
            return changed

        pipeline._checkpoint_active = checked_checkpoint
        try:
            pipeline.process_day(DAY)
        except PowerLoss:
            require(phase == 1, "Unexpected simulated power loss in phase 2")
        else:
            require(phase == 2, "Phase 1 finished without the planned post-checkpoint power loss")

        verification = root / "cloud-verification"
        raw_version = store.restore(RAW_SLUG, verification)
        status = json.loads((verification / "collection_status.json").read_text())
        with closing(sqlite3.connect(verification / "data.sqlite3")) as db:
            schema = db.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            markets = db.execute("SELECT date_utc,event_start_time,end_time FROM markets").fetchall()
            require(schema == 3, "Cloud raw snapshot did not really migrate to schema v3")
            require(markets == [(None, None, EXPECTED_END)], "Unknown start or original 12Z source end changed")
            pages, cursor, completed = db.execute("SELECT committed_page,next_cursor,completed_at FROM collection_progress").fetchone()
            rows = db.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            if phase == 1:
                require(raw_version == 1 and status["status"] == "incomplete", "First cloud checkpoint is not incomplete v1")
                require(pages == 1 and cursor and completed is None, "First cloud checkpoint is not page-1 resumable")
                mapping_rows = db.execute("SELECT COUNT(*) FROM pending_group_map").fetchone()[0]
                require(rows > 0 and mapping_rows > 0, "First cloud checkpoint lost rows or group mappings")
                evidence = {"phase": 1, "raw_version": raw_version, "schema_version": schema,
                            "committed_pages": pages, "rows": rows, "pending_group_count": mapping_rows,
                            "has_cursor": True, "unknown_start_is_null": True, "source_end_time": EXPECTED_END}
            else:
                require(raw_version == 3, "Phase 2 did not publish checkpoint v2 followed by final raw v3")
                outcome, error = final_outcome(db, status, pipeline.state["raw_days"][str(DAY)].get("error"))
                require(read_page(db) == original_rows, "Final cloud snapshot changed the original page/group values")
                require(all(checks.values()), "A required recovery/checkpoint comparison was not exercised")
                summary["recovery_checks"] = checks
                summary.update(status=outcome, raw_status=status["status"], existing_reconstruction_gap=error)
        require(runner.injected is not None and runner.confirmed, "The successful upload's 499 was not reconciled with its pinned marker")
        require(runner.executions[runner.injected["publication_id"]] == 1, "The simulated 499 repeated the actual upload")
        if phase == 1:
            month = pipeline.month(str(TODAY)[:7])
            cloud.write_json(month / "qa_checkpoint.json", evidence)
            pipeline.changed_months.add(str(TODAY)[:7])
            pipeline.save()
        else:
            before = len(runner.mutations)
            pipeline.save()
            pipeline.save()
            require(len(runner.mutations) == before, "An unchanged save created another publication")
            summary["unchanged_save_published"] = False
        summary.update(raw_version=raw_version, schema_version=schema,
                       committed_pages=pages, rows=rows, real_trade_requests=client.http_trade_requests,
                       actual_uploads=len(runner.mutations), injected_499_reconciled=True,
                       injected_publication_uploads=1, private_qa_writes_only=True)
        if phase == 1:
            summary["status"] = "passed"
    except CannotVerify as exc:
        summary.update(status="cannot_verify", reason=str(exc))
    except BaseException as exc:
        summary.update(status="failed", error_type=type(exc).__name__,
                       reason=str(exc) if isinstance(exc, AssertionError) else "Acceptance stopped; inspect the Kaggle run log")
        raise
    finally:
        if client is not None:
            client.close()
        cloud.write_json(destination, summary)
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary["status"] in {"passed", "passed_with_existing_reconstruction_gap"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
