"""The fractrade-hl-mcp command: --check, startup errors, the missing extra,
and logs on stderr with keys redacted."""

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from fractrade_hl_simple.mcp_server.__main__ import RedactingFormatter, main


@pytest.fixture(autouse=True)
def restore_logging():
    """main() sets up the root logger for the process; put it back after each test."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)

KEY = "ab" * 32  # 64 hex characters, the shape of a private key
CONFIG = f'''
[accounts.main]
address = "0x{"1" * 40}"
key_env = "HL_KEY_MAIN"
trading = true
max_order_usd = 100
max_leverage = 5
'''


def write_config(path, text):
    """A config file as --init makes it: only its owner can read and write it."""
    path.write_text(text)
    path.chmod(0o600)


def test_check_prints_the_accounts_and_exits(tmp_path, capsys):
    config = tmp_path / "mcp.toml"
    write_config(config, CONFIG)
    assert main(["--config", str(config), "--check"]) == 0
    out, err = capsys.readouterr()
    assert out == ""  # stdout is the protocol channel
    assert "main: 0x" + "1" * 40 in err and "key from $HL_KEY_MAIN" in err and "max_order_usd 100" in err


class RoleClient:
    """Stands in for HyperliquidClient in --check --online: answers userRole."""

    answer = {"role": "missing"}

    def __init__(self, **kwargs):
        pass

    def get_user_role(self, address):
        return self.answer


@pytest.mark.parametrize("answer, status, says", [
    ({"role": "missing"}, 1, "expired or was removed"),
    ({"role": "agent", "data": {"user": "0x" + "1" * 40}}, 0, "is an API wallet of 0x" + "1" * 40),
])
def test_check_online_checks_the_signer_of_each_trading_account(tmp_path, capsys, monkeypatch,
                                                                 answer, status, says):
    config = tmp_path / "mcp.toml"
    write_config(config, CONFIG)
    monkeypatch.setenv("HL_KEY_MAIN", KEY)
    monkeypatch.setattr(RoleClient, "answer", answer)
    monkeypatch.setattr("fractrade_hl_simple.hyperliquid.HyperliquidClient", RoleClient)
    assert main(["--config", str(config), "--check", "--online", "--enable-trading"]) == status
    err = capsys.readouterr().err
    assert says in err and KEY not in err


def test_online_works_only_with_check(tmp_path, capsys):
    config = tmp_path / "mcp.toml"
    write_config(config, CONFIG)
    assert main(["--config", str(config), "--online"]) == 1
    assert "--online works only with --check" in capsys.readouterr().err


def test_an_unknown_log_level_in_the_environment_fails_with_a_message(tmp_path, capsys, monkeypatch):
    # argparse checks choices only on the command line: it crashed with a raw ValueError.
    config = tmp_path / "mcp.toml"
    write_config(config, CONFIG)
    monkeypatch.setenv("FRACTRADE_MCP_LOG_LEVEL", "trace")
    with pytest.raises(SystemExit) as exited:
        main(["--config", str(config), "--check"])
    assert exited.value.code == 2 and "FRACTRADE_MCP_LOG_LEVEL must be one of DEBUG" in capsys.readouterr().err


def test_without_the_mcp_extra_the_server_says_how_to_install_it(tmp_path):
    config = tmp_path / "mcp.toml"
    write_config(config, CONFIG)
    code = ("import sys; sys.modules['mcp'] = None\n"  # as if the extra were not installed
            "from fractrade_hl_simple.mcp_server.__main__ import main\n"
            f"sys.exit(main(['--config', {str(config)!r}]))")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert done.returncode == 1 and done.stdout == ""
    assert "pip install 'fractrade-hl-simple[mcp]'" in done.stderr


def test_a_real_import_error_is_not_taken_for_the_missing_extra(tmp_path):
    pytest.importorskip("mcp")  # with the extra missing, its install hint correctly comes first
    pytest.importorskip("jsonschema")
    config = tmp_path / "mcp.toml"
    write_config(config, CONFIG)
    code = ("import sys; sys.modules['fractrade_hl_simple.mcp_server.tools.read'] = None\n"  # a broken module
            "from fractrade_hl_simple.mcp_server.__main__ import main\n"
            f"sys.exit(main(['--config', {str(config)!r}]))")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert done.returncode != 0 and "ModuleNotFoundError" in done.stderr and "pip install" not in done.stderr


def test_log_records_are_redacted_traceback_included():
    formatter = RedactingFormatter("%(message)s")
    try:
        raise RuntimeError(f"failed with {KEY}")
    except RuntimeError:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "key %s", (KEY,), sys.exc_info())
    text = formatter.format(record)
    assert KEY not in text and "RuntimeError" in text and text.count("<redacted>") == 2


def test_check_says_whether_trading_would_be_on(tmp_path, capsys, monkeypatch):
    config = tmp_path / "mcp.toml"
    write_config(config, CONFIG)
    monkeypatch.delenv("FRACTRADE_MCP_TRADING", raising=False)
    assert main(["--config", str(config), "--check"]) == 0
    assert "trading tools: off (start with --enable-trading or FRACTRADE_MCP_TRADING=1 to trade main)" in \
        capsys.readouterr().err
    monkeypatch.setenv("HL_KEY_MAIN", KEY)
    assert main(["--config", str(config), "--check", "--enable-trading"]) == 0
    err = capsys.readouterr().err
    assert "trading tools: on for main" in err and KEY not in err


def test_with_the_switch_a_missing_key_fails_the_check_and_the_server(tmp_path, capsys, monkeypatch):
    config = tmp_path / "mcp.toml"
    write_config(config, CONFIG)
    monkeypatch.delenv("HL_KEY_MAIN", raising=False)
    monkeypatch.setenv("FRACTRADE_MCP_TRADING", "1")
    assert main(["--config", str(config), "--check"]) == 1
    assert "HL_KEY_MAIN" in capsys.readouterr().err
    assert main(["--config", str(config)]) == 1  # before anything is served
    assert "HL_KEY_MAIN" in capsys.readouterr().err


def test_without_the_switch_the_signer_check_reads_no_key(tmp_path, capsys, monkeypatch):
    config = tmp_path / "mcp.toml"
    write_config(config, CONFIG)
    monkeypatch.delenv("FRACTRADE_MCP_TRADING", raising=False)
    monkeypatch.setenv("HL_KEY_MAIN", "not a key")  # reading it would fail
    assert main(["--config", str(config), "--check", "--online"]) == 0
    assert "signer check: skipped, trading is off" in capsys.readouterr().err


class TestInit:
    def run(self, path, capsys):
        assert main(["--init", "--config", str(path)]) == 0
        return capsys.readouterr().err

    def test_it_makes_the_config_and_an_empty_key_file_for_the_user_only(self, tmp_path, capsys):
        path = tmp_path / "fractrade-hl" / "mcp.toml"
        err = self.run(path, capsys)
        keys_dir, key_file = path.parent / "keys", path.parent / "keys" / "main"
        assert f"created: {path}" in err and f"created: {keys_dir}" in err and f"created: {key_file}" in err
        assert list(keys_dir.iterdir()) == [key_file] and key_file.read_text() == ""
        assert f"paste its private key into {key_file}" in err
        if sys.platform != "win32":
            assert [oct(p.stat().st_mode & 0o777) for p in (path.parent, keys_dir, path, key_file)] == [
                "0o700", "0o700", "0o600", "0o600"]

    def test_it_never_overwrites_anything(self, tmp_path, capsys):
        path = tmp_path / "mcp.toml"
        self.run(path, capsys)
        path.write_text(path.read_text().replace("0xYOUR_ACCOUNT_ADDRESS", "0x" + "1" * 40))
        (tmp_path / "keys" / "main").write_text(KEY + "\n")  # pasted
        before = path.read_text()
        err = self.run(path, capsys)
        assert "created" not in err and path.read_text() == before
        assert (tmp_path / "keys" / "main").read_text() == KEY + "\n"

    def test_unfilled_it_says_what_is_missing(self, tmp_path, capsys):
        path = tmp_path / "mcp.toml"
        self.run(path, capsys)
        assert main(["--config", str(path), "--check"]) == 1
        assert "address must be 0x followed by 40 hex" in capsys.readouterr().err
        path.write_text(path.read_text().replace("0xYOUR_ACCOUNT_ADDRESS", "0x" + "1" * 40))
        assert main(["--config", str(path), "--check", "--enable-trading"]) == 1
        assert "is empty: paste the API wallet's private key into it" in capsys.readouterr().err
        (tmp_path / "keys" / "main").unlink()
        assert main(["--config", str(path), "--check", "--enable-trading"]) == 1
        assert "does not exist: create it" in capsys.readouterr().err


ADDRESS = "0x" + "1" * 40


def filled_config(tmp_path, capsys):
    """An --init config with the account's address filled in."""
    path = tmp_path / "mcp.toml"
    assert main(["--init", "--config", str(path)]) == 0
    path.write_text(path.read_text().replace("0xYOUR_ACCOUNT_ADDRESS", ADDRESS))
    capsys.readouterr()
    return path


