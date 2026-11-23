# Trading with Fractrade and Hyperliquid in Claude

Use Fractrade to investigate a market, then use the Hyperliquid connector to inspect the
account, plan an entry, submit it and verify the result. Claude coordinates those steps.
Neither connector schedules Claude's next turn or manages a position between calls.

This guide describes **0.4.0**, including the [release changes](../CHANGELOG.md#040---2026-11-23).
Install the final build using the setup guide and restart/reconnect the server after an
update. Earlier release candidates may lack the behavior described here; verify the
installed version as well as tool discovery before relying on the guardrails.
For installation and keys, follow [MCP setup](mcp-setup.md); for individual tools, use the
[reference](mcp.md). Development and verification commands are in [CONTRIBUTING.md](../CONTRIBUTING.md).

## Connect and identify the two servers

Keep distinct names in Claude: this guide calls the market-intelligence server `fractrade`
and this package's execution server `hyperliquid`. Your installed aliases may differ.
Identify them by their tools: Fractrade offers `describe`, `markets`, `market` and `cost`;
Hyperliquid offers `list_accounts`, `plan_trade`, `get_changes` and, when enabled, `open_position`.
Connect Fractrade using its own service's connection instructions; this library does not
start or configure that service. The Hyperliquid connection is covered by
[setup step 4](mcp-setup.md#4-connect-your-assistant).

| Source | Use it for |
|---|---|
| Fractrade | Market context, trends, funding, detected activity and liquidity research |
| Hyperliquid connector | Current execution prices, the selected account's positions/orders, risk estimates, submissions and reconciliation |
| Claude and its host | The user's trading mandate, the working record, decisions and any scheduled follow-up |

Start with trading disabled to walk through research and planning. For submission, use the
account and limits the user authorized and the host's existing trade-approval settings.
Connecting another server is not permission to place orders.

## Worked example: investigate and plan a BTC entry

The call records below show a server alias, tool name and its arguments; they are not raw
MCP protocol messages. The trade figures are a hypothetical example: BTC around $85,000,
an account named `main`, a 0.001 BTC long, stop at $80,000 and target at $90,000. Replace
these with the user's chosen setup and current observations. Do not execute these prices
as a live recommendation.

### 1. Research the market

Read each Fractrade tool's full description before interpreting its data:

```json
[
  {"server":"fractrade","tool":"describe","arguments":{"tool":"markets"}},
  {"server":"fractrade","tool":"markets","arguments":{"only_markets":["hl:perp:BTC"],"max_rows":5}},
  {"server":"fractrade","tool":"describe","arguments":{"tool":"market"}},
  {"server":"fractrade","tool":"market","arguments":{"inst":"hl:perp:BTC","minutes":240,"max_points":120}},
  {"server":"fractrade","tool":"describe","arguments":{"tool":"cost"}},
  {"server":"fractrade","tool":"cost","arguments":{"inst":"hl:perp:BTC"}}
]
```

Use the trend, recent activity, funding and cost observations to assess the proposed setup.
`cost` describes its documented trade-size buckets; it is not a fill quote for your exact
order. Check source timestamps and coverage, not just when the answer arrived. Fractrade's
fields ending in `_ms` use UTC milliseconds; candle/chart series use seconds. Different
sources may cover different periods.

An `_cut` entry is `[kept, total]`: omitted rows are not evidence of no activity. Narrow the
request with `only_markets` or the tool's filters, then increase `max_rows` or `max_points`
if the missing detail matters. Use `fields` after `describe` establishes their meanings;
keep the timestamp and coverage fields needed to judge freshness. If data is stale or
missing, record that limitation and obtain a fresh observation before using it for entry.

Translate identifiers deliberately:

| Fractrade instrument | Hyperliquid argument |
|---|---|
| `hl:perp:BTC` | `symbol: "BTC"` |
| `hl:perp:xyz:NVDA` | `symbol: "xyz:NVDA"`; keep the dex prefix |
| `hl:spot:PURR/USDC` | Separate spot market; resolve its symbol with `list_markets` and `market: "spot"` |

The execution MCP trades perps. Never turn a spot instrument into a perp by stripping its
prefix. Fractrade market context also does not establish which Hyperliquid network your
account uses; a testnet order requires testnet execution data.

### 2. Select the account and check existing exposure

First call:

```json
{"server":"hyperliquid","tool":"list_accounts","arguments":{}}
```

Choose `main` only if it is the intended account. Record its `base_url`, mode, halt status,
limits and `perp_dexs`. Reads can default to a different wallet from trading calls, so pass
`account` on every wallet call. Market calls take `base_url`, not `account`. The following
example assumes the selected account reports mainnet:

```json
[
  {"server":"hyperliquid","tool":"get_account","arguments":{"account":"main"}},
  {"server":"hyperliquid","tool":"get_positions","arguments":{"account":"main"}},
  {"server":"hyperliquid","tool":"get_open_orders","arguments":{"account":"main"}},
  {"server":"hyperliquid","tool":"get_account_risk","arguments":{"account":"main"}},
  {"server":"hyperliquid","tool":"get_market_info","arguments":{"symbol":"BTC","base_url":"https://api.hyperliquid.xyz"}},
  {"server":"hyperliquid","tool":"get_order_book","arguments":{"symbol":"BTC","base_url":"https://api.hyperliquid.xyz"}}
]
```

Read all pending entries, not just BTC's: they consume the account's total-margin budget.
Check existing stop coverage, loss-limit warnings, account-value availability, leverage and
market status. Reads can cover the union of configured accounts' dexes; the account's
trading limits cover its own traded dexes. A position on another dex needs separate attention.
`enable_perp_dexs` changes session scope for every account at that network; it is not a
read-only workaround for a missing market.

Check each account/risk response's `observed_at` and `scope` before reusing its figures.
The timestamp is when the reads finished, not a simultaneous exchange snapshot. Scope
lists the perp dexes read (`""` for native perps), the symbol filter and, for open orders,
the perp/spot filter. An empty filtered result does not establish that the wallet has no
positions elsewhere. Compare account-report scope with the narrower scope of `plan_trade`
when interpreting differences in margin figures. Null values and warnings remain unknown
even when the response has a recent observation time.

If a new listing is visible but submission preparation reports `UNKNOWN_SYMBOL`, check the
exact name and network, then use [`refresh_market_specs`](mcp.md#refresh_market_specs).
Refreshing metadata does not enable another dex, and another chat may need its own refresh.

### 3. Plan the entry and review the limits

```json
{"server":"hyperliquid","tool":"plan_trade","arguments":{"account":"main","symbol":"BTC","side":"long","size":0.001,"stop_loss":80000,"take_profits":[{"price":90000,"fraction":1}]}}
```

Review `data.size`, entry estimate, margin, loss if all stops fill, `unprotected_size` and
warnings. To size from a user-chosen loss budget, replace `size` with **one** of `risk_usd`
or `risk_pct`; do not pass both. Estimates include fees and current-book slippage, but a
future stop can fill worse. A plan places nothing and reserves no funds.

Review `data.execution`: it runs the same preparation and configured order checks as
submission, including pending-entry margin. `eligible: false` names a blocker; `null`
means eligibility could not be established. A failed warn-only check does not block.
Check `trading_enabled` separately. The preview needs no signing key and reserves nothing;
it excludes signer authorization and exchange acceptance. Submission rechecks current state
and can return `GUARD_BLOCKED` after a previously eligible plan. Follow the host's existing
approval requirements before submitting.

| Setting or figure | Meaning for this decision |
|---|---|
| Margin | Entry value divided by the market's configured leverage; different from loss at the stop |
| `max_margin_pct` | Caps each proposed order's margin |
| `max_total_margin_pct` | Used position margin + remaining pending entry margin + proposed margin; a batch is summed together |
| `max_risk_pct` | Caps estimated loss at the stops for `open_position`; other risk-increasing entry tools lack that protection and are refused |
| `max_leverage` | Caps leverage changes made through `set_leverage`; it does not by itself check leverage previously set elsewhere |
| `check_order_leverage` | When enabled, checks the market's actual leverage before adding risk |
| `max_day_loss_mode`, `max_total_notional_mode` | `warn` reports a problem but permits submission; `block` refuses it; `off` disables that check |

For example, a $1,000 account with `max_total_margin_pct = 3` has a $30 budget. With no
position margin, a resting $100 entry at 5x consumes $20. A second $100 entry at 5x would
bring the estimate to $40 and is blocked. Only remaining order size counts as pending;
modifying an order replaces its old contribution. Reduce-only protection adds none, and
opposing non-reduce-only orders are not netted. This is checked at submission, not reserved
continuously against later price changes or orders sent elsewhere.

### 4. Submit the agreed plan once

This is the first trading call. Only proceed after the user-authorized account, risk and
entry decision are settled. If the plan still returns size `0.001`, the corresponding
market-entry request is:

```json
{"server":"hyperliquid","tool":"open_position","arguments":{"account":"main","symbol":"BTC","side":"long","size":0.001,"stop_losses":[{"price":80000,"fraction":1}],"take_profits":[{"price":90000,"fraction":1}]}}
```

For an eligible plan, `data.execution.arguments` supplies numeric arguments for
`open_position`. Keep the same side, time in force and protection levels. A limit-entry
plan uses `entry_price`; submission uses `limit_price`. Gtc limits (the default) support
one full-size stop and at most one full-size target. Market and **explicit IOC limit**
entries support multiple levels. For example, preview a price-capped entry with two stops
and two targets:

```json
{"server":"hyperliquid","tool":"plan_trade","arguments":{"account":"main","symbol":"BTC","side":"long","size":0.001,"entry_price":86000,"time_in_force":"Ioc","stop_losses":[{"price":80000,"fraction":0.5},{"price":79000,"fraction":0.5}],"take_profits":[{"price":90000,"fraction":0.5},{"price":95000,"fraction":0.5}]}}
```

Use current prices and the reviewed size; these numbers only illustrate the schema. IOC
fills immediately up to the cap and cancels the remainder. Protection is placed for the
filled amount, and an entry with no fill opens no position. It is not a resting entry.

Record the returned entry and protection order IDs/cloids and actual filled size. Inspect
both top-level guard `warnings` and `data.warnings`. Never infer a full fill or working
protection merely because the call succeeded.

### 5. Verify the resulting position

```json
[
  {"server":"hyperliquid","tool":"get_positions","arguments":{"account":"main","symbol":"BTC"}},
  {"server":"hyperliquid","tool":"get_open_orders","arguments":{"account":"main","symbol":"BTC"}},
  {"server":"hyperliquid","tool":"get_position_risk","arguments":{"account":"main","symbol":"BTC"}},
  {"server":"hyperliquid","tool":"get_changes","arguments":{"account":"main","symbol":"BTC"}}
]
```

If there is no position, `get_position_risk` returns `POSITION_NOT_FOUND`; inspect the entry
status rather than treating that as a failed submission. For a live position, compare actual
size and direction with the intended trade and check `unprotected_size`. Attached `children`
of a still-resting limit entry are not active protection. Verify again after replacing a stop
or canceling a partially filled parent entry.

Also inspect `get_changes.protection.coverage[].warnings`. `STOP_COVERAGE_GAP` names the
amount without counted stop coverage. `STOP_SIZE_EXCEEDS_POSITION` and
`TAKE_PROFIT_SIZE_EXCEEDS_POSITION` are informational review notices with order IDs and
excess quantities. For example, after reducing 1 BTC to 0.6 BTC, a remaining 0.5 BTC target
now represents about 83% of the position. If two such targets remain, their 1 BTC total
exceeds the position even though its stops may still cover all of it.

Compare these notices with the intended exit plan. Multiple levels, overlapping stops
and partial target coverage can be deliberate; whole-position protection is treated
separately from fixed-size excess. Use `sync_protection` only when resizing the supported
levels is appropriate and authorized, then verify the resulting orders. Native position
TP/SL and orders with attached children are left untouched by that tool. `get_changes`
performs no automatic repairs.

| Result or event | Next action |
|---|---|
| Partial market fill | Reconcile the filled size and its protection. Do not repeat the original entry to fill the remainder automatically |
| Partial Gtc limit fill | Attached protection is still waiting for the parent to finish; the filled part may have no stop. If canceling the remainder, its attached children are canceled too: read the remaining position and place/verify its required protection within the user's mandate |
| `PROTECTION_NOT_PLACED` or incomplete stop replacement | Read `details`, current orders and the position. Repair only the missing protection, or reduce/close exposure as authorized; do not repeat the entry |
| `OUTCOME_UNKNOWN`, or no answer after submission | Follow the returned IDs/cloids and hints using `get_order_status`, `get_order_status_by_cloid`, fills, positions and orders. `found: false` can mean it has not arrived yet; it does not permit a replacement entry |
| `GUARD_BLOCKED` | Explain the failed checks or unavailable input. Reassess the plan; do not loosen limits automatically |
| Emergency stop | `kill_switch` halts new risk and cancels non-reduce-only perp orders on traded dexes. Independent reduce-only protection stays; attached protection goes with a canceled entry. It does not flatten positions |

## Carry the decision into the next turn

Keep the account/network, both symbol forms, observation times, intended size and protection,
returned IDs/cloids, actual fills, unresolved outcomes and the `get_changes` cursor in the
agent's existing working record. Keep private keys out of it. No server-side journal is required.

On the next turn, call `get_changes` with that cursor as `since`, for the same account and
symbol filter. Continue with each returned cursor while `more` is true. The event interval
ends five seconds behind the call, while current positions and protection are read during
the call; inspect truncation/warnings and use direct status reads for unresolved recent orders.
`get_changes.observed_at` marks completion of the current reads, separately from the history
cursor. Its scope distinguishes the current account context from history across all perp
dexes. A rapid repeat with an empty interval returns null `observed_at` and `scope`: it has
not checked the current positions again, so do not interpret the empty result as a flat wallet.
Refresh the market context when it matters to the decision, then verify current stop coverage
before adding exposure.

A trigger entry, a future protection check and any trailing-stop decision need another
agent turn. Agree how the host will run that follow-up, or leave the task for the next
user-invoked turn. A completed MCP call creates no timer or continuous monitoring.
