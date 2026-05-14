# Active Wallet Statistics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing BTC/ETH Up/Down volume reports with active wallet statistics and Top 10 wallet concentration, while keeping volume and wallet denominators auditable.

**Architecture:** Keep `scripts/btc_updown_volume_report.py` as the single report generator. Add a Polymarket trade-history layer fed by `https://data-api.polymarket.com/trades`, aggregate wallet metrics from those trade rows, and render the results into the same Markdown report as the existing volume tables. Kalshi wallet metrics must be explicit `not_available` rows because the public Kalshi trade endpoints expose trade quantity and price, but not wallet or user identifiers.

**Tech Stack:** Python stdlib (`urllib`, `csv`, `gzip`, `json`, `decimal`, `unittest`), existing Polymarket Gamma/Data APIs, existing Kalshi public API calls.

---

## Review Gate

This is a plan-only change for Issue #9. Do not implement code until this plan is reviewed and approved.

## Scope

- Assets: both existing `--asset btc` and `--asset eth` runs.
- Market families: the existing Up/Down market types: `5min`, `15min`, `hourly`, `4hour`, `daily`.
- Date range: unchanged, `2026-02-01T00:00:00Z <= time < 2026-05-01T00:00:00Z`.
- Report placement: wallet summaries go into the existing asset Markdown report:
  - `reports/btc_updown_cross_platform_volume_summary_2026-02_2026-04.md`
  - `reports/eth_updown_cross_platform_volume_summary_2026-02_2026-04.md`
- Generated wallet CSV and raw trade snapshots remain local artifacts and must not be committed.

## Definitions

- `active_wallet_count`: number of distinct Polymarket `proxyWallet` values with at least one valid trade row in the same aggregation bucket.
- Daily bucket: `(platform, date_utc, month_utc, market_type)`, where `date_utc` is the existing market close/end date used by the volume report, not the individual trade timestamp date.
- Monthly bucket: `(platform, month_utc, market_type)`.
- Market bucket: a single `platform_market_id`.
- De-duplication rule: within a daily or monthly bucket, the same wallet is counted once even if it traded multiple markets in the same market type.
- `active_wallet_count` source: Polymarket `/trades` with `takerOnly=false`, used only for wallet identity coverage and de-duplication. Do not sum these participant rows into market成交额 because maker/taker rows can duplicate one trade.
- `taker_wallet_trade_volume`: sum of Polymarket `/trades` `size` values from `takerOnly=true` rows for one taker wallet. This is contract/share volume, not USD notional.
- `top10_wallet_volume`: sum of `taker_wallet_trade_volume` for the ten largest taker wallets in the same bucket.
- `top10_wallet_volume_share`: `top10_wallet_volume / trade_history_volume`, rounded for display.
- `trade_history_volume`: sum of valid Polymarket `takerOnly=true` trade `size` values in the same bucket. This keeps the denominator one row per market trade and comparable to market成交额.
- `participant_trade_count`: count of participant rows returned by `takerOnly=false`, for diagnostics only.
- `taker_trade_count`: count of market-volume-aligned rows returned by `takerOnly=true`.
- `platform_reported_volume`: existing platform cumulative volume from `DETAIL_FIELDS["volume"]`.
- `volume_gap`: `platform_reported_volume - trade_history_volume`.
- `data_status`:
  - `complete`: all trade pages fetched and no pagination guard tripped.
  - `truncated`: the Data API returned a full page at the maximum supported offset, so more rows may exist.
  - `not_available`: the platform does not expose wallet-level public trade data.

Do not calculate Top 10 wallet share against `platform_reported_volume`. The numerator comes from trade history, so the denominator must also come from trade history. Keep `platform_reported_volume` and `volume_gap` beside the wallet metric for audit.

## Source Findings

- Polymarket docs state the Gamma and Data APIs are public. The Data API includes `GET /trades` under profile/data endpoints and returns fields including `proxyWallet`, `conditionId`, `size`, `price`, `timestamp`, `side`, `outcome`, and `transactionHash`.
- Polymarket `/trades` accepts a `market` condition ID filter, `limit`, `offset`, and `takerOnly`.
- Read-only reviewer probe found that `takerOnly=false` can return both maker and taker participant rows for the same `transactionHash` and `size`; therefore `takerOnly=false` rows cannot be summed as market成交额.
- Kalshi public `GET /markets/trades` and `GET /historical/trades` return trade fields such as `trade_id`, `ticker`, `count_fp`, price fields, taker side fields, and `created_time`, but not a wallet, account, or user identifier.
- Therefore Polymarket wallet concentration can be computed from public data, while Kalshi wallet concentration cannot be computed from the current public endpoints without inventing an identity field.

## Polymarket Trade Row Decision

Use two Polymarket trade queries per market:

- `takerOnly=false` for `active_wallet_count`.
  - Include maker and taker participant wallets in the active wallet set.
  - Count each wallet once per daily or monthly bucket, even across multiple markets in the same market type.
  - Do not use participant row `size` sums as `trade_history_volume`.
- `takerOnly=true` for `top10_wallet_volume`, `top10_wallet_volume_share`, `trade_history_volume`, and `volume_gap`.
  - This gives one market-volume-aligned row per trade.
  - Top 10 concentration is therefore a Top 10 taker-wallet share of market成交额.
  - The report must state this limitation in the wallet section.

## Files

- Modify: `scripts/btc_updown_volume_report.py`
  - Add Polymarket Data API trade fetch and pagination helpers.
  - Add wallet CSV paths and schemas.
  - Add wallet aggregators.
  - Add wallet sections to `build_markdown_report`.
  - Keep the existing volume output behavior backward compatible.
- Modify: `tests/test_btc_updown_volume_report.py`
  - Add unit tests for trade normalization, pagination guards, aggregation, Kalshi unavailable rows, and Markdown rendering.
- Modify: `.gitignore`
  - Ignore generated wallet raw snapshots and wallet CSV outputs.