class TestInitPaths:
    def test_a_relative_config_path_gets_an_absolute_key_path(self, tmp_path, capsys, monkeypatch):
        # Loaded later from another folder, the key file must still be the one --init made.
        monkeypatch.chdir(tmp_path)
        assert main(["--init", "--config", "setup/mcp.toml"]) == 0
        monkeypatch.chdir("/")
        config = (tmp_path / "setup" / "mcp.toml").read_text().replace("0xYOUR_ACCOUNT_ADDRESS", ADDRESS)
        from fractrade_hl_simple.mcp_server.config import parse_config
        assert parse_config(config, "x").accounts["main"].key_file == tmp_path / "setup" / "keys" / "main"

    def test_a_path_with_an_apostrophe_is_written_as_valid_toml(self, tmp_path, capsys):
        path = tmp_path / "O'Brien" / "mcp.toml"
        assert main(["--init", "--config", str(path)]) == 0
        from fractrade_hl_simple.mcp_server.config import parse_config
        config = parse_config(path.read_text().replace("0xYOUR_ACCOUNT_ADDRESS", ADDRESS), "x")
        assert config.accounts["main"].key_file == path.parent / "keys" / "main"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions and symlinks")
    def test_every_folder_it_makes_is_the_users_only(self, tmp_path, capsys):
        path = tmp_path / "a" / "b" / "mcp.toml"
        assert main(["--init", "--config", str(path)]) == 0
        assert [oct(p.stat().st_mode & 0o777) for p in (tmp_path / "a", tmp_path / "a" / "b")] == ["0o700", "0o700"]

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
    def test_a_keys_folder_that_is_a_symbolic_link_is_refused(self, tmp_path, capsys):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        (tmp_path / "keys").symlink_to(elsewhere)
        assert main(["--init", "--config", str(tmp_path / "mcp.toml")]) == 1
        assert "symbolic link" in capsys.readouterr().err and list(elsewhere.iterdir()) == []


