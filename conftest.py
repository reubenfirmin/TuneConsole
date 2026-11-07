"""Lock complete suites; focused checks run independently without the machine lock."""

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import time
import uuid

import pytest

pytest_plugins = ["ci_test_shards", "local_test_workers", "pytest_progress", "pytest_time_limit", "tests.browser_fixtures"]


# Deliberately independent of checkout, cwd, TMPDIR, and pytest's cache directory.
# Never unlink this file: waiters must keep referring to the same inode.
_LOCK_PATH = Path(f"/tmp/tuneconsole-tests-{os.getuid()}.lock")
_FUNCTION_WORKSPACE = pytest.StashKey[Path]()


@pytest.fixture(scope='session', autouse=True)
def stdlib_test_storage(tmp_path_factory):
    """Keep implicit temporary files on this session's selected fixture volume.

    Explicit dir= arguments still win. The factory owns cleanup, including files
    left behind by failed tests; shared machine locks keep their fixed /tmp path.
    """
    directory = tmp_path_factory.mktemp('stdlib')
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('TMPDIR', str(directory))
        patch.setattr(tempfile, 'tempdir', str(directory))
        yield directory


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef, request):
    value = yield
    factory = getattr(request.config, '_tmp_path_factory', None)
    if (fixturedef.argname == 'tmp_path' and fixturedef.scope == 'function'
            and isinstance(value, Path) and factory is not None
            and value.parent == factory._basetemp):
        request.node.stash[_FUNCTION_WORKSPACE] = value
    return value


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    try:
        result = yield
    except BaseException:
        # Pytest closes capture in pytest_keyboard_interrupt/internalerror,
        # before its deferred sessionfinish fixture teardown. Drain admitted
        # requests while the Store, native files and captured log streams are
        # still owned. The normal runner has already unwound this protocol.
        monitor = item.config.pluginmanager.getplugin('test-session-time-limit')
        if monitor is not None:
            monitor.stop()
        item.session._setupstate.teardown_exact(None)
        raise
    # A completed protocol has run its fixture finalizers and report hooks.
    # Interrupted protocols drain above and leave directory removal to session
    # cleanup, including failures raised by a fixture finalizer.
    # Remove only this function's fixture, never factory-created shared
    # templates or another session's active, locked base directory.
    directory = item.stash.get(_FUNCTION_WORKSPACE, None)
    if directory is not None:
        try:
            shutil.rmtree(directory)
        except FileNotFoundError:
            pass  # An explicit pytest retention override may clean first.
    return result


def pytest_make_parametrize_id(val, argname):
    # Oversized-input checks must not embed megabytes of fixture data in node
    # IDs, which pytest stores persistently and repeats in failure reports.
    if isinstance(val, (str, bytes)) and len(val) > 120:
        data = val.encode("utf-8", errors="surrogatepass") if isinstance(val, str) else val
        digest = hashlib.sha256(data).hexdigest()[:12]
        unit = "chars" if isinstance(val, str) else "bytes"
        return f"{argname}-{len(val)}{unit}-{digest}"


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_sessionfinish(session):
    try:
        import local_test_workers

        # A monitor interrupt leaves worker fixtures alive. Closing execnet
        # gateways first would interrupt those finalizers a second time.
        local_test_workers.drain_workers(session.config)
        return (yield)
    finally:
        # Keep pytest's active-directory protection during the session, then
        # remove only this factory's workspace, including failures and explicit
        # --basetemp. Wait until teardown and report writers finish; collection
        # may never have used the factory or created the requested directory.
        factory = getattr(session.config, "_tmp_path_factory", None)
        if factory is not None:
            if factory._basetemp is not None:
                try:
                    shutil.rmtree(factory._basetemp)
                except FileNotFoundError:
                    # Pytest may already have cleaned this completed session
                    # when its retention policy was explicitly overridden.
                    pass