- Generated local artifacts after implementation:
  - `data/raw/btc_updown_polymarket_trades_2026-02_2026-04.jsonl.gz`
  - `data/raw/eth_updown_polymarket_trades_2026-02_2026-04.jsonl.gz`
  - `outputs/btc_updown_wallet_activity_by_market_2026-02_2026-04.csv`
  - `outputs/eth_updown_wallet_activity_by_market_2026-02_2026-04.csv`
  - `outputs/btc_updown_wallet_activity_daily_by_platform_2026-02_2026-04.csv`
  - `outputs/eth_updown_wallet_activity_daily_by_platform_2026-02_2026-04.csv`
  - `outputs/btc_updown_wallet_activity_monthly_by_platform_2026-02_2026-04.csv`
  - `outputs/eth_updown_wallet_activity_monthly_by_platform_2026-02_2026-04.csv`

## Output Schemas

`*_wallet_activity_by_market_2026-02_2026-04.csv`:

```text
asset,platform,month_utc,date_utc,market_type,platform_series,platform_market_id,title,active_wallet_count,participant_trade_count,taker_trade_count,trade_history_volume,top10_wallet_volume,top10_wallet_volume_share,platform_reported_volume,volume_gap,volume_gap_pct,data_status
```

`*_wallet_activity_monthly_by_platform_2026-02_2026-04.csv`:

```text
asset,platform,month_utc,market_type,active_wallet_count,participant_trade_count,taker_trade_count,trade_history_volume,top10_wallet_volume,top10_wallet_volume_share,platform_reported_volume,volume_gap,volume_gap_pct,data_status,unavailable_reason
```

`*_wallet_activity_daily_by_platform_2026-02_2026-04.csv`:

```text
asset,platform,date_utc,month_utc,market_type,active_wallet_count,participant_trade_count,taker_trade_count,trade_history_volume,top10_wallet_volume,top10_wallet_volume_share,platform_reported_volume,volume_gap,volume_gap_pct,data_status,unavailable_reason
```

Kalshi rows use:

```text
data_status=not_available
unavailable_reason=kalshi_public_trades_have_no_wallet_identifier
active_wallet_count=
top10_wallet_volume=
top10_wallet_volume_share=
```

## Task 1: Add Wallet Output Paths and Schemas

**Files:**
- Modify: `scripts/btc_updown_volume_report.py`
- Test: `tests/test_btc_updown_volume_report.py`

- [ ] **Step 1: Write failing tests for asset-specific wallet paths**

Add this test:

```python
def test_configure_asset_sets_wallet_output_paths(self):
    report.configure_asset("btc")
    self.assertEqual(
        report.POLYMARKET_TRADES_RAW_PATH.name,
        "btc_updown_polymarket_trades_2026-02_2026-04.jsonl.gz",
    )
    self.assertEqual(
        report.WALLET_MARKET_PATH.name,
        "btc_updown_wallet_activity_by_market_2026-02_2026-04.csv",
    )
    self.assertEqual(
        report.WALLET_DAILY_PATH.name,
        "btc_updown_wallet_activity_daily_by_platform_2026-02_2026-04.csv",
    )
    self.assertEqual(
        report.WALLET_MONTHLY_PATH.name,
        "btc_updown_wallet_activity_monthly_by_platform_2026-02_2026-04.csv",
    )

    report.configure_asset("eth")
    self.assertEqual(
        report.POLYMARKET_TRADES_RAW_PATH.name,
        "eth_updown_polymarket_trades_2026-02_2026-04.jsonl.gz",
    )
    self.assertEqual(
        report.WALLET_MARKET_PATH.name,
        "eth_updown_wallet_activity_by_market_2026-02_2026-04.csv",
    )
    self.assertEqual(
        report.WALLET_DAILY_PATH.name,
        "eth_updown_wallet_activity_daily_by_platform_2026-02_2026-04.csv",
    )
```

- [ ] **Step 2: Run the focused failing test**

Run:

```bash
python3 -m unittest tests.test_btc_updown_volume_report.BtcUpdownVolumeReportTests.test_configure_asset_sets_wallet_output_paths
```

Expected: fail with `AttributeError` for the new wallet path globals.

- [ ] **Step 3: Add path globals and field lists**

Add globals near the existing output path globals:

```python
POLYMARKET_TRADES_RAW_PATH: Path
WALLET_MARKET_PATH: Path
WALLET_DAILY_PATH: Path
WALLET_MONTHLY_PATH: Path

WALLET_MARKET_FIELDS = [
    "asset",
    "platform",
    "month_utc",
    "date_utc",
    "market_type",
    "platform_series",
    "platform_market_id",
    "title",
    "active_wallet_count",
    "participant_trade_count",
    "taker_trade_count",
    "trade_history_volume",
    "top10_wallet_volume",
    "top10_wallet_volume_share",
    "platform_reported_volume",
    "volume_gap",
    "volume_gap_pct",
    "data_status",
]

WALLET_MONTHLY_FIELDS = [
    "asset",
    "platform",
    "month_utc",
    "market_type",
    "active_wallet_count",
    "participant_trade_count",
    "taker_trade_count",
    "trade_history_volume",
    "top10_wallet_volume",
    "top10_wallet_volume_share",
    "platform_reported_volume",
    "volume_gap",
    "volume_gap_pct",
    "data_status",
    "unavailable_reason",
]

WALLET_DAILY_FIELDS = [
    "asset",
    "platform",
    "date_utc",
    "month_utc",
    "market_type",
    "active_wallet_count",
    "participant_trade_count",
    "taker_trade_count",
    "trade_history_volume",
    "top10_wallet_volume",
    "top10_wallet_volume_share",
    "platform_reported_volume",
    "volume_gap",
    "volume_gap_pct",
    "data_status",
    "unavailable_reason",
]
```

Update `configure_asset` after the existing report paths:

```python
POLYMARKET_TRADES_RAW_PATH = ROOT / "data" / "raw" / f"{output_prefix}_polymarket_trades_{DATA_RANGE_LABEL}.jsonl.gz"
WALLET_MARKET_PATH = ROOT / "outputs" / f"{output_prefix}_wallet_activity_by_market_{DATA_RANGE_LABEL}.csv"
WALLET_DAILY_PATH = ROOT / "outputs" / f"{output_prefix}_wallet_activity_daily_by_platform_{DATA_RANGE_LABEL}.csv"
WALLET_MONTHLY_PATH = ROOT / "outputs" / f"{output_prefix}_wallet_activity_monthly_by_platform_{DATA_RANGE_LABEL}.csv"
```

