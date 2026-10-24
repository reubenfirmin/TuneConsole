"""Parallelize complete local suites under one machine-wide test owner."""
import fcntl
import json
import os

import pytest


def full_local_run(config):
    if os.environ.get("CI") or config.getoption("ci_shard", default=None):
        return False
    if any(config.getoption(name, default=False) for name in (
        "collectonly", "help", "version", "usepdb", "lf", "stepwise", "keyword", "deselect",
        "run_flatpak_build", "run_live_radio",
    )):
        return False
    if config.getoption("markexpr", default="") not in ("", "not browser", "browser"):
        return False
    if config.getoption("capture", default="fd") == "no":
        return False
    if len(config.args) != 1:
        return False
    selected = (config.invocation_params.dir / config.args[0]).resolve()
    return selected in (config.rootpath.resolve(), (config.rootpath / "tests").resolve())


def has_xdist(config):
    return any(config.pluginmanager.hasplugin(name) for name in ("xdist", "xdist.plugin"))


def default_workers():
    try:
        cpus = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cpus = os.cpu_count() or 1
    return min(4, cpus) if cpus > 1 else 0


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config):
    # Run before xdist translates -n into its worker startup configuration.
    if hasattr(config, "workerinput") or not has_xdist(config) or not full_local_run(config):
        return
    if config.option.numprocesses is None and not config.option.tx:
        config.option.numprocesses = default_workers()
    if config.option.numprocesses and config.option.dist == "no":
        config.option.dist = "loadfile"
    if config.option.maxworkerrestart is None:
        config.option.maxworkerrestart = 0


def validate(config):
    workers = config.getoption("numprocesses", default=0)
    transports = config.getoption("tx", default=[])
    if workers in (None, 0) and not transports:
        return
    if not has_xdist(config) or not full_local_run(config):
        raise pytest.UsageError(
            "Tests must run serially for focused checks and CI shards; "
            "worker processes are reserved for full local runs. Remove -n."
        )
    if config.getoption("dist") != "loadfile" or any(spec != "popen" for spec in transports):
        raise pytest.UsageError("Full local runs require local workers with --dist=loadfile.")


def workers_lock_path(owner_path):
    return owner_path.with_name(owner_path.stem + "-workers.lock")


def share_owner(config, lock, owner):
    """The coordinator keeps admission exclusive while its workers share a barrier."""
    if config.getoption("numprocesses", default=0) in (None, 0):
        return
    config._tuneconsole_test_owner = owner
    fcntl.flock(lock, fcntl.LOCK_SH)
    variables = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
                 "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "BLIS_NUM_THREADS")
    previous = {name: os.environ.get(name) for name in variables}
    for name in variables:
        os.environ[name] = "1"

    def restore():
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    config.add_cleanup(restore)


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node):
    node.workerinput["tuneconsole_test_owner"] = node.config._tuneconsole_test_owner


def join_owner(config, owner_path):
    proof = config.workerinput.get("tuneconsole_test_owner")
    if (not isinstance(proof, dict) or proof.get("path") != str(owner_path)
            or proof.get("pid") != os.getppid()):
        raise pytest.UsageError("Test worker has no valid local coordinator; acquire the machine lock normally.")
    lock = workers_lock_path(owner_path).open("a+", encoding="utf-8")
    config.add_cleanup(lock.close)
    os.register_at_fork(after_in_child=lock.close)
    try:
        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        owner = json.loads(owner_path.read_text())
    except (BlockingIOError, OSError, ValueError):
        raise pytest.UsageError("Test worker could not join its coordinator's machine lock.") from None
    if any(owner.get(key) != proof.get(key) for key in ("pid", "nonce", "path")):
        raise pytest.UsageError("Test worker's coordinator no longer owns the machine lock.")
