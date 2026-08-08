"""Test fixtures.

The tests drive the real command line in real subprocesses against a throwaway
socket and database. Nothing here ever touches a user's default paths: every
process gets SOCKPOST_SOCKET, SOCKPOST_DB, SOCKPOST_PID and SOCKPOST_LOG
pointing inside a temporary directory.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

# Import the package straight from the checkout so a plain `pytest` works
# without installing first.
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class Bus:
    """A daemon plus helpers to run client commands against it."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        # Unix socket paths are limited to about 100 bytes, and pytest's tmp
        # directories are deep, so the socket lives directly under /tmp.
        self.socket_path = Path("/tmp") / ("sockpost-test-%s.sock" % uuid.uuid4().hex[:8])
        self.env = dict(os.environ)
        self.env.update({
            "SOCKPOST_SOCKET": str(self.socket_path),
            "SOCKPOST_DB": str(tmp_path / "queue.db"),
            "SOCKPOST_PID": str(tmp_path / "daemon.pid"),
            "SOCKPOST_LOG": str(tmp_path / "daemon.log"),
            "SOCKPOST_AUTOSTART": "0",
            "SOCKPOST_DELIVERY_GAP": "0.05",
            "PYTHONPATH": str(SRC),
            "PYTHONUNBUFFERED": "1",
        })
        self.env.pop("SOCKPOST_ID", None)
        self.daemon = None
        self.listeners = []

    # -- process control ------------------------------------------------

    def start_daemon(self) -> None:
        self.daemon = subprocess.Popen(
            [sys.executable, "-m", "sockpost", "daemon"],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True)
        if not self.wait_for_socket():
            output = ""
            if self.daemon.poll() is not None and self.daemon.stdout:
                output = self.daemon.stdout.read()
            raise RuntimeError("daemon did not start: %s" % output)

    def stop_daemon(self) -> None:
        if self.daemon is None:
            return
        self.daemon.terminate()
        try:
            self.daemon.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            self.daemon.kill()
        self.daemon = None

    def wait_for_socket(self, timeout: float = 10.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(1.0)
                sock.connect(str(self.socket_path))
                sock.close()
                return True
            except OSError:
                time.sleep(0.05)
        return False

    def listen(self, channel: str, *extra) -> Path:
        """Start a listener; returns the file its output is written to."""
        out_path = self.tmp_path / ("listen-%s-%d.out" % (channel, len(self.listeners)))
        handle = open(out_path, "w")
        process = subprocess.Popen(
            [sys.executable, "-m", "sockpost", "listen", "--id", channel, *extra],
            env=self.env, stdout=handle, stderr=subprocess.STDOUT, text=True)
        self.listeners.append((process, handle))
        needle = ('"connected"' if "--json" in extra
                  else "connected id=%s" % channel)
        self.wait_for_line(out_path, needle)
        return out_path

    def kill_daemon(self) -> None:
        """SIGKILL the daemon: no shutdown, no socket cleanup, no flush."""
        if self.daemon is None:
            return
        self.daemon.kill()
        self.daemon.wait(timeout=5)
        self.daemon = None
        try:
            self.socket_path.unlink()
        except OSError:
            pass

    def wedged_listener(self, channel: str):
        """A peer that connects and then never reads again.

        Returned raw so the test can hold the connection open. Used to prove
        the daemon does not block on a consumer that has stopped consuming.
        """
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(self.socket_path))
        sock.sendall(('{"op": "connect", "id": "%s"}\n' % channel).encode())
        time.sleep(0.5)  # let the daemon register it
        return sock

    def kill_listener(self, index: int = -1) -> None:
        """SIGKILL a listener so it cannot acknowledge or clean up."""
        process, handle = self.listeners.pop(index)
        process.kill()
        process.wait(timeout=5)
        handle.close()

    def run(self, *args, check: bool = True):
        result = subprocess.run(
            [sys.executable, "-m", "sockpost", *args],
            env=self.env, capture_output=True, text=True, timeout=30)
        if check and result.returncode != 0:
            raise AssertionError(
                "command %s failed (%d):\n%s%s"
                % (args, result.returncode, result.stdout, result.stderr))
        return result

    # -- assertions ------------------------------------------------------

    def wait_for_line(self, path: Path, needle: str, timeout: float = 10.0) -> str:
        deadline = time.time() + timeout
        text = ""
        while time.time() < deadline:
            if path.exists():
                text = path.read_text()
                if needle in text:
                    return text
            time.sleep(0.05)
        raise AssertionError("timed out waiting for %r in %s:\n%s"
                             % (needle, path, text))

    def wait_for_condition(self, predicate, timeout: float = 10.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.1)
        raise AssertionError("condition was still false after %gs" % timeout)

    def close(self) -> None:
        for process, handle in self.listeners:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                process.kill()
            handle.close()
        self.listeners = []
        self.stop_daemon()
        try:
            self.socket_path.unlink()
        except OSError:
            pass


@pytest.fixture()
def bus(tmp_path):
    instance = Bus(tmp_path)
    try:
        yield instance
    finally:
        instance.close()