class TestPastedKey:
    """The key file --init made, with the API wallet's key pasted into it by an editor."""

    def test_a_pasted_key_is_read_and_never_shown(self, tmp_path, capsys):
        path = filled_config(tmp_path, capsys)
        (tmp_path / "keys" / "main").write_text(KEY + "\n")  # an editor keeps the file and its mode
        assert main(["--config", str(path), "--check", "--enable-trading"]) == 0
        assert KEY[2:] not in capsys.readouterr().err

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_a_key_file_an_editor_saved_readable_by_others_is_refused_with_the_fix(self, tmp_path, capsys):
        path = filled_config(tmp_path, capsys)
        key_file = tmp_path / "keys" / "main"
        key_file.unlink()
        key_file.write_text(KEY + "\n")  # saved as a new file: the umask's mode, often 0644
        key_file.chmod(0o644)
        assert main(["--config", str(path), "--check", "--enable-trading"]) == 1
        err = capsys.readouterr().err
        assert "must be readable by its owner only (chmod 600)" in err and KEY[2:] not in err


class TestInitFailures:
    def test_a_file_whose_write_fails_halfway_is_not_left_behind(self, tmp_path, capsys, monkeypatch):
        real_fdopen = os.fdopen

        class DiskFull:
            def __init__(self, f):
                self.f = f
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                self.f.close()
            def write(self, text):
                self.f.write(text[:10])
                self.f.flush()  # part of it is on disk
                raise OSError(28, "No space left on device")

        monkeypatch.setattr("fractrade_hl_simple.mcp_server.setup.os.fdopen",
                            lambda fd, *args, **kwargs: DiskFull(real_fdopen(fd, *args, **kwargs)))
        assert main(["--init", "--config", str(tmp_path / "mcp.toml")]) == 1
        assert not (tmp_path / "mcp.toml").exists()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_a_folder_every_user_can_change_above_the_keys_is_refused(self, tmp_path, capsys):
        shared = tmp_path / "shared"
        shared.mkdir()
        shared.chmod(0o777)  # writable by everyone, without the sticky bit
        assert main(["--init", "--config", str(shared / "mine" / "mcp.toml")]) == 1
        assert "can be changed by other users" in capsys.readouterr().err
        assert not (shared / "mine").exists()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_a_folder_a_group_can_change_above_the_keys_is_refused(self, tmp_path, capsys):
        shared = tmp_path / "team"
        shared.mkdir()
        shared.chmod(0o775)
        assert main(["--init", "--config", str(shared / "mine" / "mcp.toml")]) == 1
        assert "can be changed by other users" in capsys.readouterr().err

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions and symlinks")
    def test_a_folder_others_can_change_behind_a_symbolic_link_is_refused(self, tmp_path, capsys):
        # ~/.config -> /shared/config: /shared is checked too, not only the link's own folder.
        shared = tmp_path / "shared"
        (shared / "config").mkdir(parents=True)
        (shared / "config").chmod(0o700)
        shared.chmod(0o777)
        home = tmp_path / "home"
        home.mkdir(mode=0o700)
        (home / ".config").symlink_to(shared / "config")
        assert main(["--init", "--config", str(home / ".config" / "fractrade-hl" / "mcp.toml")]) == 1
        assert f"{shared} can be changed by other users" in capsys.readouterr().err
        assert list((shared / "config").iterdir()) == []

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions and symlinks")
    @pytest.mark.parametrize("relative", [False, True])
    def test_a_folder_others_can_change_in_the_middle_of_a_chain_of_links_is_refused(self, tmp_path, capsys,
                                                                                    relative):
        # ~/.config -> /shared/bridge -> /safe/config: others can replace bridge, which lies
        # in /shared; the final folders alone (/safe/config and above) look fine.
        safe, shared, home = tmp_path / "safe", tmp_path / "shared", tmp_path / "home"
        (safe / "config").mkdir(parents=True, mode=0o700)
        shared.mkdir()
        (shared / "bridge").symlink_to("../safe/config" if relative else safe / "config")
        shared.chmod(0o777)
        home.mkdir(mode=0o700)
        (home / ".config").symlink_to(shared / "bridge")
        assert main(["--init", "--config", str(home / ".config" / "fractrade-hl" / "mcp.toml")]) == 1
        assert f"{shared} can be changed by other users" in capsys.readouterr().err
        assert list((safe / "config").iterdir()) == []

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
    def test_another_users_link_in_a_sticky_folder_is_refused(self, tmp_path, capsys, monkeypatch):
        # A sticky folder (like /tmp) keeps others from moving my entries, not their own link.
        sticky = tmp_path / "sticky"
        sticky.mkdir()
        sticky.chmod(0o1777)
        (tmp_path / "real").mkdir(mode=0o700)
        link = sticky / "link"
        link.symlink_to(tmp_path / "real")
        _owned_by_another_user(monkeypatch, "lstat", link)
        assert main(["--init", "--config", str(link / "mcp.toml")]) == 1
        assert f"{link} is a symbolic link of another user" in capsys.readouterr().err

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
    def test_a_folder_of_another_user_that_a_link_steps_out_of_with_dotdot_is_refused(self, tmp_path, capsys,
                                                                                    monkeypatch):
        # link -> base/foreign/../safe: ".." is looked up in foreign, which its owner can replace.
        base = tmp_path / "base"
        (base / "foreign").mkdir(parents=True)
        (base / "safe").mkdir()
        for folder in (base, base / "foreign", base / "safe"):
            folder.chmod(0o755)  # mkdir's mode is cut by the umask: set it
        link = tmp_path / "link"
        link.symlink_to(base / "foreign" / ".." / "safe")
        _owned_by_another_user(monkeypatch, "stat", base / "foreign")
        assert main(["--init", "--config", str(link / "mcp.toml")]) == 1
        assert f"{base / 'foreign'} belongs to another user" in capsys.readouterr().err

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
    def test_dotdot_after_a_link_is_resolved_as_the_system_does(self, tmp_path, capsys):
        # home/link -> safe/sub: home/link/../mcp.toml is safe/mcp.toml for the system (and
        # for the server reading it later), not home/mcp.toml.
        (tmp_path / "safe" / "sub").mkdir(parents=True)
        (tmp_path / "home").mkdir()
        for folder in (tmp_path / "safe", tmp_path / "safe" / "sub", tmp_path / "home"):
            folder.chmod(0o755)
        (tmp_path / "home" / "link").symlink_to(tmp_path / "safe" / "sub")
        assert main(["--init", "--config", str(tmp_path / "home" / "link" / ".." / "mcp.toml")]) == 0
        assert (tmp_path / "safe" / "mcp.toml").exists() and not (tmp_path / "home" / "mcp.toml").exists()

    def test_dotdot_after_a_folder_that_does_not_exist_creates_nothing(self, tmp_path, capsys):
        # The system cannot resolve new/.. while new does not exist: refused before any mkdir.
        assert main(["--init", "--config", str(tmp_path / "new" / ".." / "config" / "mcp.toml")]) == 1
        assert "the folder before '..' in it does not exist" in capsys.readouterr().err
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_an_existing_key_file_of_another_user_is_refused(self, tmp_path, capsys, monkeypatch):
        (tmp_path / "keys").mkdir(mode=0o700)
        key_file = tmp_path / "keys" / "main"
        key_file.write_text("")
        key_file.chmod(0o600)
        _owned_by_another_user(monkeypatch, "lstat", key_file)
        assert main(["--init", "--config", str(tmp_path / "mcp.toml")]) == 1
        assert f"{key_file} belongs to another user" in capsys.readouterr().err

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
    def test_a_loop_of_symbolic_links_is_refused(self, tmp_path, capsys):
        (tmp_path / "a").symlink_to(tmp_path / "b")
        (tmp_path / "b").symlink_to(tmp_path / "a")
        assert main(["--init", "--config", str(tmp_path / "a" / "mcp.toml")]) == 1
        assert "too many symbolic links" in capsys.readouterr().err

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_an_existing_key_file_others_can_read_is_refused_before_the_paste(self, tmp_path, capsys):
        (tmp_path / "keys").mkdir(mode=0o700)
        (tmp_path / "keys" / "main").write_text("")
        (tmp_path / "keys" / "main").chmod(0o644)
        assert main(["--init", "--config", str(tmp_path / "mcp.toml")]) == 1
        err = capsys.readouterr().err
        assert "can be read by other users: run chmod 600" in err and "paste its private key" not in err

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
    @pytest.mark.parametrize("dangling", [False, True])
    def test_an_existing_key_file_that_is_a_symbolic_link_is_refused(self, tmp_path, capsys, dangling):
        # The user is told to paste into it: an editor would write the key wherever it leads.
        (tmp_path / "keys").mkdir(mode=0o700)
        other = tmp_path / "other"
        if not dangling:
            other.write_text("someone else's file")
        (tmp_path / "keys" / "main").symlink_to(other)
        assert main(["--init", "--config", str(tmp_path / "mcp.toml")]) == 1
        assert "is a symbolic link: the key goes into a regular file of your own" in capsys.readouterr().err
        assert not dangling or not other.exists()

def _owned_by_another_user(monkeypatch, call, path):
    """Make os.stat or os.lstat report `path` as another user's: only root can make one."""
    real = getattr(os, call)

    def patched(target, *args, **kwargs):
        info = real(target, *args, **kwargs)
        if Path(target) == path:
            fields = list(info)
            fields[4] = os.getuid() + 1  # st_uid
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(os, call, patched)
