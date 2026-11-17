"""Setting up the server's files: ``--init`` makes the config file and an empty key file,
into which the user pastes the API wallet's private key (made in the Hyperliquid app).

Made for an assistant to run: nothing here reads or prints a key. Every file is created for
the user only (folders 0700, files 0600, on Linux and macOS; Windows has no such permission
bits), never over an existing one, and never through a symbolic link.
"""

from __future__ import annotations

import os
import stat
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Optional

from .config import default_config_path

POSIX = sys.platform != "win32"

CONFIG_TEMPLATE = """\
# fractrade-hl-mcp: your accounts. Reference: docs/mcp.md; setup: docs/mcp-setup.md.
# Nothing secret goes in this file: the private keys are in the key files.
# Check it:  fractrade-hl-mcp --check --enable-trading --online

default_account = "main"

[accounts.main]
address = "0xYOUR_ACCOUNT_ADDRESS"              # the account you trade (your wallet), not the API wallet
key_file = {key_file}
# key_file holds the API wallet's private key and nothing else: create the API wallet in
# the Hyperliquid app (More, API: generate it, then authorize it with your wallet) and paste
# its private key into that file. Never into this one.
trading = true
max_leverage = 10                               # the leverage set_leverage may set, at most
max_slippage = 0.01                             # market orders and closes: at most 1 % from the mid
# Risk guardrails, in % of the account value. Each is optional: delete a line to switch it
# off, but keep one of max_margin_pct, max_total_margin_pct and max_order_usd.
max_margin_pct = 5                              # one new order: its margin at most 5 % of the account
max_total_margin_pct = 30                       # margin in use with the new order: at most 30 %
max_daily_loss_pct = 10                         # down 10 % over 24 hours: no new risk until it recovers
max_orders_per_hour = 30                        # orders that add risk, per hour
# max_order_usd = 1000                          # the value of one order, at most (USD)
# check_order_leverage = true                   # refuse orders on a market set above max_leverage
# max_risk_pct = 1                              # open_position: at most 1 % lost at the stop losses, as
#                                               # estimated at entry (buy and sell that add risk: refused)
# isolated_only = true                          # isolated margin only: a position loses at most its margin
# Closing, cancelling and stop losses are never limited.

# Another wallet: copy the block with its own name, address and key file, e.g.
# [accounts.bot]
# address = "0x..."
# key_file = {bot_key_file}
# trading = true
# max_leverage = 10
# max_margin_pct = 5
# and paste that account's API wallet key into its own key file.

# A wallet you only want to watch needs just its address:
# [accounts.watch]
# address = "0x..."
"""


class SetupError(Exception):
    """A setup step that cannot be done; the message says why and what to do."""


def config_path(path: Optional[Path], environ: Mapping[str, str]) -> Path:
    """The config file, as the server finds it (``--config``, ``FRACTRADE_MCP_CONFIG``, the
    default place), made absolute: key paths written into it must not depend on the folder
    a command runs in. Not normalized: ``link/..`` stays for the system to resolve, as it
    does when the server reads the file (os.path.abspath would drop both as text)."""
    if path is not None:
        chosen = path
    elif environ.get("FRACTRADE_MCP_CONFIG"):
        chosen = Path(environ["FRACTRADE_MCP_CONFIG"])
    else:
        chosen = default_config_path(environ)
    chosen = chosen.expanduser()
    return chosen if chosen.is_absolute() else Path.cwd() / chosen


def init(path: Optional[Path], environ: Mapping[str, str]) -> list[str]:
    """``--init``: the config file (from the template), the folder for the key files and an
    empty key file for the template's account, where none is yet. Returns what to tell the
    user."""
    target = config_path(path, environ)
    created: list[str] = []
    kept: list[str] = []
    keys_dir = target.parent / "keys"
    private_folder(target.parent, created)
    private_folder(keys_dir, created)
    key_file = keys_dir / "main"
    for path, text in ((target, CONFIG_TEMPLATE.format(key_file=toml_string(str(key_file)),
                                                       bot_key_file=toml_string(str(keys_dir / "bot")))),
                       (key_file, "")):
        if path == key_file and os.path.lexists(path):
            _check_key_file(path)  # the user is told to paste into it: it must be a file of its own
        if os.path.lexists(path):
            kept.append(str(path))
        else:
            _write_new(path, text)
            created.append(str(path))
    lines = [f"created: {item}" for item in created] + [f"left as it is: {item}" for item in kept]
    lines.append("next: put your account's address into the config (replacing 0xYOUR_ACCOUNT_ADDRESS); create "
                 f"an API wallet in the Hyperliquid app (More, API), authorize it, and paste its private key into "
                 f"{key_file}; then fractrade-hl-mcp --check --enable-trading --online")
    lines.extend(_windows_note())
    return lines


def toml_string(text: str) -> str:
    """`text` as a TOML basic string: backslash, quote and control characters escaped, every
    other character as it is (TOML takes any Unicode there); checked by reading it back."""
    escaped = []
    for char in text:
        if char in ('"', "\\"):
            escaped.append("\\" + char)
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            escaped.append(f"\\u{ord(char):04X}")
        else:
            escaped.append(char)
    encoded = '"' + "".join(escaped) + '"'
    try:
        if tomllib.loads(f"value = {encoded}")["value"] == text:
            return encoded
    except tomllib.TOMLDecodeError:
        pass
    raise SetupError(f"the path {text!r} cannot be written into the config file; choose another folder")