Include the new globals in the `global` statement.

- [ ] **Step 4: Re-run the focused test**

Run:

```bash
python3 -m unittest tests.test_btc_updown_volume_report.BtcUpdownVolumeReportTests.test_configure_asset_sets_wallet_output_paths
```

Expected: pass.

## Task 2: Add Polymarket Trade Fetching and Normalization

**Files:**
- Modify: `scripts/btc_updown_volume_report.py`
- Test: `tests/test_btc_updown_volume_report.py`

- [ ] **Step 1: Write failing tests for trade normalization and dedupe key**

Add:

```python
def test_normalize_polymarket_trade_requires_wallet_condition_and_size(self):
    trade = {
        "proxyWallet": "0x0000000000000000000000000000000000000001",
        "conditionId": "0x" + "a" * 64,
        "size": "12.5",
        "price": "0.61",
        "timestamp": 1770000000,
        "side": "BUY",
        "outcome": "Up",
        "transactionHash": "0xhash",
    }
    normalized = report.normalize_polymarket_trade(trade, "0x" + "a" * 64)
    self.assertEqual(normalized["wallet"], "0x0000000000000000000000000000000000000001")
    self.assertEqual(normalized["condition_id"], "0x" + "a" * 64)
    self.assertEqual(normalized["size"], Decimal("12.5"))
    self.assertEqual(
        report.polymarket_trade_key(normalized),
        ("0xhash", "0x0000000000000000000000000000000000000001", "0x" + "a" * 64, "Up", 1770000000, "BUY", "12.5", "0.61"),
    )

def test_normalize_polymarket_trade_rejects_wrong_condition(self):
    trade = {
        "proxyWallet": "0x0000000000000000000000000000000000000001",
        "conditionId": "0x" + "b" * 64,
        "size": "1",
        "timestamp": 1770000000,
    }
    with self.assertRaises(ValueError):
        report.normalize_polymarket_trade(trade, "0x" + "a" * 64)
```

- [ ] **Step 2: Run the focused failing tests**

Run:

```bash
python3 -m unittest \
  tests.test_btc_updown_volume_report.BtcUpdownVolumeReportTests.test_normalize_polymarket_trade_requires_wallet_condition_and_size \
  tests.test_btc_updown_volume_report.BtcUpdownVolumeReportTests.test_normalize_polymarket_trade_rejects_wrong_condition
```

Expected: fail because the functions do not exist.

- [ ] **Step 3: Add Data API constants and normalization helpers**

Add near API constants:

```python
POLYMARKET_DATA_API_BASE = "https://data-api.polymarket.com"
POLYMARKET_TRADE_LIMIT = 10000
POLYMARKET_MAX_TRADE_OFFSET = 10000
```

Add helpers after `choose_polymarket_volume`:

```python
def normalize_polymarket_trade(trade: dict, expected_condition_id: str) -> dict:
    wallet = str(trade.get("proxyWallet") or "").lower()
    condition_id = str(trade.get("conditionId") or "").lower()
    expected = expected_condition_id.lower()
    if not wallet:
        raise ValueError("Polymarket trade missing proxyWallet")
    if condition_id != expected:
        raise ValueError(f"Polymarket trade conditionId mismatch: {condition_id} != {expected}")
    size = decimal_from_value(trade.get("size"), "trade.size")
    timestamp = int(trade.get("timestamp"))
    return {
        "wallet": wallet,
        "condition_id": condition_id,
        "size": size,
        "price": str(trade.get("price") or ""),
        "timestamp": timestamp,
        "side": str(trade.get("side") or ""),
        "outcome": str(trade.get("outcome") or ""),
        "transaction_hash": str(trade.get("transactionHash") or ""),
        "raw": trade,
    }


def polymarket_trade_key(trade: dict) -> tuple[str, str, str, str, int, str, str, str]:
    return (
        trade["transaction_hash"],
        trade["wallet"],
        trade["condition_id"],
        trade["outcome"],
        trade["timestamp"],
        trade["side"],
        format_decimal(trade["size"]),
        trade["price"],
    )
```

- [ ] **Step 4: Re-run the focused tests**

Run:

```bash
python3 -m unittest \
  tests.test_btc_updown_volume_report.BtcUpdownVolumeReportTests.test_normalize_polymarket_trade_requires_wallet_condition_and_size \
  tests.test_btc_updown_volume_report.BtcUpdownVolumeReportTests.test_normalize_polymarket_trade_rejects_wrong_condition
```

Expected: pass.

## Task 3: Add Polymarket Trade Pagination Guardrails

**Files:**
- Modify: `scripts/btc_updown_volume_report.py`
- Test: `tests/test_btc_updown_volume_report.py`

- [ ] **Step 1: Write failing pagination tests**

Add:

```python
def test_fetch_polymarket_trades_for_market_dedupes_and_detects_truncation(self):
    condition_id = "0x" + "a" * 64
    calls = []

    def fake_get(base_url, path, params):
        calls.append(params.copy())
        if params["offset"] == 0:
            return [
                {
                    "proxyWallet": "0x0000000000000000000000000000000000000001",
                    "conditionId": condition_id,
                    "size": "1",
                    "timestamp": 1770000000,
                    "transactionHash": "0x1",
                }
            ]
        return []

    original = report.get_json_list
    report.get_json_list = fake_get
    try:
        trades, status = report.fetch_polymarket_trades_for_market(condition_id, taker_only=False)
    finally:
        report.get_json_list = original

    self.assertEqual(status, "complete")
    self.assertEqual(len(trades), 1)
    self.assertEqual(calls[0]["market"], condition_id)
    self.assertEqual(calls[0]["takerOnly"], "false")

def test_fetch_polymarket_trades_for_market_marks_truncated_at_max_offset(self):
    condition_id = "0x" + "a" * 64

    def fake_get(base_url, path, params):
        return [
            {
                "proxyWallet": f"0x{i:040x}",
                "conditionId": condition_id,
                "size": "1",
                "timestamp": 1770000000 + i,
                "transactionHash": f"0x{i}",
            }
            for i in range(report.POLYMARKET_TRADE_LIMIT)
        ]

    original = report.get_json_list
    report.get_json_list = fake_get
    try:
        trades, status = report.fetch_polymarket_trades_for_market(condition_id, taker_only=False)
    finally:
        report.get_json_list = original

    self.assertEqual(status, "truncated")
    self.assertEqual(len(trades), report.POLYMARKET_TRADE_LIMIT * 2)
```

