"""Serialize test sessions across agents and checkouts before test collection."""

from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import shlex
import sys
import time

import pytest


# Deliberately independent of checkout, cwd, TMPDIR, and pytest's cache directory.
# Never unlink this file: waiters must keep referring to the same inode.
_LOCK_PATH = Path(f"/tmp/tuneconsole-tests-{os.getuid()}.lock")


def pytest_addoption(parser):
    parser.addoption(
        "--test-lock-wait", type=float, default=0, metavar="SECONDS",
        help="Wait up to SECONDS for the shared test lock (default: fail immediately).",
    )
    parser.addoption(
        "--run-flatpak-build", action="store_true",
        help="Build and exercise real packaged Flatpak versions in the focused packaging tests.",
    )


def _owner(lock):
    lock.seek(0)
    return lock.read().strip() or "Owner details are being written."


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    wait = config.getoption("test_lock_wait")
    if not math.isfinite(wait) or wait < 0:
        raise pytest.UsageError("--test-lock-wait must be a finite, nonnegative number")
    if config.getoption("numprocesses", default=0) not in (None, 0):
        raise pytest.UsageError("Tests must run serially; remove pytest's -n option.")

    lock = _LOCK_PATH.open("a+", encoding="utf-8")
    # Cleanup runs after session teardown, and also when configuration fails.
    config.add_cleanup(lock.close)
    # Forked helpers must not keep the parent's lock alive after it exits.
    os.register_at_fork(after_in_child=lock.close)
    deadline = time.monotonic() + wait
    announced = False
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            owner = _owner(lock)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise pytest.UsageError(
                    f"Test lock busy: {_LOCK_PATH}\n{owner}\n"
                    "Hand test requests to the owner, or use --test-lock-wait=60. "
                    "Do not delete the lock file."
                ) from None
            if not announced:
                print(f"Waiting up to {wait:g}s for test lock: {_LOCK_PATH}\n{owner}",
                      file=sys.stderr, flush=True)
                announced = True
            time.sleep(min(0.1, remaining))

    lock.seek(0)
    lock.truncate()
    json.dump({
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "cwd": str(Path.cwd()),
        "command": shlex.join([sys.executable, *sys.argv]),
    }, lock)
    lock.write("\n")
    lock.flush()
