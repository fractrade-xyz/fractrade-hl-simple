"""The state every server of one config shares (state.py): the account lock, the halt, the
order times and the cloids. Two SharedState objects on one folder behave as two servers
(flock locks belong to an open file); the tests with real processes check that a server
that ends, also by a crash, releases what it held."""

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from fractrade_hl_simple.mcp_server import state as state_module
from fractrade_hl_simple.mcp_server.setup import SetupError
from fractrade_hl_simple.mcp_server.state import SharedState

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="flock: Linux and macOS")

KEY = ("0x" + "1" * 40, "https://api.hyperliquid.xyz")
OTHER = ("0x" + "2" * 40, "https://api.hyperliquid.xyz")


def server(folder):
    state = SharedState(folder)
    state.start()
    return state


def lock_is_free(state, key, wait=0.3):
    """Whether another thread of `state` gets `key`'s lock within `wait` seconds."""
    got = threading.Event()

    def take():
        with state.account_lock(key):
            got.set()

    threading.Thread(target=take, daemon=True).start()
    return got.wait(wait)


CHILD = """
import sys, time
from pathlib import Path
from fractrade_hl_simple.mcp_server.state import SharedState
state = SharedState(Path(sys.argv[1]))
state.start()
key = ("0x" + "1" * 40, "https://api.hyperliquid.xyz")
if sys.argv[2] == "halt":
    state.halt(key)
    print("ready", flush=True)
    time.sleep(60)
else:
    with state.account_lock(key):
        print("ready", flush=True)
        time.sleep(60)
"""


@pytest.fixture
def child(tmp_path):
    """Start a server in a process of its own that halts or holds the lock, until killed."""
    started = []

    def start(action):
        process = subprocess.Popen([sys.executable, "-c", CHILD, str(tmp_path / "state"), action],
                                   stdout=subprocess.PIPE, text=True)
        started.append(process)
        assert process.stdout.readline().strip() == "ready"
        return process

    yield start
    for process in started:
        process.kill()
        process.wait(5)


class TestTheHalt:
    def test_it_holds_for_every_server_while_one_runs_and_clears_when_none_does(self, tmp_path):
        one = server(tmp_path / "state")
        one.halt(KEY)
        two = server(tmp_path / "state")  # a new chat while the first runs: still halted
        assert two.is_halted(KEY) and not two.is_halted(OTHER)
        one.close()
        three = server(tmp_path / "state")  # two still runs
        assert three.is_halted(KEY)
        two.close()
        three.close()
        assert not server(tmp_path / "state").is_halted(KEY)  # none ran: as after a restart

    def test_a_server_that_crashed_no_longer_counts_as_running(self, tmp_path, child):
        process = child("halt")
        survivor = server(tmp_path / "state")
        assert survivor.is_halted(KEY)
        survivor.close()
        process.send_signal(signal.SIGKILL)
        process.wait(5)
        fresh = server(tmp_path / "state")
        assert not fresh.is_halted(KEY)
        assert len(list((tmp_path / "state" / "servers").glob("*.lock"))) == 1  # the crashed one's file removed

    def test_a_server_that_ends_removes_its_file(self, tmp_path):
        servers = tmp_path / "state" / "servers"
        one = server(tmp_path / "state")
        assert len(list(servers.glob("*.lock"))) == 1
        one.close()
        assert list(servers.glob("*.lock")) == []

    def test_a_halting_server_that_crashed_leaves_the_halt_while_another_runs(self, tmp_path, child):
        process = child("halt")
        survivor = server(tmp_path / "state")
        process.send_signal(signal.SIGKILL)
        process.wait(5)
        assert survivor.is_halted(KEY)
        assert server(tmp_path / "state").is_halted(KEY)  # a new chat while the survivor runs

    def test_a_halt_does_not_wait_for_the_account_lock(self, tmp_path):
        one, two = server(tmp_path / "state"), server(tmp_path / "state")
        with one.account_lock(KEY):  # a trade call runs in one chat
            halter = threading.Thread(target=two.halt, args=(KEY,))
            halter.start()
            halter.join(1)
            assert not halter.is_alive() and one.is_halted(KEY)


class TestTheAccountLock:
    def test_one_server_at_a_time_and_one_thread_at_a_time(self, tmp_path):
        one, two = server(tmp_path / "state"), server(tmp_path / "state")
        with one.account_lock(KEY):
            assert not lock_is_free(two, KEY)   # the other server
            assert not lock_is_free(one, KEY)   # another thread of the same one
            assert lock_is_free(two, OTHER)     # another account
            with one.account_lock(KEY):         # the same thread again: re-entrant
                pass
            assert not lock_is_free(two, KEY)   # still held after the inner one ended
        assert lock_is_free(two, KEY)

    def test_another_process_holds_it_until_it_ends(self, tmp_path, child):
        process = child("lock")
        mine = server(tmp_path / "state")
        assert not lock_is_free(mine, KEY)
        process.send_signal(signal.SIGKILL)  # the system releases a dead process's lock
        process.wait(5)
        assert lock_is_free(mine, KEY, wait=5)