- [ ] **Step 2: Run the failing pagination tests**

Run:

```bash
python3 -m unittest \
  tests.test_btc_updown_volume_report.BtcUpdownVolumeReportTests.test_fetch_polymarket_trades_for_market_dedupes_and_detects_truncation \
  tests.test_btc_updown_volume_report.BtcUpdownVolumeReportTests.test_fetch_polymarket_trades_for_market_marks_truncated_at_max_offset
```

Expected: fail because `fetch_polymarket_trades_for_market` does not exist.

- [ ] **Step 3: Implement the fetcher**

Add:

```python
def fetch_polymarket_trades_for_market(condition_id: str, taker_only: bool) -> tuple[list[dict], str]:
    offset = 0
    trades = []
    seen_keys = set()
    status = "complete"
    while True:
        params = {
            "market": condition_id,
            "limit": POLYMARKET_TRADE_LIMIT,
            "offset": offset,
            "takerOnly": "true" if taker_only else "false",
        }
        page = get_json_list(POLYMARKET_DATA_API_BASE, "/trades", params)
        for raw_trade in page:
            normalized = normalize_polymarket_trade(raw_trade, condition_id)
            key = polymarket_trade_key(normalized)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            trades.append(normalized)

        if len(page) < POLYMARKET_TRADE_LIMIT:
            break
        if offset >= POLYMARKET_MAX_TRADE_OFFSET:
            status = "truncated"
            break
        offset += POLYMARKET_TRADE_LIMIT
        time.sleep(REQUEST_SLEEP_SECONDS)
    return trades, status
```

- [ ] **Step 4: Re-run pagination tests**

Run the same command from Step 2.

Expected: pass.

## Task 4: Aggregate Wallet Metrics

**Files:**
- Modify: `scripts/btc_updown_volume_report.py`
- Test: `tests/test_btc_updown_volume_report.py`

- [ ] **Step 1: Write failing aggregation tests**

Add:

```python
def test_wallet_market_daily_and_monthly_aggregation_dedupes_wallets_within_bucket(self):
    detail_rows = [
        {
            "platform": "polymarket",
            "month_utc": "2026-02",
            "date_utc": "2026-02-01",
            "market_type": "hourly",
            "platform_series": "btc-up-or-down-hourly",
            "platform_market_id": "0x" + "a" * 64,
            "title": "Bitcoin Up or Down - February 1",
            "volume": "100",
        },
        {
            "platform": "polymarket",
            "month_utc": "2026-02",
            "date_utc": "2026-02-01",
            "market_type": "hourly",
            "platform_series": "btc-up-or-down-hourly",
            "platform_market_id": "0x" + "b" * 64,
            "title": "Bitcoin Up or Down - February 1 1PM",
            "volume": "50",
        }
    ]
    trades_by_market = {
        "0x" + "a" * 64: {
            "participant_trades": [
                {"wallet": "0x1", "size": Decimal("5"), "transaction_hash": "0xpaired", "raw": {}},
                {"wallet": "0x2", "size": Decimal("5"), "transaction_hash": "0xpaired", "raw": {}},
                {"wallet": "0x3", "size": Decimal("10"), "transaction_hash": "0xsolo", "raw": {}},
            ],
            "participant_status": "complete",
            "taker_trades": [
                {"wallet": "0x2", "size": Decimal("5"), "transaction_hash": "0xpaired", "raw": {}},
                {"wallet": "0x3", "size": Decimal("10"), "transaction_hash": "0xsolo", "raw": {}},
            ],
            "taker_status": "truncated",
        },
        "0x" + "b" * 64: {
            "participant_trades": [
                {"wallet": "0x1", "size": Decimal("5"), "transaction_hash": "0xother", "raw": {}},
                {"wallet": "0x4", "size": Decimal("15"), "transaction_hash": "0xother", "raw": {}},
            ],
            "participant_status": "complete",
            "taker_trades": [
                {"wallet": "0x4", "size": Decimal("15"), "transaction_hash": "0xother", "raw": {}},
            ],
            "taker_status": "complete",
        },
    }
    market_rows, daily_rows, monthly_rows = report.aggregate_wallet_activity(detail_rows, trades_by_market)
    self.assertEqual(market_rows[0]["active_wallet_count"], "3")
    self.assertEqual(market_rows[0]["participant_trade_count"], "3")
    self.assertEqual(market_rows[0]["taker_trade_count"], "2")
    self.assertEqual(market_rows[0]["trade_history_volume"], "15")
    self.assertEqual(market_rows[0]["top10_wallet_volume"], "15")
    self.assertEqual(market_rows[0]["top10_wallet_volume_share"], "1")
    self.assertEqual(market_rows[0]["platform_reported_volume"], "100")
    self.assertEqual(market_rows[0]["volume_gap"], "85")
    self.assertEqual(market_rows[0]["data_status"], "truncated")
    self.assertEqual(daily_rows[0]["active_wallet_count"], "4")
    self.assertEqual(daily_rows[0]["participant_trade_count"], "5")
    self.assertEqual(daily_rows[0]["taker_trade_count"], "3")
    self.assertEqual(daily_rows[0]["trade_history_volume"], "30")
    self.assertEqual(monthly_rows[0]["active_wallet_count"], "4")
    self.assertEqual(monthly_rows[0]["trade_history_volume"], "30")

def test_kalshi_wallet_rows_are_not_available(self):
    platform_rows = [
        {
            "platform": "kalshi",
            "date_utc": "2026-02-01",
            "month_utc": "2026-02",
            "market_type": "15min",
            "total_volume": "123",
        }
    ]
    rows = report.build_kalshi_wallet_unavailable_rows(platform_rows)
    self.assertEqual(rows[0]["platform"], "kalshi")
    self.assertEqual(rows[0]["date_utc"], "2026-02-01")
    self.assertEqual(rows[0]["data_status"], "not_available")
    self.assertEqual(rows[0]["unavailable_reason"], "kalshi_public_trades_have_no_wallet_identifier")
```

