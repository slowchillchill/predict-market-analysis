import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import polymarket_kaggle_pipeline as producer


SERIES = "btc-up-or-down-daily"
TODAY = date(2026, 11, 10)
YESTERDAY = TODAY - timedelta(days=1)


class MemoryStore:
    """Versioned remote files, independent of the producer's scratch directory."""
    def __init__(self):
        self.datasets = {}
        self.published = []
        self.downloads = []
        self.restores = []

    def ref(self, slug):
        return "owner/" + slug

    def version(self, slug):
        return max(self.datasets[slug]) if slug in self.datasets else None

    def list_slugs(self, prefix):
        return sorted(slug for slug in self.datasets if slug.startswith(prefix))

    def restore(self, slug, destination):
        version = self.version(slug)
        self.restores.append((slug, version))
        destination.mkdir(parents=True, exist_ok=True)
        if version:
            for name, data in self.datasets[slug][version].items():
                path = destination / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        return version

    def download_file(self, slug, filename, version, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.datasets[slug][version][filename])
        self.downloads.append((slug, filename, version))
        return destination

    def publish(self, slug, directory, **kwargs):
        version = (self.version(slug) or 0) + 1
        files = {str(path.relative_to(directory)): path.read_bytes()
                 for path in directory.rglob("*") if path.is_file()}
        self.datasets.setdefault(slug, {})[version] = files
        status = json.loads(files["collection_status.json"])["status"] if "collection_status.json" in files else None
        self.published.append((slug, version, status))
        return version


