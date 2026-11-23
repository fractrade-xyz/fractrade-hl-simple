"""Run-time state shared by every server of one config: each assistant session starts a
server of its own, and they trade the same accounts.

In the folder ``state`` next to the config file, for the user only:

- ``servers/``: one file per running server, locked by it for as long as it runs. The system
  releases the lock when the process ends, also after a crash, so a server that starts can
  tell whether another one still runs.
- per exchange account (network and address):

  - ``<account>.lock``: held by a trade call for its whole run, by every server, so two
    calls for one account never run at once, from whichever chat;
  - ``<account>.halted``: the kill switch; the account is halted while this file exists;
  - ``<account>.orders``: when the orders that added risk were sent (the last hour);
  - ``<account>.cloids``: the client order ids used (the last 30 days).

A halt lasts while any server runs: a server that starts while no other one does clears the
halts, as a restart did when there was one server. Opening another chat does not.

Everything but the halt is read and written holding the account's lock. Plain files, as the
OS file lock serializes them anyway: on Linux and macOS ``flock``, on Windows
``msvcrt.locking`` (not tested on Windows).
"""

from __future__ import annotations

import contextlib
import errno
import math
import os
import secrets
import sys
import threading
import time
import weakref
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from .setup import private_folder

ORDER_WINDOW = 3600                # max_orders_per_hour counts the last hour
CLOID_WINDOW = 30 * 86400          # a cloid stays used for 30 days
_OPEN = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)

Key = tuple[str, str]              # Target.key: (address in lower case, base_url)


