"""Select temporary storage and reserve coverage's namespace before collection."""

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import uuid

import pytest


_OWNER_ENV = "TUNECONSOLE_COVERAGE_OWNER"


def pytest_addoption(parser):
    from pytest_fixture_storage import addoption

    addoption(parser)


def _open_lock(config, path):
    lock = path.open("a+", encoding="utf-8")
    config.add_cleanup(lock.close)
    # Helpers forked by a test must not retain the pytest process's ownership.
    os.register_at_fork(after_in_child=lock.close)
    return lock


def _read_owner(lock):
    lock.seek(0)
    return lock.read().strip()


def _busy(destination, owner):
    raise pytest.UsageError(
        f"Coverage destination busy: {destination}\n{owner}\n"
        "Use --cov-config with a private [run] data_file, or rerun after its owner and workers exit. "
        "Do not delete coverage ownership lock files."
    ) from None


def _check_overlaps(directory, destination):
    # Coverage erases/combines basename.* as well as basename itself. Distinct
    # basenames therefore conflict when either is the other's dotted suffix.
    for path in directory.glob("*.lock"):
        if len(path.stem) != 64:  # Only destination records, not admission/workers.
            continue
        with path.open(encoding="utf-8") as lock:
            owner = _read_owner(lock)
            try:
                other = Path(json.loads(owner)["destination"])
            except (ValueError, KeyError, TypeError):
                continue
            if destination.parent != other.parent or not (
                destination == other or destination.name.startswith(other.name + ".")
                or other.name.startswith(destination.name + ".")
            ):
                continue
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with path.with_stem(path.stem + "-workers").open("a+") as workers:
                    fcntl.flock(workers, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                _busy(destination, owner)


def _reserve(config, destination):
    # These stable inodes must live outside coverage's basename.* erase glob.
    # Different checkouts and symlink spellings of one destination share them.
    directory = Path(f"/tmp/tuneconsole-coverage-{os.getuid()}")
    directory.mkdir(mode=0o700, exist_ok=True)
    digest = hashlib.sha256(os.fsencode(destination)).hexdigest()
    lock = _open_lock(config, directory / f"{digest}.lock")
    workers = _open_lock(config, directory / f"{digest}-workers.lock")
    try:
        inherited = json.loads(os.environ.get(_OWNER_ENV, "null"))
    except ValueError:
        inherited = None
    worker = (isinstance(inherited, dict) and inherited.get("destination") == str(destination)
              and inherited.get("pid") == os.getppid() and bool(os.environ.get("PYTEST_XDIST_WORKER")))
    try:
        if worker:
            fcntl.flock(workers, fcntl.LOCK_SH | fcntl.LOCK_NB)
            if json.loads(_read_owner(lock)) != inherited:
                raise pytest.UsageError("Coverage worker could not verify its coordinator's destination ownership.")
            owner = inherited
        else:
            # Serialize just the namespace check and claim so two new dotted
            # basenames cannot both pass admission. Collection remains concurrent.
            with (directory / "admission.lock").open("a+") as admission:
                fcntl.flock(admission, fcntl.LOCK_EX)
                _check_overlaps(directory, destination)
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(workers, fcntl.LOCK_EX | fcntl.LOCK_NB)
                owner = {"pid": os.getpid(), "nonce": uuid.uuid4().hex,
                         "destination": str(destination), "cwd": str(Path.cwd()),
                         "started_at": datetime.now(timezone.utc).isoformat()}
                lock.seek(0)
                lock.truncate()
                json.dump(owner, lock)
                lock.flush()
                # The coordinator retains its destination lock. Workers retain
                # this shared lifetime barrier if the coordinator is killed.
                fcntl.flock(workers, fcntl.LOCK_SH)
    except BlockingIOError:
        _busy(destination, _read_owner(lock))

    config._tuneconsole_coverage_owner = owner
    config._tuneconsole_coverage_worker = worker
    if not worker:
        previous = os.environ.get(_OWNER_ENV)
        os.environ[_OWNER_ENV] = json.dumps(owner)

        def restore():
            if previous is None:
                os.environ.pop(_OWNER_ENV, None)
            else:
                os.environ[_OWNER_ENV] = previous

        config.add_cleanup(restore)
    return worker


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_load_initial_conftests(early_config):
    from pytest_fixture_storage import prepare

    prepare(early_config)
    options = early_config.known_args_namespace
    if not getattr(options, "cov_source", None) or getattr(options, "no_cov", False):
        return (yield)

    from coverage import Coverage

    # Loading configuration does not start collection or erase existing data.
    # Coverage itself resolves COVERAGE_FILE and configuration substitutions.
    destination = Path(Coverage(config_file=options.cov_config).get_option("run:data_file")).resolve()
    if not _reserve(early_config, destination):
        return (yield)

    # pytest-cov 7 starts a coordinator even during a worker's early parse,
    # before xdist installs workerinput. Defer that worker's collector until
    # sessionstart chooses DistWorker, which never erases the shared data.
    sources = options.cov_source
    options.cov_source = []
    try:
        result = yield
    finally:
        options.cov_source = sources
    from pytest_cov.plugin import CovPlugin

    early_config.pluginmanager.register(CovPlugin(options, early_config.pluginmanager, start=False), "_cov")
    return result


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node):
    owner = getattr(node.config, "_tuneconsole_coverage_owner", None)
    if owner is not None:
        node.workerinput["tuneconsole_coverage_owner"] = owner


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    if not hasattr(config, "workerinput") or not hasattr(config, "_tuneconsole_coverage_owner"):
        return
    if (not config._tuneconsole_coverage_worker
            or config.workerinput.get("tuneconsole_coverage_owner") != config._tuneconsole_coverage_owner):
        raise pytest.UsageError("Coverage worker has no verified local coordinator; shared data remains protected.")