class PublicAPI:
    def __init__(self, clock):
        self.clock = clock
        self.events = {}
        self.trades = []
        self.after_wallet = None
        self.wallet_error = None

    def market(self, day):
        ended = next(producer.collector.expected_slots(SERIES, day))
        slug = producer.collector.market_slug(SERIES, ended)
        event = {"id": slug, "seriesSlug": SERIES, "slug": slug, "markets": [{
            "id": slug, "slug": slug, "conditionId": str(day), "question": "BTC up or down",
            "eventStartTime": (ended - timedelta(days=1)).isoformat(), "endDate": ended.isoformat(),
            "outcomes": ["Up", "Down"]}]}
        self.events[str(day)] = event
        return event

    def get(self, url, params):
        if url == producer.collector.GAMMA_SERIES:
            return [{"slug": SERIES, "id": "1"}]
        if url == producer.collector.GAMMA_EVENTS_KEYSET:
            day = date.fromisoformat(params["end_date_min"][:10])
            return {"events": [self.market(day)], "next_cursor": None}
        if url.startswith(producer.collector.GAMMA_EVENT_SLUG):
            raise AssertionError("No missing metadata expected in this fixture")
        if url == producer.core.TRADES_URL:
            day = params["condition"]
            mode = params["taker_only"]
            if mode != "false":
                raise AssertionError("Production must not request a second trade route")
            self.trades.append((day, mode, params.get("cursor")))
            if self.wallet_error:
                raise self.wallet_error
            if self.after_wallet:
                self.after_wallet(date.fromisoformat(day))
            timestamp = int(datetime.combine(date.fromisoformat(day), datetime.min.time(), timezone.utc).timestamp())
            data = [{"proxy_wallet": wallet, "transaction_hash": "pair", "timestamp": timestamp,
                     "side": side, "outcome": "Up", "outcome_index": 0, "token_id": "up",
                     "price": "0.2", "size": "10"}
                    for wallet, side in (("buyer", "BUY"), ("seller", "SELL"))]
            return {"data": data, "pagination": {"next_cursor": None}}
        raise AssertionError((url, params))

    def close(self):
        pass


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = MemoryStore()
        self.clock = [TODAY]
        self.client = PublicAPI(self.clock)
        self.catalog = patch.dict(producer.collector.TARGET_SERIES, {SERIES: 1}, clear=True)
        self.catalog.start()
        self.addCleanup(self.catalog.stop)
        self.pipeline = producer.Pipeline(self.store, self.root / "work", today=lambda: self.clock[0],
                                          client=self.client)

    def quiet(self):
        return contextlib.redirect_stdout(io.StringIO())

    def seed_history(self, day):
        self.pipeline.state["history"] = {
            "start_date_utc": str(day), "target_end_date_utc": str(day), "next_date_utc": str(day),
            "series_first_end": {SERIES: next(producer.collector.expected_slots(SERIES, day)).isoformat()},
            "gaps": {}, "finished": False,
        }

    def latest_files(self, slug):
        return self.store.datasets[slug][self.store.version(slug)]

    def inspect_raw(self, files):
        path = self.root / "inspected.sqlite3"
        path.write_bytes(files["data.sqlite3"])
        return sqlite3.connect(path)

    def assert_complete_raw_shape(self, files):
        with contextlib.closing(self.inspect_raw(files)) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(tables, {"markets", "collection_progress", "trades"})
            columns = {row[1] for row in db.execute("PRAGMA table_info(trades)")}
            self.assertIn("group_id", columns)
            self.assertNotIn("transaction_hash", columns)
            rows = db.execute("SELECT group_id,typeof(group_id) FROM trades").fetchall()
            self.assertTrue(rows)
            self.assertTrue(all(value > 0 and storage_type == "integer" for value, storage_type in rows))
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], producer.RAW_SCHEMA_VERSION)

    def test_complete_day_publishes_exact_raw_version_then_formal_result(self):
        with self.quiet():
            self.pipeline.process_day(YESTERDAY)
        slug = f"pm-updown-raw-{YESTERDAY}"
        files = self.latest_files(slug)
        metadata = json.loads(files["collection_status.json"])
        self.assertEqual(metadata["status"], "complete")
        self.assertEqual(metadata["schema_version"], producer.RAW_SCHEMA_VERSION)
        self.assertEqual(metadata["group_key"], ["market_slug", "group_id"])
        self.assertEqual(metadata["volume_method"], "wallet_group_first_row_v1")
        self.assertEqual(metadata["coverage_policy"], "official_series_all_conditions_v1")
        self.assertNotIn("volume_sources", metadata)
        self.assertFalse(any(key.startswith("market_volume_") for key in metadata["coverage"]))
        self.assert_complete_raw_shape(files)
        month = self.latest_files("pm-updown-analysis-2026-11")
        bundle = json.loads(month[f"daily_{YESTERDAY}.json"])
        self.assertEqual(bundle["source_versions"][str(YESTERDAY)], {"dataset": "owner/" + slug, "version": 1})
        self.assertEqual(bundle["schema_version"], 1)
        self.assertEqual(bundle["raw_schema_version"], producer.RAW_SCHEMA_VERSION)
        self.assertEqual(bundle["volume_method"], "wallet_group_first_row_v1")
        self.assertEqual(bundle["coverage_policy"], metadata["coverage_policy"])
        self.assertEqual(self.pipeline.state["raw_days"][str(YESTERDAY)]["coverage_policy"],
                         metadata["coverage_policy"])
        self.assertEqual(self.pipeline.state["raw_days"][str(YESTERDAY)]["raw_schema_version"], producer.RAW_SCHEMA_VERSION)
        self.assertEqual(self.pipeline.state["raw_days"][str(YESTERDAY)]["volume_method"],
                         "wallet_group_first_row_v1")
        self.assertEqual(bundle["payload"]["current"]["volume_micro_usdc"], 2000000)
        self.assertFalse((self.root / "work/raw" / str(YESTERDAY)).exists())
        self.assertEqual(self.store.published[0], (slug, 1, "complete"))

    def test_wallet_only_production_keeps_the_same_amount_without_second_api_route(self):
        with self.quiet():
            self.pipeline.process_day(YESTERDAY)
        self.assertEqual(self.client.trades, [(str(YESTERDAY), "false", None)])
        raw = json.loads(self.latest_files(f"pm-updown-raw-{YESTERDAY}")["collection_status.json"])
        self.assertEqual(raw["status"], "complete")
        self.assertEqual(raw["volume_method"], "wallet_group_first_row_v1")
        bundle = json.loads(self.latest_files("pm-updown-analysis-2026-11")[f"daily_{YESTERDAY}.json"])
        self.assertEqual(bundle["payload"]["current"]["volume_micro_usdc"], 2000000)

    def test_wallet_only_resume_rebuilds_summary_without_redownloading_committed_wallets(self):
        self.client.after_wallet = lambda _: setattr(self.pipeline, "stopping", True)
        with self.quiet(), self.assertRaises(producer.core.CollectionPaused):
            self.pipeline.process_day(YESTERDAY)
        self.pipeline.stopping = False
        self.client.after_wallet = None
        with self.quiet():
            self.pipeline.process_day(YESTERDAY)
        self.assertEqual(self.client.trades, [(str(YESTERDAY), "false", None)])
        raw = json.loads(self.latest_files(f"pm-updown-raw-{YESTERDAY}")["collection_status.json"])
        self.assertEqual(raw["status"], "complete")
        self.assertEqual(raw["volume_method"], "wallet_group_first_row_v1")
        self.assert_complete_raw_shape(self.latest_files(f"pm-updown-raw-{YESTERDAY}"))

    def test_partial_page_mapping_survives_publication_and_resumes_in_a_new_session(self):
        original_get = self.client.get

        def paged_get(url, params):
            payload = original_get(url, params)
            if url == producer.core.TRADES_URL:
                if params.get("cursor") is None:
                    payload["data"] = payload["data"][:1]
                    payload["pagination"]["next_cursor"] = "page-2"
                    self.pipeline.stopping = True
                else:
                    payload["data"] = payload["data"][1:]
            return payload

        self.client.get = paged_get
        with self.quiet(), self.assertRaises(producer.core.CollectionPaused):
            self.pipeline.process_day(YESTERDAY)
        slug = f"pm-updown-raw-{YESTERDAY}"
        with contextlib.closing(self.inspect_raw(self.latest_files(slug))) as db:
            self.assertEqual(db.execute("SELECT transaction_hash,group_id FROM pending_group_map").fetchall(),
                             [("pair", 1)])
            self.assertEqual(db.execute("SELECT next_cursor,completed_at FROM collection_progress").fetchone(),
                             ("page-2", None))
            self.assertEqual(db.execute("SELECT group_id FROM trades").fetchall(), [(1,)])
        resumed = producer.Pipeline(self.store, self.root / "resumed", today=lambda: TODAY, client=self.client)
        with self.quiet():
            resumed.process_day(YESTERDAY)
        self.assertEqual(self.client.trades, [(str(YESTERDAY), "false", None),
                                             (str(YESTERDAY), "false", "page-2")])
        self.assertEqual(set(self.store.datasets[slug]), {1, 2})
        self.assert_complete_raw_shape(self.latest_files(slug))
        with contextlib.closing(self.inspect_raw(self.latest_files(slug))) as db:
            self.assertEqual(db.execute("SELECT group_id FROM trades ORDER BY page_number,row_number").fetchall(),
                             [(1,), (1,)])

    def test_sustained_429_still_publishes_recoverable_incomplete_day(self):
        self.client.wallet_error = producer.core.RateLimitError("HTTP 429", status_code=429, retry_after=7)
        with self.quiet(), self.assertRaises(producer.core.RateLimitError):
            self.pipeline.process_day(YESTERDAY)
        slug = f"pm-updown-raw-{YESTERDAY}"
        self.assertEqual(json.loads(self.latest_files(slug)["collection_status.json"])["status"], "incomplete")
        month = self.latest_files("pm-updown-analysis-2026-11")
        self.assertNotIn(f"daily_{YESTERDAY}.json", month)
        progress = json.loads(month["progress.json"])
        self.assertEqual(progress["raw_days"][str(YESTERDAY)]["version"], 1)
        self.assertEqual(progress["raw_days"][str(YESTERDAY)]["status"], "incomplete")
        self.assertEqual(self.client.trades, [(str(YESTERDAY), "false", None)])

    def test_incomplete_day_has_no_formal_statistics(self):
        self.client.wallet_error = producer.core.FetchError("HTTP 400", status_code=400)
        with self.quiet(), contextlib.redirect_stderr(io.StringIO()):
            self.pipeline.process_day(YESTERDAY)
        month = self.latest_files("pm-updown-analysis-2026-11")
        self.assertNotIn("features.sqlite3", month)
        self.assertNotIn("index.json", month)
        self.assertEqual(self.pipeline.state["raw_days"][str(YESTERDAY)]["status"], "incomplete")

    def test_statistics_sql_error_still_archives_finished_raw_for_recovery(self):
        with patch.object(producer.analysis, "extract_day_features",
                          side_effect=sqlite3.OperationalError("Temporary statistics query failure")), self.quiet():
            self.pipeline.process_day(YESTERDAY)
        raw = json.loads(self.latest_files(f"pm-updown-raw-{YESTERDAY}")["collection_status.json"])
        self.assertEqual(raw["status"], "incomplete")
        self.assertEqual(raw["coverage"]["completed_markets"], 1)
        self.assertFalse(any(key.startswith("market_volume_") for key in raw["coverage"]))
        self.assertIn("OperationalError", raw["error"])
        self.assertNotIn("index.json", self.latest_files("pm-updown-analysis-2026-11"))

    def test_new_utc_day_preempts_history_and_preserves_committed_wallet_pages(self):
        historical = YESTERDAY - timedelta(days=1)
        self.seed_history(historical)

        def next_utc_day(day):
            if day == historical:
                self.clock[0] = TODAY + timedelta(days=1)

        self.client.after_wallet = next_utc_day
        with self.quiet():
            self.pipeline.run(max_days=3)
        raw_publications = [(slug.removeprefix("pm-updown-raw-"), status)
                            for slug, _, status in self.store.published if slug.startswith("pm-updown-raw-")]
        self.assertEqual(raw_publications, [(str(YESTERDAY), "complete"), (str(historical), "incomplete"),
                                            (str(TODAY), "complete"), (str(historical), "complete")])
        history_wallet_calls = [call for call in self.client.trades if call[:2] == (str(historical), "false")]
        self.assertEqual(len(history_wallet_calls), 1)
        self.assertTrue(all(call[1] == "false" for call in self.client.trades))
        self.assertIn((f"pm-updown-raw-{historical}", 1), self.store.restores)

    def test_resume_uses_newer_raw_snapshot_when_monthly_progress_is_stale(self):
        slug = f"pm-updown-raw-{YESTERDAY}"
        folder = self.root / "raw-publication"
        path = folder / "data.sqlite3"
        db = producer.collector.connect_database(path)
        producer.collector.discover_series(db, self.client, SERIES, YESTERDAY, TODAY)
        producer.write_json(folder / "collection_status.json", {"status": "incomplete"})
        db.close()
        self.store.publish(slug, folder)
        self.pipeline.state["raw_days"][str(YESTERDAY)] = {
            "slug": slug, "version": 1, "status": "incomplete"}
        self.pipeline.save()

        # The raw version advances, then the producer dies before publishing
        # the corresponding monthly progress snapshot.
        db = producer.collector.connect_database(path)
        market = db.execute("SELECT slug FROM markets").fetchone()[0]
        producer.core.collect_market(db, self.client, market)
        db.close()
        self.store.publish(slug, folder)
        resumed = producer.Pipeline(self.store, self.root / "resumed", today=lambda: TODAY,
                                    client=self.client)
        self.assertEqual(resumed.state["raw_days"][str(YESTERDAY)]["version"], 1)
        self.client.trades.clear()
        with self.quiet():
            resumed.process_day(YESTERDAY)
        self.assertIn((slug, 2), self.store.restores)
        self.assertEqual(self.client.trades, [])
        self.assertEqual(resumed.state["raw_days"][str(YESTERDAY)]["version"], 3)
        self.assertEqual(resumed.state["raw_days"][str(YESTERDAY)]["status"], "complete")

    def test_existing_work_database_keeps_commits_newer_than_published_snapshot(self):
        slug = f"pm-updown-raw-{YESTERDAY}"
        folder = self.root / "raw-publication"
        db = producer.collector.connect_database(folder / "data.sqlite3")
        producer.collector.discover_series(db, self.client, SERIES, YESTERDAY, TODAY)
        db.close()
        producer.write_json(folder / "collection_status.json", {"status": "incomplete"})
        self.store.publish(slug, folder)
        self.pipeline.state["raw_days"][str(YESTERDAY)] = {
            "slug": slug, "version": 1, "status": "incomplete"}
        work = self.root / "work" / "raw" / str(YESTERDAY) / "data.sqlite3"
        db = producer.collector.connect_database(work)
        producer.collector.discover_series(db, self.client, SERIES, YESTERDAY, TODAY)
        producer.core.collect_market(db, self.client, db.execute("SELECT slug FROM markets").fetchone()[0])
        db.close()
        self.client.trades.clear()
        with self.quiet():
            self.pipeline.process_day(YESTERDAY)
        self.assertNotIn((slug, 1), self.store.restores)
        self.assertEqual(self.client.trades, [])

    def test_resume_refuses_to_downgrade_a_recorded_raw_version(self):
        slug = f"pm-updown-raw-{YESTERDAY}"
        folder = self.root / "raw-publication"
        db = producer.collector.connect_database(folder / "data.sqlite3")
        db.close()
        self.store.publish(slug, folder)
        self.pipeline.state["raw_days"][str(YESTERDAY)] = {
            "slug": slug, "version": 2, "status": "incomplete"}
        with self.quiet(), self.assertRaisesRegex(producer.KaggleError, "older or missing"):
            self.pipeline.process_day(YESTERDAY)
        self.assertFalse((self.root / "work/raw" / str(YESTERDAY) / "data.sqlite3").exists())
        with self.quiet(), self.assertRaisesRegex(producer.KaggleError, "older or missing"):
            self.pipeline.process_day(YESTERDAY)

    def test_stop_signal_flag_saves_last_committed_page_and_leaves_history_pointer(self):
        historical = YESTERDAY - timedelta(days=1)
        self.seed_history(historical)
        self.pipeline.attempted.add(str(YESTERDAY))
        self.client.after_wallet = lambda _: setattr(self.pipeline, "stopping", True)
        with self.quiet(), self.assertRaises(producer.core.CollectionPaused):
            self.pipeline.process_day(historical, historical=True)
        self.assertEqual(self.pipeline.state["history"]["next_date_utc"], str(historical))
        raw = json.loads(self.latest_files(f"pm-updown-raw-{historical}")["collection_status.json"])
        self.assertEqual(raw["coverage"]["completed_markets"], 1)
        self.assertFalse(any(key.startswith("market_volume_") for key in raw["coverage"]))
        self.assertEqual(raw["status"], "incomplete")

    def test_history_boundary_scans_later_pages_before_freezing_minimum_market_end(self):
        self.pipeline.attempted.add(str(YESTERDAY))
        earlier = "2026-04-01T17:00:00Z"
        later = "2026-05-01T17:00:00Z"
        def boundary_event(ended):
            return {"seriesSlug": SERIES, "markets": [{"slug": ended, "conditionId": ended,
                    "endDate": ended, "outcomes": ["Up", "Down"]}]}
        responses = [[{"slug": SERIES, "id": "1"}],
                     {"events": [boundary_event(later)], "next_cursor": "next"},
                     {"events": [boundary_event(earlier)], "next_cursor": None}]
        self.client.get = Mock(side_effect=responses)
        with self.quiet():
            self.pipeline.discover_history()
        self.assertEqual(self.pipeline.state["history"]["series_first_end"][SERIES],
                         "2026-04-01T17:00:00+00:00")
        self.assertEqual(self.client.get.call_args_list[-1].args[1]["after_cursor"], "next")

    def test_new_date_during_history_discovery_returns_to_fresh_priority(self):
        self.pipeline.state["raw_days"][str(YESTERDAY)] = {"status": "complete"}

        def new_date_due():
            self.clock[0] = TODAY + timedelta(days=1)
            raise producer.core.CollectionPaused("Fresh UTC day is due")

        with patch.object(self.pipeline, "discover_history", side_effect=new_date_due), self.quiet():
            self.pipeline.run(max_days=1)
        self.assertEqual(self.pipeline.state["raw_days"][str(TODAY)]["status"], "complete")
        self.assertEqual([slug for slug, _, _ in self.store.published if slug.startswith("pm-updown-raw-")],
                         [f"pm-updown-raw-{TODAY}"])

    def test_history_boundary_error_continues_other_series_and_restarts_failed_series_next_session(self):
        other = 'eth-up-or-down-daily'
        self.pipeline.state['raw_days'][str(YESTERDAY)] = {'status': 'complete'}
        broken = [True]
        calls = []

        def event(series, ended, *, malformed=False):
            market = {'slug': f'{series}-{ended}', 'conditionId': f'{series}-{ended}',
                      'endDate': ended, 'outcomes': ['Up', 'Down']}
            if malformed:
                market.pop('conditionId')
            return {'seriesSlug': series, 'markets': [market]}

        def get(url, params):
            if url == producer.collector.GAMMA_SERIES:
                series = params['slug']
                calls.append(('series', series))
                return [{'slug': series, 'id': series}]
            self.assertEqual(url, producer.collector.GAMMA_EVENTS_KEYSET)
            series = params['series_id']
            cursor = params.get('after_cursor')
            calls.append((series, cursor))
            if series == SERIES and cursor is None:
                return {'events': [event(series, '2026-06-01T17:00:00Z')], 'next_cursor': 'second-page'}
            if series == SERIES:
                return {'events': [event(series, '2026-04-01T17:00:00Z', malformed=broken[0])], 'next_cursor': None}
            return {'events': [event(series, '2026-05-01T17:00:00Z')], 'next_cursor': None}

        self.client.get = get
        with patch.dict(producer.collector.TARGET_SERIES, {SERIES: 1, other: 2}, clear=True):
            with self.quiet():
                self.pipeline.run()
            self.assertIsNone(self.pipeline.state['history'])
            self.assertEqual(self.pipeline.processed, 0)
            discovery = self.pipeline.state['history_discovery']
            self.assertNotIn(SERIES, discovery['series_first_end'])
            self.assertEqual(discovery['series_first_end'][other], '2026-05-01T17:00:00+00:00')
            self.assertIn(SERIES, discovery['errors'])
            self.assertNotIn('in_progress', discovery)
            self.assertIn((other, None), calls)
            saved = json.loads(self.latest_files('pm-updown-analysis-2026-11')['progress.json'])
            self.assertEqual(saved['history_discovery'], discovery)
            self.assertIsNone(saved['history'])

            broken[0] = False
            calls.clear()
            resumed = producer.Pipeline(self.store, self.root / 'boundary-fixed',
                                        today=lambda: TODAY, client=self.client)
            resumed.attempted.add(str(YESTERDAY))
            with self.quiet():
                resumed.discover_history()
            self.assertEqual(calls, [('series', SERIES), (SERIES, None), (SERIES, 'second-page')])
            self.assertEqual(resumed.state['history']['start_date_utc'], '2026-04-01')
            self.assertEqual(resumed.state['history']['series_first_end'], {
                SERIES: '2026-04-01T17:00:00+00:00', other: '2026-05-01T17:00:00+00:00'})
            self.assertEqual(resumed.state['history_discovery']['errors'], {})

    def test_history_boundary_fetch_error_is_local_but_rate_limit_stops(self):
        other = 'eth-up-or-down-daily'
        self.pipeline.attempted.add(str(YESTERDAY))
        calls = []

        def get(url, params):
            if url == producer.collector.GAMMA_SERIES:
                calls.append(params['slug'])
                if params['slug'] == SERIES:
                    raise producer.core.FetchError('HTTP 404', status_code=404)
                return [{'slug': other, 'id': other}]
            return {'events': [{'seriesSlug': other, 'markets': [{
                'slug': 'healthy-boundary', 'conditionId': 'healthy-boundary',
                'endDate': '2026-04-01T17:00:00Z', 'outcomes': ['Up', 'Down']}]}], 'next_cursor': None}

        self.client.get = get
        with patch.dict(producer.collector.TARGET_SERIES, {SERIES: 1, other: 2}, clear=True):
            with self.quiet():
                self.pipeline.discover_history()
            self.assertEqual(calls, [SERIES, other])
            self.assertEqual(self.pipeline.state['history_discovery']['errors'][SERIES]['http_status'], 404)
            self.assertIsNone(self.pipeline.state['history'])
            self.client.get = Mock(side_effect=producer.core.RateLimitError('HTTP 429'))
            with self.quiet(), self.assertRaises(producer.core.RateLimitError):
                self.pipeline.discover_history()
            self.assertEqual(self.client.get.call_count, 1)

    def test_history_boundary_error_save_failure_does_not_continue_other_series(self):
        other = 'eth-up-or-down-daily'
        self.pipeline.attempted.add(str(YESTERDAY))
        self.client.get = Mock(side_effect=producer.core.FetchError('HTTP 404', status_code=404))
        self.store.publish = Mock(side_effect=producer.KaggleError('Unconfirmed boundary error snapshot'))
        with patch.dict(producer.collector.TARGET_SERIES, {SERIES: 1, other: 2}, clear=True), \
                self.quiet(), self.assertRaisesRegex(producer.KaggleError, 'Unconfirmed'):
            self.pipeline.discover_history()
        self.assertEqual(self.client.get.call_count, 1)
        self.assertIsNone(self.pipeline.state['history'])

    def test_history_boundary_repeated_resume_cursor_is_recorded_and_other_series_continues(self):
        other = 'eth-up-or-down-daily'
        self.pipeline.attempted.add(str(YESTERDAY))
        self.pipeline.state['history_discovery'] = {
            'window_start_utc': '2023-11-10', 'end_date_utc_exclusive': str(TODAY),
            'series_first_end': {}, 'in_progress': {
                'series_slug': SERIES, 'next_cursor': 'saved-page',
                'earliest_end_time': '2026-05-01T17:00:00+00:00'}}
        calls = []

        def get(url, params):
            if url == producer.collector.GAMMA_SERIES:
                return [{'slug': params['slug'], 'id': params['slug']}]
            series = params['series_id']
            calls.append((series, params.get('after_cursor')))
            return {'events': [{'seriesSlug': series, 'markets': [{
                'slug': series, 'conditionId': series, 'endDate': '2026-04-01T17:00:00Z',
                'outcomes': ['Up', 'Down']}]}],
                'next_cursor': 'saved-page' if series == SERIES else None}

        self.client.get = get
        with patch.dict(producer.collector.TARGET_SERIES, {SERIES: 1, other: 2}, clear=True), self.quiet():
            self.pipeline.discover_history()
        self.assertEqual(calls, [(SERIES, 'saved-page'), (other, None)])
        discovery = self.pipeline.state['history_discovery']
        self.assertIn('repeated a cursor', discovery['errors'][SERIES]['message'])
        self.assertNotIn(SERIES, discovery['series_first_end'])
        self.assertIn(other, discovery['series_first_end'])
        self.assertNotIn('in_progress', discovery)
        self.assertIsNone(self.pipeline.state['history'])

    def test_gap_retry_does_not_rewind_completed_history_range(self):
        gap = YESTERDAY - timedelta(days=3)
        self.seed_history(gap)
        self.pipeline.state["history"].update(next_date_utc=str(YESTERDAY), target_end_date_utc=str(YESTERDAY - timedelta(days=1)),
                                               finished=True, gaps={str(gap): "Retry this market"})
        self.pipeline.state["raw_days"][str(YESTERDAY)] = {"status": "complete"}
        with self.quiet():
            self.pipeline.run(max_days=1)
        self.assertEqual(self.pipeline.state["history"]["next_date_utc"], str(YESTERDAY))
        self.assertEqual(self.pipeline.state["history"]["gaps"], {})