class SharedState:
    """The state of one config's servers, in `folder` (created by `start`)."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self._guard = threading.Lock()
        self._locks: dict[Key, _AccountLock] = {}
        self._server: Optional[int] = None  # this server's file in servers/, locked while it runs
        # The files this object holds open: closed when it is gone (a server holds them until
        # it ends; a test makes many), which releases their locks.
        self._open: list[int] = []
        self._mine: list[Path] = []  # this server's file in servers/: removed when it ends normally
        self._closer = weakref.finalize(self, _close_all, self._open, self._mine)

    def close(self) -> None:
        """Close its files, which releases its locks, and remove its file in servers/: as when
        the server ends (also run at exit; after a crash the next start removes the file)."""
        self._closer()

    # ── servers ─────────────────────────────────────────────────────

    def start(self) -> None:
        """Create the folders (for the user only, refused where others could change them),
        claim this server's file, and clear the halts if no other server runs. Claiming and
        looking happen holding ``servers.lock``, so two servers starting at once see each
        other, and none clears a halt another one set."""
        servers = self.folder / "servers"
        private_folder(servers, [])
        fd = os.open(self.folder / "servers.lock", _OPEN, 0o600)
        try:
            _lock(fd)
            mine = servers / f"{os.getpid()}-{secrets.token_hex(4)}.lock"
            self._server = os.open(mine, _OPEN | os.O_EXCL, 0o600)
            self._open.append(self._server)
            self._mine.append(mine)
            _lock(self._server)
            if not self._others_run(servers, mine):
                for halted in self.folder.glob("*.halted"):
                    halted.unlink(missing_ok=True)
            _unlock(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _others_run(servers: Path, mine: Path) -> bool:
        """Whether another server holds its file; the files of servers that ended are removed."""
        running = False
        for path in servers.glob("*.lock"):
            if path == mine:
                continue
            try:
                fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
            except FileNotFoundError:
                continue
            ended = _lock(fd, blocking=False)
            if ended:
                _unlock(fd)
            os.close(fd)  # before the unlink: Windows does not delete an open file
            if ended:
                path.unlink(missing_ok=True)  # its server ended: the system released its lock
            else:
                running = True
        return running

    # ── the account lock ────────────────────────────────────────────

    @contextlib.contextmanager
    def account_lock(self, key: Key) -> Iterator[None]:
        """Hold the account's lock: one thread of this server and no other server at a time.
        Re-entrant in a thread, so the methods below take it themselves."""
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = self._locks[key] = _AccountLock(self.folder / f"{_name(key)}.lock", self._open)
        with lock:
            yield

    # ── the halt ────────────────────────────────────────────────────

    def halt(self, key: Key) -> None:
        """Halt the account for every server, at once: without its lock, as a trade call may
        hold it. A file created in one step; halting twice keeps the first."""
        fd = os.open(self.folder / f"{_name(key)}.halted", os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                     0o600)
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} pid {os.getpid()}\n"
                         .encode())
        finally:
            os.close(fd)

    def is_halted(self, key: Key) -> bool:
        return os.path.lexists(self.folder / f"{_name(key)}.halted")

    # ── orders that add risk ────────────────────────────────────────

    def count_risk_order(self, key: Key, count: int = 1) -> float:
        """Count `count` orders that add risk (a batch: in one write), before they are sent (a
        crash after sending then cannot leave them uncounted); returns their time, for
        release_risk_order."""
        with self.account_lock(key):
            path = self.folder / f"{_name(key)}.orders"
            now = time.time()
            _write(path, [*(str(t) for t in _recent(_times(path), now, ORDER_WINDOW)), *[str(now)] * count])
            return now

    def release_risk_order(self, key: Key, at: float, count: int = 1) -> None:
        """Uncount the `count` orders counted at `at`: they provably were not sent."""
        with self.account_lock(key):
            path = self.folder / f"{_name(key)}.orders"
            times = _times(path)
            removed = 0
            while removed < count and at in times:
                times.remove(at)
                removed += 1
            if removed:
                _write(path, [str(t) for t in times])

    def risk_orders_last_hour(self, key: Key) -> int:
        """Only reads: the file is pruned when an order is counted."""
        with self.account_lock(key):
            return self.peek_risk_orders_last_hour(key)

    def peek_risk_orders_last_hour(self, key: Key) -> int:
        """Read the last atomic file replacement, without creating a lock or state folder.

        For a preview only: submission checks again while holding the account lock.
        """
        return len(_recent(_times(self.folder / f"{_name(key)}.orders"), time.time(), ORDER_WINDOW))

    # ── client order ids ────────────────────────────────────────────

    def reserve_cloid(self, key: Key, cloid: Optional[str]) -> Optional[str]:
        """`cloid`, or a new one if None, now used; None if `cloid` was used already."""
        reserved = self.reserve_cloids(key, [cloid])
        return None if reserved is None else reserved[0]

    def reserve_cloids(self, key: Key, cloids: list[Optional[str]]) -> Optional[list[str]]:
        """Reserve the cloids of a batch together, in one write: each given one, a new one for
        each None. None if any given one was used already, or one is given twice (in any case
        of its hex digits): then none is reserved."""
        with self.account_lock(key):
            used = self._cloids(key)
            given = [cloid.lower() for cloid in cloids if cloid is not None]
            if len(set(given)) != len(given) or any(cloid in used for cloid in given):
                return None
            reserved = []
            for cloid in cloids:
                if cloid is None:
                    cloid = "0x" + secrets.token_hex(16)
                    while cloid in used:  # pragma: no cover - 128 random bits
                        cloid = "0x" + secrets.token_hex(16)
                used[cloid.lower()] = time.time()
                reserved.append(cloid)
            self._save_cloids(key, used)
            return reserved

    def remember_cloids(self, key: Key, cloids: Iterable[str]) -> None:
        """Mark `cloids` as used, in one write."""
        with self.account_lock(key):
            used = self._cloids(key)
            new = {cloid.lower() for cloid in cloids} - used.keys()
            if new:
                now = time.time()
                used.update(dict.fromkeys(sorted(new), now))
                self._save_cloids(key, used)

    def release_cloid(self, key: Key, cloid: str) -> None:
        self.release_cloids(key, [cloid])

    def release_cloids(self, key: Key, cloids: Iterable[str]) -> None:
        """Free cloids whose orders provably were not sent, in one write."""
        with self.account_lock(key):
            used = self._cloids(key)
            freed = [used.pop(cloid.lower(), None) for cloid in cloids]
            if any(at is not None for at in freed):
                self._save_cloids(key, used)

    def _cloids(self, key: Key) -> dict[str, float]:
        """The cloids of the last 30 days, with when each was used. A line that does not read
        as one is dropped (only an edit by hand makes one)."""
        cutoff = time.time() - CLOID_WINDOW
        used: dict[str, float] = {}
        for line in _lines(self.folder / f"{_name(key)}.cloids"):
            when, _, cloid = line.partition(" ")
            try:
                at = float(when)
            except ValueError:
                continue
            if not math.isfinite(at):
                at = time.time()  # kept as used: a damaged time never frees a cloid
            if at >= cutoff and cloid:
                used[cloid] = at
        return used

    def _save_cloids(self, key: Key, used: dict[str, float]) -> None:
        _write(self.folder / f"{_name(key)}.cloids", [f"{at} {cloid}" for cloid, at in used.items()])


class _AccountLock:
    """A threading.RLock and the account's lock file: the file is locked by the first
    acquire of a thread and unlocked by its last release."""

    def __init__(self, path: Path, held: list[int]) -> None:
        self._path = path
        self._held = held  # the state's open files: this one's goes there too
        self._lock = threading.RLock()
        self._depth = 0
        self._fd: Optional[int] = None

    def __enter__(self) -> None:
        self._lock.acquire()
        try:
            if self._depth == 0:
                if self._fd is None:
                    self._fd = os.open(self._path, _OPEN, 0o600)
                    self._held.append(self._fd)
                _lock(self._fd)
            self._depth += 1
        except BaseException:
            self._lock.release()
            raise

    def __exit__(self, *exc: object) -> None:
        try:
            self._depth -= 1
            if self._depth == 0 and self._fd is not None:
                _unlock(self._fd)
        finally:
            self._lock.release()


def _close_all(fds: list[int], files: list[Path]) -> None:
    for fd in fds:  # first: Windows does not delete an open file
        try:
            os.close(fd)
        except OSError:
            pass
    fds.clear()
    for path in files:
        try:
            path.unlink(missing_ok=True)
        except OSError:  # the next start removes it
            pass
    files.clear()


def _name(key: Key) -> str:
    """The files' name for an exchange account: network host and address."""
    address, base_url = key
    return f"{urlsplit(base_url).netloc.replace(':', '_')}-{address}"


