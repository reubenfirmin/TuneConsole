"""Exercise real lock contention using tiny pytest sessions, without app imports."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


_PLUGIN = Path(__file__).resolve().parents[1] / "conftest.py"
_BOOTSTRAP = """
import importlib.util
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(sys.argv[1]).parent))
spec = importlib.util.spec_from_file_location("test_lock_plugin", sys.argv[1])
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)
# Isolate lock tests from the outer suite's real machine lock.
plugin._LOCK_PATH = Path(sys.argv[2])

class WorkerOption:
    def pytest_addoption(self, parser):
        # Exercise the xdist guard without installing or launching any workers.
        parser.addoption("--numprocesses", type=int, default=0)

raise SystemExit(pytest.main(sys.argv[3:], plugins=[plugin, WorkerOption()]))
"""


@pytest.fixture
def test_sessions(tmp_path):
    processes = []
    lock_path = tmp_path / "shared.lock"

    def start(name, *args, hold=False, fail_collection=False):
        checkout = tmp_path / name
        checkout.mkdir()
        (checkout / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        (checkout / "test_probe.py").write_text(
            "from pathlib import Path\n"
            "import time\n"
            "Path('collected').touch()\n"
            + ("raise RuntimeError('collection failed')\n" if fail_collection else "")
            + "def test_probe():\n"
            "    Path('started').touch()\n"
            + ("    while not Path('release').exists():\n"
               "        time.sleep(0.02)\n" if hold else "    pass\n"),
            encoding="utf-8",
        )
        process = subprocess.Popen(
            [sys.executable, "-c", _BOOTSTRAP, str(_PLUGIN), str(lock_path),
             "-q", "-c", "pytest.ini", "--confcutdir=.", *args],
            cwd=checkout,
            env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": ""},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        processes.append(process)
        return process, checkout

    yield start, lock_path
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


def _wait_for(path, process):
    deadline = time.monotonic() + 10
    while not path.exists():
        if process.poll() is not None:
            pytest.fail(f"pytest exited before creating {path}: {process.communicate()}")
        if time.monotonic() >= deadline:
            pytest.fail(f"pytest did not create {path}")
        time.sleep(0.02)


def _finish(process, expected=0):
    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == expected, stdout + stderr
    return stdout + stderr


def test_busy_session_reports_owner_without_collecting_and_waiter_takes_over(test_sessions):
    start, lock_path = test_sessions
    owner, first = start("first-checkout", hold=True)
    _wait_for(first / "started", owner)
    details = json.loads(lock_path.read_text())
    assert details["pid"] == owner.pid
    assert details["cwd"] == str(first)
    assert details["started_at"] and details["command"]

    contender, second = start("second-checkout")
    output = _finish(contender, expected=4)
    assert "Test lock busy" in output and str(owner.pid) in output and str(first) in output
    assert not (second / "collected").exists()

    waiter, third = start("third-checkout", "--test-lock-wait=5")
    assert "Waiting up to 5s for test lock" in waiter.stderr.readline()
    assert not (third / "collected").exists()
    (first / "release").touch()
    _finish(owner)
    assert "1 passed" in _finish(waiter)
    assert json.loads(lock_path.read_text())["pid"] == waiter.pid


def test_wait_timeout_does_not_collect_tests(test_sessions):
    start, _ = test_sessions
    owner, first = start("owner", hold=True)
    _wait_for(first / "started", owner)
    waiter, second = start("waiter", "--test-lock-wait=0.1")
    output = _finish(waiter, expected=4)
    assert "Waiting up to 0.1s" in output and "Test lock busy" in output
    assert not (second / "collected").exists()


def test_killed_owner_releases_lock_without_deleting_file(test_sessions):
    start, lock_path = test_sessions
    owner, first = start("owner", hold=True)
    _wait_for(first / "started", owner)
    inode = lock_path.stat().st_ino
    owner.kill()
    owner.communicate(timeout=10)
    successor, _ = start("successor")
    assert "1 passed" in _finish(successor)
    assert lock_path.stat().st_ino == inode


def test_collection_failure_releases_lock(test_sessions):
    start, _ = test_sessions
    broken, _ = start("broken", fail_collection=True)
    assert "collection failed" in _finish(broken, expected=2)
    successor, _ = start("successor")
    assert "1 passed" in _finish(successor)


@pytest.mark.parametrize("wait", ["-1", "nan", "inf"])
def test_invalid_wait_is_rejected_before_collection(test_sessions, wait):
    start, _ = test_sessions
    process, checkout = start("invalid", f"--test-lock-wait={wait}")
    assert "finite, nonnegative" in _finish(process, expected=4)
    assert not (checkout / "collected").exists()


@pytest.mark.parametrize("workers", [1, 2])
def test_pytest_workers_are_rejected_before_collection(test_sessions, workers):
    start, _ = test_sessions
    process, checkout = start("parallel", f"--numprocesses={workers}")
    assert "Tests must run serially" in _finish(process, expected=4)
    assert not (checkout / "collected").exists()