- [ ] **Step 2: Run the failing aggregation tests**

Run:

```bash
python3 -m unittest \
  tests.test_btc_updown_volume_report.BtcUpdownVolumeReportTests.test_wallet_market_daily_and_monthly_aggregation_dedupes_wallets_within_bucket \
  tests.test_btc_updown_volume_report.BtcUpdownVolumeReportTests.test_kalshi_wallet_rows_are_not_available
```

Expected: fail because the aggregation functions do not exist.

- [ ] **Step 3: Implement aggregation helpers**

Add helpers after the existing combined aggregators:

```python
def decimal_ratio(numerator: Decimal, denominator: Decimal) -> str:
    if denominator == 0:
        return ""
    return format_decimal((numerator / denominator).quantize(Decimal("0.0001")))


def volume_gap_pct(gap: Decimal, denominator: Decimal) -> str:
    if denominator == 0:
        return ""
    return format_decimal((gap / denominator).quantize(Decimal("0.0001")))


def aggregate_wallet_activity(detail_rows: list[dict], trades_by_market: dict[str, dict]) -> tuple[list[dict], list[dict], list[dict]]:
    market_rows = []
    daily_active_wallets: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    daily_taker_wallet_sizes: dict[tuple[str, str, str], dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    daily_participant_counts: dict[tuple[str, str, str], int] = defaultdict(int)
    daily_taker_counts: dict[tuple[str, str, str], int] = defaultdict(int)
    daily_trade_totals: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    daily_platform_totals: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    daily_statuses: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    monthly_active_wallets: dict[tuple[str, str], set[str]] = defaultdict(set)
    monthly_taker_wallet_sizes: dict[tuple[str, str], dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    monthly_participant_counts: dict[tuple[str, str], int] = defaultdict(int)
    monthly_taker_counts: dict[tuple[str, str], int] = defaultdict(int)
    monthly_trade_totals: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    monthly_platform_totals: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    monthly_statuses: dict[tuple[str, str], set[str]] = defaultdict(set)

    for detail in detail_rows:
        if detail["platform"] != "polymarket":
            continue
        condition_id = detail["platform_market_id"]
        trade_group = trades_by_market.get(
            condition_id,
            {
                "participant_trades": [],
                "participant_status": "complete",
                "taker_trades": [],
                "taker_status": "complete",
            },
        )
        participant_trades = trade_group["participant_trades"]
        taker_trades = trade_group["taker_trades"]
        participant_wallets = {trade["wallet"] for trade in participant_trades}
        taker_wallet_sizes: dict[str, Decimal] = defaultdict(Decimal)
        for trade in taker_trades:
            taker_wallet_sizes[trade["wallet"]] += trade["size"]
        trade_total = sum(taker_wallet_sizes.values(), Decimal("0"))
        top10_total = sum(sorted(taker_wallet_sizes.values(), reverse=True)[:10], Decimal("0"))
        platform_total = decimal_from_value(detail["volume"], "detail.volume")
        gap = platform_total - trade_total
        daily_key = (detail["date_utc"], detail["month_utc"], detail["market_type"])
        monthly_key = (detail["month_utc"], detail["market_type"])
        daily_active_wallets[daily_key].update(participant_wallets)
        monthly_active_wallets[monthly_key].update(participant_wallets)
        for wallet, size in taker_wallet_sizes.items():
            daily_taker_wallet_sizes[daily_key][wallet] += size
            monthly_taker_wallet_sizes[monthly_key][wallet] += size
        daily_participant_counts[daily_key] += len(participant_trades)
        daily_taker_counts[daily_key] += len(taker_trades)
        daily_trade_totals[daily_key] += trade_total
        daily_platform_totals[daily_key] += platform_total
        daily_statuses[daily_key].update({trade_group["participant_status"], trade_group["taker_status"]})
        monthly_participant_counts[monthly_key] += len(participant_trades)
        monthly_taker_counts[monthly_key] += len(taker_trades)
        monthly_trade_totals[monthly_key] += trade_total
        monthly_platform_totals[monthly_key] += platform_total
        monthly_statuses[monthly_key].update({trade_group["participant_status"], trade_group["taker_status"]})
        market_rows.append(
            {
                "asset": ASSET_KEY,
                "platform": "polymarket",
                "month_utc": detail["month_utc"],
                "date_utc": detail["date_utc"],
                "market_type": detail["market_type"],
                "platform_series": detail["platform_series"],
                "platform_market_id": condition_id,
                "title": detail["title"],
                "active_wallet_count": str(len(participant_wallets)),
                "participant_trade_count": str(len(participant_trades)),
                "taker_trade_count": str(len(taker_trades)),
                "trade_history_volume": format_decimal(trade_total),
                "top10_wallet_volume": format_decimal(top10_total),
                "top10_wallet_volume_share": decimal_ratio(top10_total, trade_total),
                "platform_reported_volume": format_decimal(platform_total),
                "volume_gap": format_decimal(gap),
                "volume_gap_pct": volume_gap_pct(gap, platform_total),
                "data_status": (
                    "truncated"
                    if "truncated" in {trade_group["participant_status"], trade_group["taker_status"]}
                    else "complete"
                ),
            }
        )

    daily_rows = []
    for key in sorted(daily_trade_totals, key=lambda item: (item[1], item[0], market_type_sort_key(item[2]))):
        date_utc, month_utc, market_type = key
        active_wallets = daily_active_wallets[key]
        taker_wallet_sizes = daily_taker_wallet_sizes[key]
        trade_total = daily_trade_totals[key]
        top10_total = sum(sorted(taker_wallet_sizes.values(), reverse=True)[:10], Decimal("0"))
        platform_total = daily_platform_totals[key]
        gap = platform_total - trade_total
        daily_rows.append(
            {
                "asset": ASSET_KEY,
                "platform": "polymarket",
                "date_utc": date_utc,
                "month_utc": month_utc,
                "market_type": market_type,
                "active_wallet_count": str(len(active_wallets)),
                "participant_trade_count": str(daily_participant_counts[key]),
                "taker_trade_count": str(daily_taker_counts[key]),
                "trade_history_volume": format_decimal(trade_total),
                "top10_wallet_volume": format_decimal(top10_total),
                "top10_wallet_volume_share": decimal_ratio(top10_total, trade_total),
                "platform_reported_volume": format_decimal(platform_total),
                "volume_gap": format_decimal(gap),
                "volume_gap_pct": volume_gap_pct(gap, platform_total),
                "data_status": "truncated" if "truncated" in daily_statuses[key] else "complete",
                "unavailable_reason": "",
            }
        )

    monthly_rows = []
    for key in sorted(monthly_trade_totals, key=lambda item: (item[0], market_type_sort_key(item[1]))):
        month_utc, market_type = key
        active_wallets = monthly_active_wallets[key]
        taker_wallet_sizes = monthly_taker_wallet_sizes[key]
        trade_total = monthly_trade_totals[key]
        top10_total = sum(sorted(taker_wallet_sizes.values(), reverse=True)[:10], Decimal("0"))
        platform_total = monthly_platform_totals[key]
        gap = platform_total - trade_total
        monthly_rows.append(
            {
                "asset": ASSET_KEY,
                "platform": "polymarket",
                "month_utc": month_utc,
                "market_type": market_type,
                "active_wallet_count": str(len(active_wallets)),
                "participant_trade_count": str(monthly_participant_counts[key]),
                "taker_trade_count": str(monthly_taker_counts[key]),
                "trade_history_volume": format_decimal(trade_total),
                "top10_wallet_volume": format_decimal(top10_total),
                "top10_wallet_volume_share": decimal_ratio(top10_total, trade_total),
                "platform_reported_volume": format_decimal(platform_total),
                "volume_gap": format_decimal(gap),
                "volume_gap_pct": volume_gap_pct(gap, platform_total),
                "data_status": "truncated" if "truncated" in monthly_statuses[key] else "complete",
                "unavailable_reason": "",
            }
        )
    return market_rows, daily_rows, monthly_rows
```

