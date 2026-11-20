# Set up the MCP server, step by step

This guide takes you from nothing to an AI assistant (Claude Code, Codex: the CLI or the
app, or Claude Desktop) that reads your Hyperliquid account and, if you want, trades it within limits you
set. You do not need to program.

**The easy way: let your assistant set it up.** In Claude Code or Codex, say:

> Set up the fractrade-hl-simple MCP server for me, following
> https://github.com/fractrade-xyz/fractrade-hl-simple/blob/main/docs/mcp-setup.md

It runs the commands and edits the config for you. You only do
the two things below that must stay yours: you tell it your account's address and your
limits, and you create its trading key in the Hyperliquid app with your own wallet and
paste it into a file. **Never paste a private key into the chat**: this guide never needs
one there.

The full reference, with every tool and setting, is [mcp.md](mcp.md).

**Contents**

1. [What you need](#1-what-you-need)
2. [Install](#2-install)
3. [Create your config file](#3-create-your-config-file)
4. [Connect your assistant](#4-connect-your-assistant)
5. [Try it](#5-try-it)
6. [Trading (optional)](#6-trading-optional)
7. [Staying safe](#7-staying-safe)
8. [When something does not work](#8-when-something-does-not-work)
9. [For assistants: how to run this setup](#9-for-assistants-how-to-run-this-setup)

## 1. What you need

- **Python 3.12 or newer.** Check with `python3 --version` (on Windows: `python --version`).
  If it is missing or older, install it from [python.org](https://www.python.org/downloads/).
- **An AI assistant that speaks MCP:** [Claude Code](https://docs.anthropic.com/en/docs/claude-code),
  Codex (the CLI or the app) or Claude Desktop. Claude Desktop cannot run the setup
  commands itself: do the setup with Claude Code or Codex once, or by hand in a terminal.
- **Your Hyperliquid account's address:** the `0x…` address of the wallet you use on
  Hyperliquid (shown in the Hyperliquid app). It is public, not a secret.

Doing it by hand, you type the commands below in a terminal: on macOS the app *Terminal*,
on Windows *PowerShell*, on Linux your usual one.

## 2. Install

Install it with [pipx](https://pipx.pypa.io/), which keeps it apart from your system's
Python (many Linux and macOS Pythons refuse a plain `pip install`) and puts the command on
your `PATH`:

1. **Get pipx**, if you do not have it, and run `pipx ensurepath` once:
   - macOS: `brew install pipx`
   - Debian or Ubuntu: `sudo apt update && sudo apt install pipx`
   - elsewhere: `python3 -m pip install --user pipx`, then `python3 -m pipx ensurepath`

   Then open a new terminal (or restart the assistant), so the `PATH` change applies.
2. **Install the server:**

   ```bash
   pipx install "fractrade-hl-simple[mcp]"
   ```

   Until a release with the MCP server is on PyPI, from GitHub instead:

   ```bash
   pipx install "fractrade-hl-simple[mcp] @ git+https://github.com/fractrade-xyz/fractrade-hl-simple.git"
   ```

3. **Check it:** `fractrade-hl-mcp --help`. The Codex app and Claude Desktop need the
   command's full path: `which fractrade-hl-mcp` prints it (with pipx usually
   `~/.local/bin/fractrade-hl-mcp`).

To update later: `pipx upgrade fractrade-hl-simple` (or the same `pipx install` command
with `--force` for the GitHub version).

## 3. Create your config file

```bash
fractrade-hl-mcp --init
```

This creates the config file and a folder for the key, readable by you only (on macOS
and Linux; on Windows they are in your user folder), and prints where they are:

| File | What goes in it |
|---|---|
| `~/.config/fractrade-hl/mcp.toml` | your account's address and your limits; nothing secret |
| `~/.config/fractrade-hl/keys/main` | only for trading (step 6): empty until you paste the trading key into it |

(`~` is your home folder: `/Users/you` on macOS, `/home/you` on Linux, `C:\Users\you` on
Windows.) It never overwrites a file that is already there.

In `mcp.toml`, replace `0xYOUR_ACCOUNT_ADDRESS` with your account's address (any text
editor: VS Code, Notepad, TextEdit, nano). Then check it:

```bash
fractrade-hl-mcp --check
```

It prints your account and ends with `trading tools: off …`. That is right for now: the
server only reads until you switch trading on in step 6.

To watch more wallets, add a block per wallet at the end of `mcp.toml`:

```toml
[accounts.whale]
address = "0x…"
```

## 4. Connect your assistant

The assistant starts the server itself; you never run it by hand.

**Claude Code:**

```bash
claude mcp add --scope user fractrade -- fractrade-hl-mcp
```

**Codex CLI:**

```bash
codex mcp add fractrade -- fractrade-hl-mcp
```

**Codex app:** it uses the same settings as Codex CLI, `~/.codex/config.toml`. Add (or let
Codex add) these lines to it, with the command's full path (step 2):

```toml
[mcp_servers.fractrade]
command = "/full/path/to/fractrade-hl-mcp"
```

**Claude Desktop:** in Settings → Developer → Edit Config, add (with the command's full
path, step 2; on Windows `where.exe fractrade-hl-mcp` prints it):

```json
{
  "mcpServers": {
    "fractrade": { "command": "/full/path/to/fractrade-hl-mcp" }
  }
}
```

Restart the assistant afterwards: the server starts with it and reads `mcp.toml` then, so
restart it after every change too. The Codex app starts a server for every new chat: a new
chat picks a change up; chats already open keep the server they started with.

## 5. Try it

Ask your assistant, for example:

- "Show my Hyperliquid positions."
- "What is my account value and how much margin am I using?"
- "What is the funding rate on BTC and ETH?"
- "List my fills of the last week."
- "What do I make or lose at my stops and take profits on my ICP position?"
- "Plan a BTC long with a stop at 60,000, risking 1 % of my account."

This only reads, and needs no key. You can let your assistant run these read tools without
asking each time.

## 6. Trading (optional)

With trading on, your assistant can place and cancel orders, close positions and set stop
losses, within limits you set. It trades with an **API wallet**: a key that Hyperliquid
lets trade for your account but **never withdraw or transfer** your funds. The server
refuses your account's own key, so nothing it signs can move money out of your account.

### 6.1 Create the trading key

In the [Hyperliquid app](https://app.hyperliquid.xyz), connected with your account's wallet:

1. Open **More → API**.
2. Enter a name (e.g. `mcp`) and click **Generate**: the app makes a new API wallet, an
   address and its private key.
3. Click **Authorize API Wallet**, choose how many days it stays valid (at most 180), and
   confirm with your wallet.
4. Copy the **private key** before you close the window.

Then paste it into the key file that `--init` made:

1. Open the key file in a text editor (Notepad, TextEdit, your Linux text editor): the
   path `--init` printed, normally `~/.config/fractrade-hl/keys/main`. Your assistant can
   open it for you. The folder `.config` starts with a dot, so file dialogs may hide it:
   press Cmd+Shift+. on macOS, Ctrl+H on Linux.
2. Paste the key, nothing else: 64 hex characters, with or without `0x`. Save it as plain
   text under the same name, without an extension such as `.txt` (TextEdit: Format → Make
   Plain Text first), and close the editor.

The key goes only into that file: never into the chat, never into `mcp.toml`. Use this API
wallet only for the MCP server, not for bots or other apps.

### 6.2 Set your limits

`mcp.toml` comes with these limits (`symbols` is one you can add). Change them to fit you:

```toml
max_leverage = 10           # the highest leverage the assistant may set
max_slippage = 0.01         # market orders and closes: at most 1 % from the mid, before rounding
max_margin_pct = 5          # one new order: at most 5 % of your account as margin
max_total_margin_pct = 30   # your positions plus the new order: at most 30 % as margin
max_daily_loss_pct = 10     # lost 10 % in 24 hours: no new trades until it recovers
max_orders_per_hour = 30    # at most 30 orders that add risk per hour
# max_order_usd = 1000      # optional: the value of one order, at most, in USD
# check_order_leverage = true   # optional: no trades on a market set above max_leverage
# isolated_only = true      # optional: isolated margin only
# max_risk_pct = 1          # optional: a trade is planned to lose at most 1 % at its stops,
#                           # estimated at entry (open_position only: other orders that add
#                           # risk are then refused)
# symbols = ["BTC", "ETH"]  # optional: new risk and leverage changes on these markets only
```

**What margin means here:** a trade's margin is its value divided by its leverage. With
$1000 in your account, 5 % is $50: a BTC trade of up to $500 at 10x, or $150 on a market
at 3x. The margin is not the most you can lose: with Hyperliquid's usual cross margin your
account backs every position, so a position can lose more than its margin. A stop loss
limits a loss; with `isolated_only` (isolated margin), a position can lose at most its
margin ([more](mcp.md#risk-guardrails)).

Each guardrail is optional: delete a line to switch it off, but keep one of
`max_margin_pct`, `max_total_margin_pct` and `max_order_usd`. Start small. The limits never
block cancelling an order or setting a stop loss, and a close is held only to
`max_slippage`. Open orders are not part of `max_total_margin_pct`, and a market stop loss
or take profit executes with Hyperliquid's own 10 % tolerance, not `max_slippage`
([what the limits do not cover](mcp.md#risk-guardrails)).

### 6.3 Check everything

```bash
fractrade-hl-mcp --check --enable-trading --online
```

It should end with `main: the key (0x…) is an API wallet of 0x…`. Anything else says what
is wrong ([section 8](#8-when-something-does-not-work)).

### 6.4 Switch trading on

Start the server with `--enable-trading`. For Claude Code, replace the server:

```bash
claude mcp remove fractrade
claude mcp add --scope user fractrade -- fractrade-hl-mcp --enable-trading
```

(Codex CLI: the same with `codex mcp remove` and `codex mcp add`; the Codex app: add
`args = ["--enable-trading"]` under `command` in `~/.codex/config.toml`; Claude Desktop: add
`"args": ["--enable-trading"]` next to `"command"`.) Restart the assistant.

Now you can ask, for example, "buy 0.001 BTC at market", "set a stop loss at 80000 for my
BTC position", "close my ETH position", or with the protection in one step: "plan a BTC long
risking 1 % with stops at 60,000 and 59,000, then open it".

**More than one account:** copy the `[accounts.main]` block in `mcp.toml`, give it another
name, its own address and `key_file`, the full path of a new key file (e.g.
`key_file = "~/.config/fractrade-hl/keys/bot"`; a relative path is read from the folder
of `mcp.toml`). Create an API wallet for that
account (step 6.1) and paste its key into the new file. Each account gets its own API
wallet. On macOS and Linux the file must be readable by you only: `--check` tells you if
it is not, and `chmod 600 ~/.config/fractrade-hl/keys/bot` fixes it.

## 7. Staying safe

- **Keep the approval prompt for trade tools.** Your assistant then asks you before each
  order.
- **Emergency stop:** ask "use the kill switch". It halts trading at once, in every chat,
  and cancels the orders that would open or add to a position (stop losses and take
  profits stay); closing positions and cancelling orders still work. Opening a new chat does not lift it:
  quit the assistant (the Codex app: the whole app, every chat) and start it again. If the
  answer is an error saying an account is halted in this chat only, quit the other chats
  too.
- **The API wallet expires** after the days you chose. Then the server refuses it
  (`SIGNER_NOT_AUTHORIZED`): create a new one (step 6.1) and replace the old key in
  `keys/main` with the new one. The file holds one key only. A server reads the key when it
  starts: start a new chat (or restart the assistant) to use the new key.
- **If a key ever leaks**, revoke the API wallet in the Hyperliquid app (More → API) at
  once. Whoever has the key can trade your account directly, without this server and
  without your limits, and lose you money that way; they cannot withdraw or transfer your
  funds.
- **The key file** must stay readable by you only. Full-disk encryption (FileVault,
  BitLocker, LUKS) protects it if your computer is lost
  ([details](mcp.md#the-key-file)).
- **Your assistant can read files as you can.** It is told never to read the key file
  (section 9), but that is an instruction, not something this server can enforce. Keep your
  assistant's approval settings on, and refuse anything that reads
  `~/.config/fractrade-hl/keys`. If it did read the file, treat the key as leaked.

## 8. When something does not work

| You see | What to do |
|---|---|
| The assistant shows no fractrade tools | Run `fractrade-hl-mcp --check` yourself; if that works, give the assistant the command's full path. |
| `address must be 0x followed by 40 hex characters` | Fill in your account's address in `mcp.toml`. |
| `key file … does not exist` | Run `fractrade-hl-mcp --init`, or create the file, and paste the key into it (step 6.1). |
| `key file … is empty` | Paste the API wallet's private key into it (step 6.1). |
| `does not hold a private key` | The file holds something else: only the key belongs in it, 64 hex characters, with or without `0x`. |
| `can be changed by other users` | `mcp.toml` is writable by others: `chmod 600 ~/.config/fractrade-hl/mcp.toml`. |
| `must be readable by its owner only` | Your editor saved it readable by others: `chmod 600 ~/.config/fractrade-hl/keys/main` (your assistant can run it; it does not read the file). |
| `is the account's own key` | That is your wallet's own key, not an API wallet's: create an API wallet (step 6.1) and paste its key instead. |
| `Hyperliquid does not know it` | The API wallet is not authorized yet, or it expired: authorize it, or create a new one (step 6.1). |
| `is an API wallet of another account` | The address in `mcp.toml` is not the account that authorized this API wallet. |
| No trade tools | Start the server with `--enable-trading` (step 6.4) and restart the assistant. |
| `GUARD_BLOCKED` | The order is over one of your limits; the message says which. If it says a limit could not read the account, try again when the answer says `retryable: true`. |
| `the market uses cross margin` | `isolated_only` is on: ask the assistant to set the market to isolated margin (`set_leverage` with `is_cross` false), or delete `isolated_only`. |
| `the market's leverage … is above max_leverage` | `check_order_leverage` is on: ask the assistant to lower the market's leverage (`set_leverage`), or raise `max_leverage`. |
| `KILL_SWITCH_ENGAGED` | The kill switch is on: quit the assistant (the Codex app: the whole app) and start it again to trade. |

Still stuck? Run `fractrade-hl-mcp --check --log-level DEBUG` and read the messages, or
see the [reference](mcp.md#troubleshooting).

## 9. For assistants: how to run this setup

If you are an AI assistant setting this up for a user, follow sections 2 to 6 with these
rules:

1. **Never read, print, copy or move a key file** (`~/.config/fractrade-hl/keys/*`), never
   ask the user for a private key, and stop a user who starts pasting one into the chat.
   The user pastes the key into the key file (section 6.1). You may open that file for
   them in their text editor (macOS: `open -e FILE`; Windows: `notepad FILE`; Linux: their
   desktop's text editor) and, when `--check` asks for it, run `chmod 600 FILE`: neither
   returns the file's contents to you. Never preview, screenshot, print or search it.
2. **Ask the user** for their account's address (`0x…`, public) and whether the limits
   and guardrails in `mcp.toml` suit them (section 6.2); explain margin with their account
   size. Write them into `mcp.toml`; never put a key there.
3. **The user creates and authorizes** the API wallet in the Hyperliquid app with their own
   wallet (section 6.1). You cannot do this step and must not try.
4. **Check** with `fractrade-hl-mcp --check --enable-trading --online` and report the
   result; it never shows keys.
5. **Switch trading on** only when the user asks for it (section 6.4), and tell them to
   restart the assistant.
6. **Never change limits or switch trading on** without the user asking for exactly that.
