# Changelog

All notable changes to fractrade-hl-simple will be documented in this file.

## [0.3.4] - 2026-03-27

### Performance

- **Pass cached meta/spot_meta to Exchange() constructor** — Eliminates 2-3 redundant API calls per authenticated client creation. Exchange's internal Info() was re-fetching meta and spot_meta from the API even though they were already cached.

## [0.3.3] - 2026-03-27

### Bug Fixes

- **`get_funding_rates()` and `get_portfolio()` now use SDK session** — Previously used raw `requests.post`, bypassing the SDK's session (and any proxy/retry configuration). Now routes through `self.info.post()`.

### New Features

- **Proxy support** — `HyperliquidClient(proxy={"https": "http://user:pass@host:port"})` routes all API calls through the specified proxy. Applied to all internal SDK sessions.

## [0.3.2] - 2026-03-27

### Bug Fixes

- **`get_user_state()` missing xyz: positions** — When `extended_universe=True`, `get_user_state()` now auto-merges positions and margin summaries across all perp dexes. Previously only queried the default clearinghouse, silently missing xyz: positions (commodities, indices, forex). Pass `dex=""` or `dex="xyz"` explicitly for per-dex isolation.
- **`get_perp_balance()` missing xyz: margin** — Now includes xyz margin in totals via the auto-merging `get_user_state()`.

### New Features

- **`bbo_cache` parameter for `maker_order()` and `get_optimal_limit_price()`** — Pass a pre-fetched best-bid/ask dict from a websocket feed to skip REST `get_order_book()` calls. Reduces API calls from 4 to 2 per maker order reprice cycle.
- **`fill_event` parameter for `maker_order()`** — Pass a `threading.Event` that your websocket fill handler sets. Replaces `time.sleep()` with `event.wait()` for instant fill detection without polling.

## [0.3.1] - 2026-03-26

### New Features

- **`maker_order()` / `maker_buy()` / `maker_sell()`** — Fee-optimized order execution using post_only orders with automatic chase and fallback. Places maker orders at best bid/ask, re-prices periodically, and falls back to IOC on timeout. Saves ~0.048% per trade (maker rebate vs taker fee).
  - Auto-detects optimal `timeout` and `reprice_interval` from order book spread width
  - Tight spread (<2 bps): 30s timeout, 3s reprice (BTC, ETH, SOL)
  - Medium spread (<20 bps): 45s timeout, 5s reprice (kPEPE, DOGE)
  - Wide spread (>20 bps): 60s timeout, 8s reprice (DYM, low-cap)
  - `fallback` parameter: "ioc" (default), "market", or "cancel"
  - Available as module-level functions via `from fractrade_hl_simple import maker_buy, maker_sell`

- **Order execution metadata** — `Order` dataclass now includes:
  - `is_maker: bool` — whether the order filled as maker or taker/IOC fallback
  - `attempts: int` — number of order placement attempts
  - `elapsed: float` — seconds from first attempt to fill
  - `spread_bps: float` — spread in basis points at time of execution

- **`get_spot_balance()` accepts `prices` parameter** — Pass a pre-fetched price dict to avoid redundant `get_price()` API calls when checking multiple wallets

### Bug Fixes

- **Bug 1: `get_spot_balance` returns $0 for USDC wallets** — USDC, USDT, USDC.e, and USDbC now default to $1 when not found in the perp price dict, since these stablecoins have no perp market
- **Bug 3: Every `HyperliquidClient()` makes unnecessary API calls** — `meta` and `spot_meta` responses are now cached and passed to `Info()` on subsequent instantiations, saving 2 API calls per client init
- **Bug 4: 429 rate limit errors crash instead of retrying** — `_with_retry` now catches SDK `ClientError` with status 429 and retries with exponential backoff, converting to `RateLimitException` after max retries
- **`remaining_size` bug in maker chase loop** — Partial fills during the maker order chase are now tracked correctly via order status API; remaining size decreases appropriately

### Performance

- **`cancel_all_orders()` uses bulk cancel** — Single `bulk_cancel` API call instead of N individual cancels. With 18 open orders, that's 1 API call instead of 18.
- **`cancel_all_spot_orders()` uses bulk cancel** — Same optimization for spot orders.
- **`open_long_position()` / `open_short_position()` reuse entry price** — SL/TP validation uses the entry order's limit price instead of calling `get_price()` again. Saves 1-2 API calls per trade with SL/TP.
- **`maker_order()` no-poll design** — Uses sleep + cancel + single status check pattern (4 API calls/cycle) instead of polling order status every second.

### Data Completeness

New fields added to capture previously dropped API data:

- **`Position.cum_funding`** (`CumFunding` dataclass) — Cumulative funding payments with `all_time`, `since_open`, and `since_change` breakdowns. Essential for true P&L calculation.
- **`Position.max_leverage`** — Maximum allowed leverage for this asset
- **`UserState.cross_maintenance_margin_used`** — Cross-margin maintenance margin usage. Critical for liquidation risk assessment.
- **`Fill.start_position`** — Position size before the fill. Enables reconstructing position history without querying all state changes.
- **`Fill.fee_token`** — Token the fee was paid in (e.g., "USDC")
- **`Order.children`** — Nested child orders for bracket/TP/SL chains
- **`Order.is_position_tpsl`** — Whether the order is a position management TP/SL
- **`Order.trigger_condition`** — Trigger evaluation type: "mark", "index", or "last"
- **Maker order fill price** — `maker_order()` now reports `average_fill_price` when detecting fills via order status API

### API Changes

- `get_spot_balance()` new optional `prices` parameter (backward-compatible)
- All new Order/Fill/Position fields use `Optional` defaults — fully backward-compatible
- All new functions (`maker_order`, `maker_buy`, `maker_sell`) added to `api.py` and `__init__.py` exports

## [0.3.0] - 2026-03-19

- Spot trading support (`spot_buy`, `spot_sell`, `get_spot_balance`, `get_spot_price`)
- Extended universe (xyz:) support for stocks, commodities, indices, forex
- `filled_size` fix: reports actual fill size instead of original requested size
- Vault/sub-account support

## [0.2.2] - 2026-03-15

- Vault/sub-account support via `is_vault` parameter

## [0.2.1] - 2026-03-14

- Fix `filled_size` reporting original requested size instead of actual fill

## [0.2.0] - 2026-03-13

- Leverage management, order tracking, bulk orders
- Retry logic with exponential backoff
- Price formatting overhaul
- Reliability fixes

## [0.1.0] - 2026-03-01

- Initial release
- Simple `buy()`, `sell()`, `close()` interface
- Market and limit orders
- Position management
- Funding rates