def _lines(path: Path) -> list[str]:
    """The lines of `path`; none if it does not exist. A byte that is no UTF-8 (only damage or
    an edit by hand makes one) spoils only its line, which each reader treats as damaged."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []


def _times(path: Path) -> list[float]:
    """The times in an orders file. A line that does not read as one counts as an order now
    (only an edit by hand makes one): an order rate is never lowered by a damaged file."""
    times = []
    for line in _lines(path):
        try:
            at = float(line)
        except ValueError:
            at = math.nan
        times.append(at if math.isfinite(at) else time.time())
    return times


def _recent(times: list[float], now: float, window: float) -> list[float]:
    return [t for t in times if now - t <= window]


def _write(path: Path, lines: list[str]) -> None:
    """Replace `path` with `lines`, in one step (another server never reads half a file):
    written to a temporary file, then renamed over it. Only holding the account's lock."""
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("".join(line + "\n" for line in lines))
    os.replace(temporary, path)


def _lock(fd: int, blocking: bool = True) -> bool:
    """Lock `fd`'s file for this open file, exclusively; False if not blocking and taken."""
    if sys.platform == "win32":
        import msvcrt
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return True
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EDEADLK):  # held by another: anything else is raised
                    raise
                if not blocking:
                    return False
                time.sleep(0.05)
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except BlockingIOError:
        return False
    return True


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_UN)