Add:

```python
def build_kalshi_wallet_unavailable_rows(platform_rows: list[dict]) -> list[dict]:
    rows = []
    for row in platform_rows:
        if row["platform"] != "kalshi":
            continue
        unavailable_row = {
            "asset": ASSET_KEY,
            "platform": "kalshi",
            "month_utc": row["month_utc"],
            "market_type": row["market_type"],
            "active_wallet_count": "",
            "participant_trade_count": "",
            "taker_trade_count": "",
            "trade_history_volume": "",
            "top10_wallet_volume": "",
            "top10_wallet_volume_share": "",
            "platform_reported_volume": row["total_volume"],
            "volume_gap": "",
            "volume_gap_pct": "",
            "data_status": "not_available",
            "unavailable_reason": "kalshi_public_trades_have_no_wallet_identifier",
        }
        if "date_utc" in row:
            unavailable_row["date_utc"] = row["date_utc"]
        rows.append(unavailable_row)
    return rows
```

- [ ] **Step 4: Re-run aggregation tests**

Run the same command from Step 2.

Expected: pass.

## Task 5: Collect Wallet Rows During Report Generation

**Files:**
- Modify: `scripts/btc_updown_volume_report.py`
- Test: `tests/test_btc_updown_volume_report.py`

- [ ] **Step 1: Write a failing generation-unit test**

Add:

```python
def test_collect_polymarket_wallet_rows_fetches_each_polymarket_market(self):
    detail_rows = [
        {
            "platform": "polymarket",
            "month_utc": "2026-02",
            "date_utc": "2026-02-01",
            "market_type": "hourly",
            "platform_series": "btc-up-or-down-hourly",
            "platform_market_id": "0x" + "a" * 64,
            "title": "Bitcoin Up or Down - February 1",
            "volume": "1",
        },
        {
            "platform": "kalshi",
            "month_utc": "2026-02",
            "date_utc": "2026-02-01",
            "market_type": "15min",
            "platform_series": "KXBTC15M",
            "platform_market_id": "KXBTC15M-1",
            "title": "BTC 15M price up down",
            "volume": "1",
        },
    ]
    fetched = []

    def fake_fetch(condition_id, taker_only):
        fetched.append((condition_id, taker_only))
        return ([{"wallet": "0x1", "size": Decimal("1"), "raw": {"conditionId": condition_id}}], "complete")

    original = report.fetch_polymarket_trades_for_market
    report.fetch_polymarket_trades_for_market = fake_fetch
    try:
        trades_by_market, raw_records, anomalies = report.collect_polymarket_wallet_trades(detail_rows)
    finally:
        report.fetch_polymarket_trades_for_market = original

    self.assertEqual(fetched, [("0x" + "a" * 64, False), ("0x" + "a" * 64, True)])
    self.assertIn("0x" + "a" * 64, trades_by_market)
    self.assertEqual(len(raw_records), 2)
    self.assertEqual(anomalies, [])
```

- [ ] **Step 2: Run the failing test**

Run:

```bash
python3 -m unittest tests.test_btc_updown_volume_report.BtcUpdownVolumeReportTests.test_collect_polymarket_wallet_rows_fetches_each_polymarket_market
```

Expected: fail because `collect_polymarket_wallet_trades` does not exist.

- [ ] **Step 3: Implement collection**

Add:

