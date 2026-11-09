"""User-approved English BUY report copy, shared by the daily report pages."""

COPY_VERSION = 'buy_activity_approved_v1'
BRAND = 'Polymarket · Crypto Up/Down'
OVERVIEW_TITLE = 'BUY Trade Activity'
MARKET_CONTEXT_TITLE = 'Market Context'
CONTEXT_LABELS = (
    'Taker-Side Market Volume',
    'Change vs Previous Day',
    'Active Trading Wallets — BUY or SELL',
    'Active Wallets With Existing Bot Labels',
    'Wallets Meeting the Daily Frequency Rule',
)
SUMMARY_TITLE = 'Reconstructed BUY Trades'
GROUP_ALL = 'All BUY Trades'
GROUP_BOTS = 'Trades With Suspected Bot Buyers'
SUMMARY_COLUMNS = ('Metric', GROUP_ALL, GROUP_BOTS)
SUMMARY_METRICS = (
    'Trades',
    'Share of All BUY Trades',
    'Matched Quantity — Shares',
    'Share of All BUY Shares',
    'Up-Equivalent Trade Notional — USDC',
    'Share of All BUY Up-Equivalent Notional',
)
OVERVIEW_NOTES = (
    'Each reconstructed match is counted once. The suspected-bot subset requires at least one labelled buyer.',
    'Shares measure matched trading quantity, not holdings. Up-equivalent notional measures trade participation, not wallet spending.',
    'Bot labels apply from the first observed qualifying market-end date onward. Rule: mean interval ≤60 seconds; trade span ≥90 minutes.',
    'Taker-side market volume and active-wallet counts are separate market-context measures.',
)
DISTRIBUTION_TITLE = 'BUY Trade Distribution by Up-Equivalent Price'
DISTRIBUTION_COLUMNS = (
    'Up Price — ¢', 'Trades', 'Trade Mix Within Group', 'Matched Shares',
    'Share of All BUY Shares', 'Up-Equivalent Notional — USDC', 'Share of All BUY Notional',
)
DISTRIBUTION_NOTES = (
    'Trade mix uses each group’s total trades. Shares and notional percentages use the corresponding totals for all BUY trades.',
    'Up-equivalent price: Up = p; Down = 1 − p. Notional = Up-equivalent price × matched shares.',
    'Only matches containing a BUY are included. Each match is counted once; trades are not unique orders.',
    'Markets are selected by UTC end date, including their associated early and cross-date trades.',
    'Values use unrounded calculations. Displayed amounts and shares have two decimals; rounded percentages may not sum exactly to totals.',
)