def _windows_note() -> list[str]:
    if POSIX:
        return []
    return ["on Windows the files' permissions are not set or checked: keep them in your user folder, which "
            "other users of the computer cannot open by default"]


# ── files ───────────────────────────────────────────────────────────


def private_folder(path: Path, created: list[str]) -> None:
    """`path` and every missing folder above it, created for the user only (0700). Checked
    first: no folder above it may be changeable by other users (it could be swapped), and
    after: `path` itself must be a real folder of the user that no one else can change. A
    symbolic link above it is followed, as the user's own setup may use one (~/.config).
    Nothing is created for a path with ``..`` after a folder that does not exist, which the
    system cannot resolve."""
    _check_ancestors(path)
    missing = []
    folder = path
    while not os.path.lexists(folder):  # a dangling symbolic link counts as there
        if folder.name == "..":
            raise SetupError(f"{path}: the folder before '..' in it does not exist; give the path without '..'")
        missing.append(folder)
        folder = folder.parent
    for folder in reversed(missing):
        try:
            os.mkdir(folder, 0o700)
        except FileExistsError:  # another server starting at the same moment made it: checked below
            continue
        if POSIX:
            os.chmod(folder, 0o700)  # mkdir's mode is cut by the umask
        created.append(f"{folder}{os.sep}")
    if missing:  # the folders made meanwhile, by this or another process, are checked too
        _check_ancestors(path)
    _check_folder(path)


MAX_LINKS = 40  # symbolic links followed to reach a path, as Linux allows


def _check_ancestors(path: Path) -> None:
    """Refuse a path that someone else could redirect: through a folder of another user
    (root's are fine) or one that a group or every user can write to without the sticky bit
    (which keeps others from moving what is in it), or through a symbolic link of another
    user. Every folder the path is looked up in is checked, following each link as the
    system does, hop by hop: once no other user can change any of them, the path stays
    what it is."""
    if not POSIX:
        return
    folders, links = _lookups(path)
    for folder in folders:
        info = os.stat(folder)
        if info.st_uid not in (0, os.getuid()):
            raise SetupError(f"{folder} belongs to another user: choose a folder of your own")
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not info.st_mode & stat.S_ISVTX:
            raise SetupError(f"{folder} can be changed by other users: choose a folder of your own")
    for link in links:
        if os.lstat(link).st_uid not in (0, os.getuid()):
            raise SetupError(f"{link} is a symbolic link of another user: choose a folder of your own")


def _lookups(path: Path) -> tuple[list[Path], list[Path]]:
    """The folders `path`'s parts are looked up in and the symbolic links followed, as the
    system resolves it: a link's target replaces it, one hop at a time. Stops at the first
    part that does not exist (the folders to create)."""
    folders: list[Path] = []
    links: list[Path] = []
    if not path.is_absolute():  # from the working folder, not from the root
        path = Path.cwd() / path
    current = Path(path.anchor or os.sep)
    pending = list(Path(path).parts[1:])
    while pending:
        part = pending.pop(0)
        if part in ("", "."):
            continue
        if part == "..":  # looked up in `current`, a folder of its own (no link): it counts too
            folders.append(current)
            current = current.parent
            continue
        folders.append(current)
        entry = current / part
        if not os.path.lexists(entry):
            break
        if os.path.islink(entry):
            links.append(entry)
            if len(links) > MAX_LINKS:
                raise SetupError(f"{path}: too many symbolic links")
            target = Path(os.readlink(entry))
            if target.is_absolute():
                current = Path(target.anchor)
                pending = list(target.parts[1:]) + pending
            else:
                pending = list(target.parts) + pending
            continue
        if not os.path.isdir(entry):
            break
        current = entry
    return folders, links


def _check_key_file(path: Path) -> None:
    """An existing key file, without reading it: a regular file, not a link to another one
    (pasting into a link writes the key wherever it leads), and on Linux and macOS the
    user's own, readable by no one else, before the user is told to paste into it."""
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        raise SetupError(f"{path} is a symbolic link: the key goes into a regular file of your own; remove the "
                         "link and run fractrade-hl-mcp --init again")
    if not stat.S_ISREG(info.st_mode):
        raise SetupError(f"{path} is not a regular file: remove it and run fractrade-hl-mcp --init again")
    if POSIX and info.st_uid != os.getuid():
        raise SetupError(f"{path} belongs to another user: remove it and run fractrade-hl-mcp --init again")
    if POSIX and info.st_mode & 0o077:
        raise SetupError(f"{path} can be read by other users: run chmod 600 {path} before you paste the key")


def _check_folder(path: Path) -> None:
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        raise SetupError(f"{path} is a symbolic link: the key files need a real folder of your own")
    if not stat.S_ISDIR(info.st_mode):
        raise SetupError(f"{path} is not a folder")
    if POSIX and (info.st_uid != os.getuid() or info.st_mode & 0o022):
        raise SetupError(f"{path} can be changed by other users: use a folder only you can write to")


def _write_new(path: Path, text: str) -> None:
    """Create `path` with `text`, for the user only, in one step that fails if anything is
    there (never over a file, never through a link). If writing fails, the partly written
    file is removed."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
    except BaseException:
        os.unlink(path)  # created by this call: nothing else is lost
        raise
