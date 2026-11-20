# Changelog

All notable changes to fractrade-hl-simple will be documented in this file.

## [Unreleased]

Orders you can rely on: no order is sent twice, the exchange's answer to every signed action is checked, and the library's errors are documented exceptions. Many fixes change what a call returns or raises, so read "Behaviour changes to check in your code" before you upgrade. The methods are described in the [README](README.md), the MCP server in [docs/mcp.md](docs/mcp.md).

### Bug Fixes

Orders:

- **An order could be sent twice.** Orders, modifies, transfers and margin changes were retried after network errors and timeouts, even when the exchange had executed them. They are now retried only when the request provably did not reach the exchange. An order whose answer is lost is looked up by its client order id; if the outcome cannot be settled, `OrderOutcomeUnknownException` is raised (`ActionOutcomeUnknownException` for other actions). Neither is one of the error types raised before, on purpose: a handler that sends again on those would execute the action twice.
- **Prices from 100000 on were cut to 5 significant figures**: a limit of 100006 was sent as 100010, so a buy could fill above its limit. They are now rounded to a whole number, which Hyperliquid accepts.
- **Sizes could be rounded up**, so an order could be larger than requested. They are rounded down to the lot size.
- **A price of 0, below 0 or NaN was signed and sent**, as was a NaN size, and a price that rounds to 0 at the market's precision. Each now raises `InvalidArgumentException` before anything is sent.
- **`post_only=True` did not send a post-only order.** It now uses Hyperliquid's `Alo` time in force.
- **Two actions of one key in the same millisecond collided**, as the nonce is the millisecond. The signed actions of a key are now sent one at a time, across all clients in the process, each a millisecond after the one before (the clock must be right: [README](README.md#client-configuration)).
- **Requests had no timeout**, so a stalled connection blocked forever. New `request_timeout`, 10 seconds by default.
- **Server errors (HTTP 5xx) were not retried on reads.**
- **`max_retries` and `retry_delay` were not checked**: with a negative `max_retries` nothing was sent. Values out of range raise `InvalidArgumentException`.
- **An order id that is not a whole number was cut off**: `cancel_order(10.9, "BTC")` canceled order 10. An order id is now an integer or its text; anything else, floats and booleans included, raises `InvalidArgumentException` (`cancel_order()`, `spot_cancel_order()`, `modify_order()`, `get_order_status()`, `get_open_order_by_id()`, `bulk_cancel()`).
- **`modify_order()` reported a modify that filled at once as a failure.**
- **`maker_order()` could fill more than the requested size** when it could not read the status of a canceled attempt; it now stops with `ActionOutcomeUnknownException`. After partial fills it reported only the last part; it now reports the total filled size and the average price. It retried every rejection until its timeout; it now retries only a post-only crossing.

Stop losses and take profits:

- **Short positions got no working stop loss or take profit**: `open_short_position()` placed both as sell orders. `stop_loss()` and `take_profit()` now take the closing side from the position when `is_buy` is omitted.
- **`open_long_position()` / `open_short_position()` checked the stop loss and take profit prices only after the entry was sent**, so a wrong price left an unprotected position. They are now checked first, for a market entry against the market price. After a partial fill the levels are sized to the filled amount.
- **`update_stop_loss()` / `update_take_profit()` could leave a position unprotected or doubly protected**: they canceled the old order before the new one was placed. The new order is now placed first; while the position is open, the old ones are canceled only once it is read open and covers the position.
- **Market stop losses and take profits were sent with their trigger price as limit price**: if Hyperliquid bounds the fill by it (it does not document whether it does), a stop in a falling market, or a take profit when the price turns right after triggering, might not fill. Their limit price is now beyond the trigger, by Hyperliquid's documented 10 % tolerance for market TP/SL (a sell 10 % below, a buy 10 % above), so `Order.limit_price` shows that price.
- **`update_stop_loss()` / `update_take_profit()` also canceled entry orders**: every stop (take profit) trigger of the market, also one on the side that opens or adds to the position, such as a buy stop above the price while long. Only those on the side that closes the position are replaced now. `get_stop_loss_price()` and `get_take_profit_price()` read the same way, and return None without a position.

Balances and margin:

- **Balances of unified accounts were wrong.** A unified account (the default for accounts created in the Hyperliquid app since February 2026) keeps one balance per token in the spot account; `get_perp_balance()` returned only the part backing positions. The balance methods now follow the account mode (new `get_account_mode()`).
- **`get_all_balances()` always raised `AttributeError`.**
- **Spot balances were valued at perp prices**: a token without a perp counted as $0, and one that shares its name with a perp got the perp's price (spot TRUMP: about 1400 times its own). Each token is now valued at its own USDC pair: USDC at 1, a token without such a pair or its mid at 0. Other stablecoins are valued at their pair too, no longer at 1 by name.
- **`set_leverage(is_cross=True)` was sent for markets that allow isolated margin only**, and `add_isolated_margin()` accepted zero and negative amounts (the exchange removes margin for a negative one). Both now raise `ValueError` before sending.

Reads and client setup:

- **`get_funding_history()` and `get_fills_by_time()` returned only the first page** (500 rates, 2000 fills), without a sign of the cut. Both now read page after page.
- **A reduce-only order that Hyperliquid shrank looked partly filled.** Hyperliquid shrinks a resting reduce-only order to the position and reports only the sizes placed and open. `Order` gains `filled_size_known`: `False` when the difference may be a shrink.
- **`Order.order_type` lost its strings**: the time in force and a trigger's `tpsl` came out as `True`. Open Stop Limit and Take Profit Limit orders were marked `isMarket: True`.
- **`get_spot_fills()` and `get_spot_open_orders()` raised `IndexError` on mainnet**, and every spot order was reported as a plain limit order. Pairs quoted in another token than USDC are now reported as `BASE/QUOTE` (`HYPE/USDT0`).
- **Clients shared market metadata across networks and dex lists**: a testnet client could use mainnet metadata, an `extended_universe=True` client specs without the `xyz:` markets.
- **`refresh_market_specs()` did not make new markets tradeable**: an order on one failed inside the SDK with a `KeyError`.
- **`proxy=` was not used for the requests a client sends while it is created.**
- **Creating a client requested the market metadata twice.**
- **The `api` functions lacked parameters of the client methods they call** (`buy`/`sell` had no `reduce_only`, `post_only` or `slippage`) and refused an account given as a dict. Each now takes every parameter of its method; the added ones are keyword-only.

### New Features

- **MCP server `fractrade-hl-mcp`** (extra `mcp`): Claude Code, Codex and Claude Desktop read your Hyperliquid accounts and markets. It is read only by default and then loads no private key. With `--enable-trading` it adds trade tools for the accounts you allow: orders that add risk are checked against the account's limits (closes, cancels and stops are exempt from all but the slippage cap of a close; two limits only warn unless you set them to block), and it signs only with an API wallet. Setup: [docs/mcp-setup.md](docs/mcp-setup.md); reference: [docs/mcp.md](docs/mcp.md).
- **Trigger entries**: `trigger_order()` places a stop order that opens or adds to a position once the price rises (buy) or falls (sell) to `trigger_price`, as a market order within Hyperliquid's 10 % tolerance or, with `limit_price`, a limit order. Hyperliquid attaches no stop loss to a stop order: place it once it fills. MCP tool `place_trigger_entry`.
- **A resting limit entry with its stop loss and take profit attached**: `open_long_position()` / `open_short_position()` with a Gtc or Alo `limit_price` and `stop_loss_price` / `take_profit_price` send them attached to the entry, as Hyperliquid's app does (it refused them before); Hyperliquid places them once the entry has filled. MCP `open_position` takes `limit_price`; `get_open_orders` shows attached orders in `children`.
- **Several stop losses and take profits** with `open_long_position()` / `open_short_position()`: `stop_losses=[(price, fraction), ...]` and `take_profits=[...]`. New `time_in_force="Ioc"` for a limit entry that executes at once.
- **`resize_protection()`**: the stop losses and take profits of an open position resized to its live size, level by level. It returns `ProtectionSync` and raises `ProtectionSyncIncompleteException` if the position is not protected as asked.
- **Client order ids**: `create_order()`, `buy()`, `sell()`, `stop_loss()`, `take_profit()`, `modify_order()` and the spot order methods take `cloid=`; without it they make a random one, returned in `Order.cloid`. Methods that place several orders give each its own. `bulk_order()` sends only the `cloid` given in an order's dict. New `get_order_status_by_cloid()` and `cancel_order_by_cloid()` find an order whose answer was lost.
- **`prepare_order()`**: the size and limit price an order would be sent with, without sending it.
- **Any HIP-3 dex**: `HyperliquidClient(perp_dexs=["xyz", "para"])`; `extended_universe=True` stays the same as `perp_dexs=["xyz"]`. New `get_perp_dexs()`, `get_leverage(symbol)` and `get_fee_rates(symbol)`; market specs gain `margin_mode`, `growth_mode`, `deployer_fee_scale` and `collateral`.
- **Read any wallet without its key**: every read about one wallet takes `address=`, and `HyperliquidClient(trust_env=False)` reads none of this library's settings from the environment: no `.env`, key, address or `HYPERLIQUID_ENV` (requests still applies HTTP proxy variables).
- **Account**: `get_account_summary()` (the balances by account mode) and `get_user_role()` (user, API wallet, vault or sub-account).
- **History**: `get_pnl()` (trades with their PnL, fees and funding), `get_fill_history()` (the fills of a time range, and whether Hyperliquid's limits cut it short), `get_user_funding()` (funding paid and received) and `get_historical_orders()` (recent orders with their status).
- **Markets**: `get_markets()`, `get_spot_markets()` and `get_candles()`. `coin_market(coin)` names a coin's kind of market (`perp`, `hip3_perp`, `spot`, `outcome`), also in the new `market` field of `Order` and `Fill`. `get_order_book(decimal=True)` returns `Decimal` values.
- **`base_url` selects the network, as in the Hyperliquid SDK**: `HyperliquidClient(base_url=constants.TESTNET_API_URL)`. Only the SDK's constants are accepted.
- **Documented exceptions**, each with a fixed `code` and `retryable`; a few failures keep a plain Python type ([README](README.md#error-handling)). Network and server failures raise the library's `NetworkException`, `TimeoutException` and `ServerErrorException`; each also is the requests or SDK type it replaces.
- **Type hints**: the package ships a `py.typed` marker.

### Behaviour changes to check in your code

Most new exceptions also are the `ValueError` or `RuntimeError` raised before, so existing handlers still catch them. Where that is not so, the entry says it.

Calls that now raise:

- **`close()`** raises `PositionNotClosedException` when its order filled only part of the position, or the exchange rejected it. Before, a partial fill returned the order, and a rejection was a `ValueError`, which the new exception is not.
- **`close_all_positions()`** tries every position and then raises `ClosePositionsIncompleteException` with the closed and the failed ones. Before, failures were only logged.
- **Cancels are checked.** `cancel_all_orders()`, `spot_cancel_all_orders()` and `bulk_cancel()` raise `OrdersNotCanceledException`, which names each order still open (`failed`), and those canceled and no longer open (`canceled`, `not_open`). `cancel_order()` and `spot_cancel_order()` return `False` for an order that already filled (it was `True`), and raise errors they used to turn into `False`.
- **`bulk_order()`** raises `OrderRejectedException` when the exchange refuses the whole action; rejections of single orders stay in the returned answer. An order dict without a required key raises `InvalidArgumentException` (it was `KeyError`), as do an `is_buy` or `reduce_only` that is not a boolean and an unknown `time_in_force`, before anything is sent.
- **`update_stop_loss()` / `update_take_profit()` / `trailing_stop()`** raise `ProtectionReplacementIncompleteException` (not a `ValueError`) when the new order does not cover the position, that could not be checked, or an old order could not be canceled. The new order is placed (`new_order`); `old_order_ids` are the old ones that may still be open. Before, they logged a warning.
- **`open_long_position()` / `open_short_position()`** raise `ProtectionOrderException` (not a `ValueError`) when the entry was placed but a stop loss or take profit was not; its `orders` holds what was placed.
- **`open_long_position()` / `open_short_position()` refuse a limit entry that can rest** (Gtc or Alo) together with stop losses or take profits. Before, the levels were placed for the full size while the entry was unfilled. Pass `time_in_force="Ioc"`, or place the levels once the entry fills.
- **`maker_order()`** raises `MakerOrderIncompleteException` (not a `ValueError`) when it filled part of the size and then stopped on an error. Send at most its `remaining` again. With `fallback="cancel"`, the returned order has `status="canceled"`; it was `"open"`.
- **`modify_order()`** keeps the type of its errors. Before, every error became `ValueError("Failed to modify order: ...")`.
- **`set_leverage()` and `add_isolated_margin()`** raise on exchange errors instead of returning the error dict.
- **`stop_loss()` / `take_profit()`** raise `ValueError` for an `is_buy` that would not close the position. `open_long_position()` / `open_short_position()` with `stop_loss_price=0` raise instead of placing no stop.
- **`post_only=True` without `limit_price` or `time_in_force`** raises `InvalidArgumentException`: a post-only order needs a price.
- **An unknown symbol** raises `UnknownSymbolException` (a `ValueError`) in `create_order()`, `buy()`, `sell()`, the cancel methods and `set_leverage()`, before anything is signed, and in market reads such as `get_price()`, `get_order_book()` and `get_funding_history()`. Some of these raised the SDK's `KeyError`, and `cancel_all_orders(symbol)` with a misspelled symbol returned as if it had canceled.
- **Reads about a wallet** raise `AddressRequiredException` without an address or account, and `ValueError("Invalid address format")` for a malformed address. An open order or balance that cannot be parsed raises; it was skipped.
- **Reads no longer wrap errors in `ValueError`**: `get_portfolio()`, `get_funding_rates()`, `get_order_book()` and `get_spot_price()` raise errors as they are (e.g. `NetworkException`), and a market without a usable price raises `HyperliquidException`.
- **A response body that is not JSON** is retried for a read and then raises `ServerErrorException`; for an order it is an unknown outcome.
- **Error messages changed** for exchange rejections (`"BTC order rejected: <Hyperliquid's text>"`; `.reason` holds the text alone) and for a missing key. `InsufficientMarginException` is an `OrderRejectedException` and takes `(action, reason)`: a single argument is now the action, not the message.

Changed defaults and values:

- **`create_order()` and `create_spot_order()` without `limit_price` default to IOC** (a market order), unless `time_in_force` is passed. The default was Gtc, so what did not fill at once stayed in the book.
- **`cancel_all_orders()`** (and its alias `cancel_all()`) cancels perp orders only. Call `spot_cancel_all_orders()` for spot orders.
- **`get_perp_balance()`** returns the spot USDC for a unified account and raises `UnsupportedAccountModeException` for a portfolio-margin account. Code that adds it and the spot USDC counts a unified balance twice; use `get_all_balances()`. Both balance methods make one more request (the account mode).
- **`get_spot_balance(prices=...)`** looks a price up under the USDC pair's name (`"PURR/USDC"`, `"@107"`), no longer under the token's name.
- **Open orders**: `size` is the original size (it was the remaining one; use `remaining_size`), and `trigger_price` is `None` for an order without a trigger (it was `0`).
- **`Order.order_id` is an `int`**, as Hyperliquid sends it; it was text. Methods that take an order id still accept text.
- **`Order.created_at` is `None`** for an order returned by placing or modifying it (it was the local time), and for an order read without a timestamp (it was `0`). `get_open_orders()` and `get_order_status()` have Hyperliquid's time. A placed order without a trigger has `trigger_condition="N/A"` and `is_position_tpsl=False`, as Hyperliquid reports open orders.
- **New fields**: `Order` has `cloid`, `market`, `status_time` and `filled_size_known`, `Fill` has `market` and `trade_id`. `dataclasses.asdict()` and equality include them.

Creating a client:

- **An account that cannot sign raises**: `InvalidArgumentException` for a missing or malformed key or address, `ConfigurationException` for a `HYPERLIQUID_PRIVATE_KEY` that cannot be used. Before, the client silently became read only.
- **A client is not created without its markets**: if they cannot be loaded, creating it raises (e.g. `NetworkException`). The built-in table `models.MARKET_SPECS` is removed.
- **No call reloads the markets on its own.** In a long-running program, call `refresh_market_specs()` when markets were listed; it raises on failure.
- **Importing the package no longer loads `.env`** into the process environment. It is loaded when the library reads its settings, and looked for from the working directory upwards, no longer from the folder the package is installed in.
- **`HYPERLIQUID_ENV` is read** (it was documented, but ignored): `testnet` selects testnet when neither `base_url` nor `env` is passed.

Removed and deprecated:

- `get_evm_balance()` is removed; it called an SDK method that does not exist.
- `env=` is deprecated; pass `base_url=`.
- `api.stop_loss(stop_price=...)` and `api.take_profit(take_profit_price=...)` are deprecated; the price is `trigger_price`.

### Dependencies

- New optional extra `mcp` (`mcp >=2.3,<3`, `jsonschema >=4.20`); a plain install is unchanged.
- `requests >=2.32.4` (CVE-2024-47081).
- `hyperliquid-python-sdk >=0.24.0,<0.25` (it was `>=0.22.0,<1.0.0`): SDK 0.22 and older can no longer build a client against mainnet, and each new SDK minor version is checked before it is allowed.
- `eth-account >=0.10.0` is now declared; the library imports it directly.

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