```python
def collect_polymarket_wallet_trades(detail_rows: list[dict]) -> tuple[dict[str, dict], list[dict], list[str]]:
    trades_by_market = {}
    raw_records = []
    anomalies = []
    for row in detail_rows:
        if row["platform"] != "polymarket":
            continue
        condition_id = row["platform_market_id"]
        participant_trades, participant_status = fetch_polymarket_trades_for_market(condition_id, taker_only=False)
        taker_trades, taker_status = fetch_polymarket_trades_for_market(condition_id, taker_only=True)
        trades_by_market[condition_id] = {
            "participant_trades": participant_trades,
            "participant_status": participant_status,
            "taker_trades": taker_trades,
            "taker_status": taker_status,
        }
        for taker_only, trades in ((False, participant_trades), (True, taker_trades)):
            for trade in trades:
                raw_records.append(
                    {
                        "platform": "polymarket",
                        "asset": ASSET_KEY,
                        "month_utc": row["month_utc"],
                        "market_type": row["market_type"],
                        "platform_market_id": condition_id,
                        "taker_only": taker_only,
                        "trade": trade["raw"],
                    }
                )
        if participant_status == "truncated" or taker_status == "truncated":
            anomalies.append(f"Polymarket trades truncated for {condition_id}")
        print(
            f"polymarket trades {row['month_utc']} {row['market_type']} {condition_id}: "
            f"{len(participant_trades)} participant rows ({participant_status}), "
            f"{len(taker_trades)} taker rows ({taker_status})",
            flush=True,
        )
    return trades_by_market, raw_records, anomalies
```

- [ ] **Step 4: Re-run the focused test**

Run the same command from Step 2.

Expected: pass.

## Task 6: Render Wallet Metrics in Markdown and Write Outputs

**Files:**
- Modify: `scripts/btc_updown_volume_report.py`
- Test: `tests/test_btc_updown_volume_report.py`

- [ ] **Step 1: Write a failing Markdown rendering test**

Add:

```python
def test_markdown_report_includes_wallet_activity_section(self):
    report.configure_asset("btc")
    text = report.build_markdown_report(
        detail_rows=[],
        platform_daily_rows=[],
        platform_monthly_rows=[],
        combined_daily_rows=[],
        combined_monthly_rows=[],
        wallet_daily_rows=[
            {
                "asset": "btc",
                "platform": "polymarket",
                "date_utc": "2026-02-01",
                "month_utc": "2026-02",
                "market_type": "hourly",
                "active_wallet_count": "3",
                "participant_trade_count": "6",
                "taker_trade_count": "4",
                "trade_history_volume": "10",
                "top10_wallet_volume": "9",
                "top10_wallet_volume_share": "0.9",
                "platform_reported_volume": "11",
                "volume_gap": "1",
                "volume_gap_pct": "0.0909",
                "data_status": "complete",
                "unavailable_reason": "",
            },
        ],
        wallet_monthly_rows=[
            {
                "asset": "btc",
                "platform": "polymarket",
                "month_utc": "2026-02",
                "market_type": "hourly",
                "active_wallet_count": "3",
                "participant_trade_count": "6",
                "taker_trade_count": "4",
                "trade_history_volume": "10",
                "top10_wallet_volume": "9",
                "top10_wallet_volume_share": "0.9",
                "platform_reported_volume": "11",
                "volume_gap": "1",
                "volume_gap_pct": "0.0909",
                "data_status": "complete",
                "unavailable_reason": "",
            },
            {
                "asset": "btc",
                "platform": "kalshi",
                "month_utc": "2026-02",
                "market_type": "15min",
                "active_wallet_count": "",
                "participant_trade_count": "",
                "taker_trade_count": "",
                "trade_history_volume": "",
                "top10_wallet_volume": "",
                "top10_wallet_volume_share": "",
                "platform_reported_volume": "5",
                "volume_gap": "",
                "volume_gap_pct": "",
                "data_status": "not_available",
                "unavailable_reason": "kalshi_public_trades_have_no_wallet_identifier",
            },
        ],
        anomalies=[],
        kalshi_cutoff=datetime(2026, 3, 15, tzinfo=timezone.utc),
    )
    self.assertIn("## Wallet Activity Daily", text)
    self.assertIn("## Wallet Activity Monthly", text)
    self.assertIn("| polymarket | 2026-02-01 | hourly | 3 | 0.9 | complete |", text)
    self.assertIn("| polymarket | 2026-02 | hourly | 3 | 0.9 | complete |", text)
    self.assertIn("kalshi_public_trades_have_no_wallet_identifier", text)
```

- [ ] **Step 2: Run the failing test**

Run:

```bash
python3 -m unittest tests.test_btc_updown_volume_report.BtcUpdownVolumeReportTests.test_markdown_report_includes_wallet_activity_section
```

Expected: fail because `build_markdown_report` does not accept wallet activity rows.

- [ ] **Step 3: Update report rendering**

Change `build_markdown_report` signature:

```python
def build_markdown_report(
    detail_rows: list[dict],
    platform_daily_rows: list[dict],
    platform_monthly_rows: list[dict],
    combined_daily_rows: list[dict],
    combined_monthly_rows: list[dict],
    wallet_daily_rows: list[dict],
    wallet_monthly_rows: list[dict],
    anomalies: list[str],
    kalshi_cutoff: datetime,
) -> str:
```

Add this section before `## Kalshi Coverage Notes`:

```python
lines.extend(
    [
        "",
        "Wallet metric notes:",
        "- `active_wallet_count` uses Polymarket `takerOnly=false` participant rows and counts unique wallets.",
        "- `top10_wallet_volume_share` uses Polymarket `takerOnly=true` rows so the denominator stays market-volume aligned.",
        "- Kalshi wallet metrics are `not_available` because public Kalshi trades do not expose wallet identifiers.",
        "",
        "## Wallet Activity Daily",
        "",
        "| platform | date_utc | market_type | active_wallet_count | top10_wallet_volume_share | data_status |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
)
for row in wallet_daily_rows:
    lines.append(
        "| "
        + " | ".join(
            [
                row["platform"],
                row["date_utc"],
                row["market_type"],
                row["active_wallet_count"],
                row["top10_wallet_volume_share"],
                row["data_status"],
            ]
        )
        + " |"
    )

lines.extend(
    [
        "",
        "## Wallet Activity Monthly",
        "",
        "| platform | month_utc | market_type | active_wallet_count | top10_wallet_volume_share | data_status |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
)
for row in wallet_monthly_rows:
    lines.append(
        "| "
        + " | ".join(
            [
                row["platform"],
                row["month_utc"],
                row["market_type"],
                row["active_wallet_count"],
                row["top10_wallet_volume_share"],
                row["data_status"],
            ]
        )
        + " |"
    )

unavailable_reasons = sorted(
    {row["unavailable_reason"] for row in wallet_daily_rows + wallet_monthly_rows if row["unavailable_reason"]}
)
if unavailable_reasons:
    lines.extend(["", "Wallet metric unavailable reasons:"])
    lines.extend(f"- `{reason}`" for reason in unavailable_reasons)
```