class TestOrdersAndCloids:
    def test_order_times_are_shared_and_kept_one_hour(self, tmp_path, monkeypatch):
        now = [1_800_000_000.0]
        monkeypatch.setattr(state_module, "time", SimpleNamespace(time=lambda: now[0], sleep=time.sleep))
        one, two = server(tmp_path / "state"), server(tmp_path / "state")
        one.count_risk_order(KEY)
        now[0] += 1800
        two.count_risk_order(KEY)
        assert (one.risk_orders_last_hour(KEY), two.risk_orders_last_hour(OTHER)) == (2, 0)
        now[0] += 1801
        assert two.risk_orders_last_hour(KEY) == 1

    def test_cloids_are_shared_and_kept_thirty_days(self, tmp_path, monkeypatch):
        now = [1_800_000_000.0]
        monkeypatch.setattr(state_module, "time", SimpleNamespace(time=lambda: now[0], sleep=time.sleep))
        one, two = server(tmp_path / "state"), server(tmp_path / "state")
        cloid = "0x" + "AB" * 16
        assert one.reserve_cloid(KEY, cloid) == cloid
        assert two.reserve_cloid(KEY, cloid.lower()) is None  # compared in lower case
        assert two.reserve_cloid(OTHER, cloid) == cloid       # another account
        two.release_cloid(KEY, cloid)                         # not sent: free again
        assert one.reserve_cloid(KEY, cloid) == cloid
        now[0] += 30 * 86400 + 1
        assert two.reserve_cloid(KEY, cloid) == cloid

    def test_an_order_released_as_not_sent_no_longer_counts(self, tmp_path):
        one = server(tmp_path / "state")
        first, second = one.count_risk_order(KEY), one.count_risk_order(KEY)
        one.release_risk_order(KEY, second)
        assert one.risk_orders_last_hour(KEY) == 1
        one.release_risk_order(KEY, first)
        assert one.risk_orders_last_hour(KEY) == 0

    def test_a_damaged_orders_line_counts_as_an_order_and_a_damaged_cloid_line_is_dropped(self, tmp_path):
        # Only an edit by hand damages them: the rate is never lowered by it.
        one = server(tmp_path / "state")
        one.count_risk_order(KEY)
        orders = next((tmp_path / "state").glob("*.orders"))
        orders.write_text(orders.read_text() + "not a time\nnan\n-inf\ninf\n")
        assert one.risk_orders_last_hour(KEY) == 5
        one.remember_cloid(KEY, "0x" + "c" * 32)
        cloids = next((tmp_path / "state").glob("*.cloids"))
        cloids.write_text(cloids.read_text() + "garbage\n")
        assert one.reserve_cloid(KEY, "0x" + "c" * 32) is None


class TestTheFolder:
    def test_it_and_its_files_are_the_users_only(self, tmp_path):
        state = server(tmp_path / "state")
        state.halt(KEY)
        state.count_risk_order(KEY)
        state.remember_cloid(KEY, "0x" + "c" * 32)
        folder = tmp_path / "state"
        assert {oct(p.stat().st_mode & 0o777) for p in (folder, folder / "servers")} == {"0o700"}
        assert {oct(p.stat().st_mode & 0o777) for p in folder.rglob("*") if p.is_file()} == {"0o600"}
        assert not list(folder.glob("*.tmp"))

    def test_a_folder_others_can_change_above_it_is_refused(self, tmp_path):
        shared = tmp_path / "shared"
        shared.mkdir()
        shared.chmod(0o777)
        with pytest.raises(SetupError, match="can be changed by other users"):
            server(shared / "state")

    def test_a_relative_folder_is_checked_from_the_working_folder(self, tmp_path, monkeypatch):
        shared = tmp_path / "shared"
        (shared / "mine").mkdir(parents=True)
        (shared / "mine").chmod(0o700)
        shared.chmod(0o777)
        monkeypatch.chdir(shared / "mine")
        with pytest.raises(SetupError, match=f"{shared} can be changed by other users"):
            server(Path("state"))

    def test_two_servers_making_the_folders_at_the_same_moment_both_start(self, tmp_path, monkeypatch):
        # The other server's mkdir wins: this one finds the folder there, and checks it.
        real_mkdir = os.mkdir

        def raced(path, mode=0o777):
            real_mkdir(path, 0o700)
            raise FileExistsError(path)

        monkeypatch.setattr(os, "mkdir", raced)
        state = server(tmp_path / "state")
        monkeypatch.setattr(os, "mkdir", real_mkdir)
        assert (tmp_path / "state" / "servers").is_dir() and not state.is_halted(KEY)

    def test_a_folder_another_process_made_meanwhile_is_checked_too(self, tmp_path, monkeypatch):
        # Someone else wins the mkdir of state with a folder every user can write to: refused,
        # although servers below it is this server's own.
        real_mkdir = os.mkdir

        def raced(path, mode=0o777):
            if Path(path).name == "state":
                real_mkdir(path)
                os.chmod(path, 0o777)
                raise FileExistsError(path)
            real_mkdir(path, mode)

        monkeypatch.setattr(os, "mkdir", raced)
        with pytest.raises(SetupError, match="state can be changed by other users"):
            server(tmp_path / "state")

    def test_the_server_keeps_it_next_to_its_config(self, tmp_path):
        from fractrade_hl_simple.mcp_server.__main__ import _state_folder
        from fractrade_hl_simple.mcp_server.config import parse_config
        config = parse_config(f'[accounts.main]\naddress = "{KEY[0]}"\n', str(tmp_path / "mcp.toml"))
        assert _state_folder(config) == tmp_path / "state"
