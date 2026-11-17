"""Private keys of configured accounts.

A key comes only from an environment variable (``key_env``) or from a file that only its
owner can read (``key_file``), never from the config file. Error messages name the variable
or the file, never the key. Paths and variable names are configuration, not secrets: they are
shown as configured (a key used as a file or directory name is not protected against).
"""

from __future__ import annotations

import os
import re
import stat
import sys
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import Optional

# 64 hex characters: the length of a private key. Addresses have 40. A longer run is
# replaced as a whole: replacing 64-character blocks would leave part of a key that follows
# other hex characters.
_KEY_LIKE = re.compile(r"[0-9a-fA-F]{64,}")
_KEY = re.compile(r"(?:0x)?[0-9a-fA-F]{64}")


def contains_private_key(text: str) -> bool:
    """True if `text` contains something that looks like a private key."""
    return _KEY_LIKE.search(text) is not None


def redact(text: str) -> str:
    """`text` with everything that looks like a private key replaced by <redacted>."""
    return _KEY_LIKE.sub("<redacted>", text)


def read_key_file(path: Path) -> str:
    """The text of a key file, after checking the opened file itself: a regular file owned
    by the current user with no permissions for group or others. Checking the same open
    file that is read means the file cannot be swapped in between. On Windows, which has no
    such permission bits, the check is skipped with a warning."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))  # never block on a FIFO
    except FileNotFoundError:
        raise ValueError(f"key file {path} does not exist: create it (fractrade-hl-mcp --init makes the first one) "
                         "and paste the API wallet's private key into it") from None
    except OSError as e:
        raise ValueError(f"key file {path} cannot be opened: {e.strerror}") from None
    with os.fdopen(fd, "rb") as f:
        _check_key_file(path, os.fstat(f.fileno()))
        data = f.read()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:  # not passed on: it holds the raw bytes
        text = None
    if text is None:
        raise ValueError(f"key file {path} is not valid UTF-8 text")
    return text


def _check_key_file(path: Path, info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"key file {path} is not a regular file")
    if sys.platform == "win32":
        warnings.warn(redact(f"cannot check the permissions of key file {path} on Windows"), stacklevel=2)
        return
    if info.st_uid != os.getuid():
        raise ValueError(f"key file {path} is not owned by the current user")
    if info.st_mode & 0o077:
        raise ValueError(f"key file {path} must be readable by its owner only (chmod 600)")


def read_key(key_env: Optional[str], key_file: Optional[Path], environ: Mapping[str, str]) -> str:
    """The private key from `key_env` or `key_file` (exactly one is set), as ``0x`` + hex."""
    if key_env is not None:
        source = f"environment variable {key_env}"
        raw = environ.get(key_env, "")
        if not raw:
            raise ValueError(f"{source} is not set")
    elif key_file is not None:
        source = f"key file {key_file}"
        raw = read_key_file(key_file)
    else:
        raise ValueError("no key_env or key_file configured")
    key = raw.strip()
    if not key:
        raise ValueError(f"{source} is empty: paste the API wallet's private key into it (made in the Hyperliquid "
                         "app: More, API)")
    if not _KEY.fullmatch(key):
        raise ValueError(f"{source} does not hold a private key (64 hex characters, optionally with 0x)")
    return key if key.startswith("0x") else "0x" + key