Update the output files list with:

```python
f"- Polymarket raw trades snapshot: `{POLYMARKET_TRADES_RAW_PATH.relative_to(ROOT)}`",
f"- Wallet market activity CSV: `{WALLET_MARKET_PATH.relative_to(ROOT)}`",
f"- Wallet daily activity CSV: `{WALLET_DAILY_PATH.relative_to(ROOT)}`",
f"- Wallet monthly activity CSV: `{WALLET_MONTHLY_PATH.relative_to(ROOT)}`",
```

- [ ] **Step 4: Wire generation outputs**

In `generate_report`, after existing monthly aggregations:

```python
trades_by_market, wallet_raw_records, wallet_anomalies = collect_polymarket_wallet_trades(detail_rows)
wallet_market_rows, wallet_daily_rows, wallet_monthly_rows = aggregate_wallet_activity(detail_rows, trades_by_market)
wallet_daily_rows.extend(build_kalshi_wallet_unavailable_rows(platform_daily_rows))
wallet_monthly_rows.extend(build_kalshi_wallet_unavailable_rows(platform_monthly_rows))
anomalies.extend(wallet_anomalies)

write_jsonl_gzip(POLYMARKET_TRADES_RAW_PATH, wallet_raw_records)
write_csv(WALLET_MARKET_PATH, WALLET_MARKET_FIELDS, wallet_market_rows)
write_csv(WALLET_DAILY_PATH, WALLET_DAILY_FIELDS, wallet_daily_rows)
write_csv(WALLET_MONTHLY_PATH, WALLET_MONTHLY_FIELDS, wallet_monthly_rows)
```

Pass `wallet_daily_rows` and `wallet_monthly_rows` into `build_markdown_report`, return the wallet rows from `generate_report`, and print the new output paths in `main`.

- [ ] **Step 5: Re-run the focused Markdown test**

Run the same command from Step 2.

Expected: pass.

## Task 7: Ignore Generated Wallet Artifacts

**Files:**
- Modify: `.gitignore`

- [ ] **Step 1: Add ignore patterns**

Add:

```gitignore
data/raw/*_updown_polymarket_trades_*.jsonl.gz
outputs/*_updown_wallet_activity_by_market_*.csv
outputs/*_updown_wallet_activity_daily_by_platform_*.csv
outputs/*_updown_wallet_activity_monthly_by_platform_*.csv
```

- [ ] **Step 2: Verify generated paths stay untracked**

Run:

```bash
git check-ignore \
  data/raw/btc_updown_polymarket_trades_2026-02_2026-04.jsonl.gz \
  outputs/btc_updown_wallet_activity_by_market_2026-02_2026-04.csv \
  outputs/btc_updown_wallet_activity_daily_by_platform_2026-02_2026-04.csv \
  outputs/btc_updown_wallet_activity_monthly_by_platform_2026-02_2026-04.csv
```

Expected: all four paths are printed.

## Task 8: Final Validation After Implementation

**Files:**
- Modify: none beyond prior tasks.

- [ ] **Step 1: Run unit tests**

Run:

```bash
python3 -m unittest discover -s tests
```

Expected: all tests pass.

- [ ] **Step 2: Run BTC report generation**

Run:

```bash
python3 scripts/btc_updown_volume_report.py --asset btc
```

Expected:

- exits with code `0`
- writes existing BTC volume outputs
- writes BTC wallet outputs
- BTC Markdown report contains `## Wallet Activity Daily`
- BTC Markdown report contains `## Wallet Activity Monthly`
- Kalshi wallet rows are marked `not_available`

- [ ] **Step 3: Run ETH report generation**

Run:

```bash
python3 scripts/btc_updown_volume_report.py --asset eth
```

Expected:

- exits with code `0`
- writes existing ETH volume outputs
- writes ETH wallet outputs
- ETH Markdown report contains `## Wallet Activity Daily`
- ETH Markdown report contains `## Wallet Activity Monthly`
- Kalshi wallet rows are marked `not_available`

- [ ] **Step 4: Run structural wallet checks**

Run:

```bash
python3 - <<'PY'
import csv
from decimal import Decimal

for asset in ("btc", "eth"):
    for grain in ("daily", "monthly"):
        path = f"outputs/{asset}_updown_wallet_activity_{grain}_by_platform_2026-02_2026-04.csv"
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert rows, path
        poly_rows = [row for row in rows if row["platform"] == "polymarket"]
        kalshi_rows = [row for row in rows if row["platform"] == "kalshi"]
        assert poly_rows, f"missing polymarket {grain} rows for {asset}"
        assert kalshi_rows, f"missing kalshi {grain} rows for {asset}"
        for row in poly_rows:
            assert row["data_status"] in {"complete", "truncated"}, row
            if row["trade_history_volume"]:
                share = Decimal(row["top10_wallet_volume_share"])
                assert Decimal("0") <= share <= Decimal("1"), row
        for row in kalshi_rows:
            assert row["data_status"] == "not_available", row
            assert row["unavailable_reason"] == "kalshi_public_trades_have_no_wallet_identifier", row
print("wallet structural checks passed")
PY
```

Expected: prints `wallet structural checks passed`.

## Self-Review

- Spec coverage:
  - Active wallet counts are defined and scoped by asset, platform, day, month, and market type.
  - Top 10 wallet volume share is defined with a same-source trade-history denominator.
  - Existing BTC/ETH volume report remains the report destination.
  - Kalshi public data limitation is explicit and does not fabricate wallet stats.
- Placeholder scan:
  - No placeholder markers or unspecified edge handling remain in this plan.
- Risk notes:
  - Polymarket `/trades` pagination is capped by documented `offset` and `limit` ranges. The implementation must mark rows `truncated` if the final reachable page is full.
  - `takerOnly=false` is required only for active wallet identity coverage. It must not feed `trade_history_volume`, Top 10 volume share, or `volume_gap`; those use `takerOnly=true`.