def pytest_addoption(parser):
    from pytest_fixture_storage import addoption

    addoption(parser)
    parser.addoption('--listening-acceptance-url', default=None, metavar='URL',
        help='Inspect Charts in an already running app without recording review impressions.')
    parser.addoption('--listening-acceptance-db', default=None, metavar='PATH',
        help='Run listening visual acceptance against a read-only copy of this saved database.')
    parser.addoption('--group-readonly-db', default=None, metavar='PATH',
        help='Benchmark musical groups using read-only prepared inputs from this database.')
    parser.addoption('--group-snapshot', default=None, metavar='PATH',
        help='Save or replay the local musical-group input snapshot.')
    parser.addoption('--group-report', default=None, metavar='PATH',
        help='Write the musical-group benchmark report to this local path.')
    parser.addoption('--vibe-readonly-db', default=None, metavar='PATH',
        help='Rehearse vibe migration and startup on an isolated copy of this read-only database.')
    parser.addoption(
        "--test-lock-wait", type=float, default=0, metavar="SECONDS",
        help="Wait up to SECONDS for the full-suite test lock (default: fail immediately).",
    )
    parser.addoption(
        "--run-flatpak-build", action="store_true",
        help="Build and exercise real packaged Flatpak versions in the focused packaging tests.",
    )
    parser.addoption(
        "--run-live-radio", action="store_true",
        help="Switch one continuation in the running, signed-in Radio Session and observe playback.",
    )
    parser.addoption(
        "--live-radio-trace", default=None, metavar="PATH",
        help="Retain the live Radio acceptance trace at PATH instead of pytest's temporary directory.",
    )
    parser.addoption(
        "--live-radio-song-smoke", action="store_true",
        help="Limit live stream-song acceptance to one middle-of-stream click and its playback latency.",
    )


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config):
    # Configure before pytest constructs TempPathFactory. Each session owns one
    # base; pytest only creates it when a fixture actually requests storage.
    # Workers receive their coordinator's explicit child base from xdist.
    if config.option.basetemp is None and not hasattr(config, 'workerinput'):
        root = Path(config.getoption('fixture_root') or Path(__file__).parent / 'build' / 'pytest')
        root.mkdir(parents=True, exist_ok=True)
        config.option.basetemp = str(root / f'run-{os.getpid()}-{uuid.uuid4().hex}')


def _owner(lock):
    lock.seek(0)
    return lock.read().strip() or "Owner details are being written."


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    import local_test_workers

    wait = config.getoption("test_lock_wait")
    if not math.isfinite(wait) or wait < 0:
        raise pytest.UsageError("--test-lock-wait must be a finite, nonnegative number")
    if hasattr(config, "workerinput"):
        local_test_workers.join_owner(config, _LOCK_PATH)
        return
    local_test_workers.validate(config)
    if not local_test_workers.full_suite(config):
        return

    lock = _LOCK_PATH.open("a+", encoding="utf-8")
    # Cleanup runs after session teardown, and also when configuration fails.
    config.add_cleanup(lock.close)
    # Forked helpers must not keep the parent's lock alive after it exits.
    os.register_at_fork(after_in_child=lock.close)
    # Workers keep this barrier shared even if their coordinator is killed.
    # Admission stays exclusive on the original lock until every worker exits.
    workers = local_test_workers.workers_lock_path(_LOCK_PATH).open("a+", encoding="utf-8")
    config.add_cleanup(workers.close)
    os.register_at_fork(after_in_child=workers.close)
    deadline = time.monotonic() + wait
    announced = False
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(workers, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            fcntl.flock(lock, fcntl.LOCK_UN)
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
    owner = {
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "cwd": str(Path.cwd()),
        "command": shlex.join([sys.executable, *sys.argv]),
        "nonce": uuid.uuid4().hex,
        "path": str(_LOCK_PATH),
    }
    json.dump(owner, lock)
    lock.write("\n")
    lock.flush()
    local_test_workers.share_owner(config, workers, owner)