class ProducerMainTests(unittest.TestCase):
    def invoke(self, pipeline, *, initialization_error=None):
        client = Mock()
        stream = io.StringIO()
        with patch.object(producer.Path, 'is_dir', return_value=True), \
                patch.object(producer.core, 'Client', return_value=client), \
                patch.object(producer, 'KaggleStore') as store, \
                patch.object(producer, 'Pipeline', return_value=pipeline,
                             side_effect=initialization_error) as constructor, \
                patch.object(producer.signal, 'signal'), \
                patch('polymarket_kaggle_migration.migrate_existing'), \
                contextlib.redirect_stdout(stream):
            result = producer.main([])
        return result, stream.getvalue(), client, constructor, store

    def test_default_work_directory_retains_failure_evidence_in_notebook_output(self):
        pipeline = Mock()
        result, _, client, constructor, store = self.invoke(pipeline)
        self.assertEqual(result, 0)
        self.assertEqual(constructor.call_args.args[1], Path('/kaggle/working/polymarket-pipeline'))
        self.assertEqual(store.call_args.args[1], Path('/kaggle/working/polymarket-pipeline/downloads'))
        self.assertIs(constructor.call_args.kwargs['client'], client)
        client.close.assert_called_once_with()

    def test_publication_failure_logs_redacted_underlying_cause(self):
        cause = producer.KaggleError('HTTP 403 Forbidden Authorization: Bearer secret-test-token')
        failure = producer.KaggleError('Cannot determine Dataset publication result; keep working files')
        failure.__cause__ = cause
        pipeline = Mock()
        pipeline.run.side_effect = failure
        with patch.dict(os.environ, {'KAGGLE_API_TOKEN': 'secret-test-token'}):
            result, output, client, _, _ = self.invoke(pipeline)
        self.assertEqual(result, 1)
        self.assertIn('Producer stopped without advancing unconfirmed work:', output)
        self.assertIn('Cannot determine Dataset publication result', output)
        self.assertIn('403', output)
        self.assertNotIn('secret-test-token', output)
        pipeline.save.assert_not_called()
        client.close.assert_called_once_with()

    def test_restore_failure_reports_cause_and_closes_client_before_pipeline_exists(self):
        result, output, client, _, _ = self.invoke(Mock(),
            initialization_error=producer.KaggleError('HTTP 503 during restore'))
        self.assertEqual(result, 1)
        self.assertIn('503 during restore', output)
        client.close.assert_called_once_with()

    def test_paused_collection_save_failure_uses_publication_error_handler(self):
        pipeline = Mock()
        pipeline.run.side_effect = producer.core.CollectionPaused('Stopping')
        pipeline.save.side_effect = producer.KaggleError('Pending publication failed')
        result, output, client, _, _ = self.invoke(pipeline)
        self.assertEqual(result, 1)
        self.assertIn('Pending publication failed', output)
        pipeline.save.assert_called_once_with()
        client.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
