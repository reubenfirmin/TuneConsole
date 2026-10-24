"""Verify automatic worker selection and real subprocess ownership on tiny suites."""

import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

import local_test_workers as workers


def config_for(tmp_path, args=("tests",), **options):
    return SimpleNamespace(
        rootpath=tmp_path, args=list(args),
        invocation_params=SimpleNamespace(dir=tmp_path),
        getoption=lambda name, default=None: options.get(name, default),
    )


@pytest.mark.parametrize("args,options,expected", [
    (("tests",), {}, True), ((".",), {}, True),
    (("tests",), {"markexpr": "browser"}, True),
    (("tests/test_one.py",), {}, False),
    (("tests/browser",), {}, False),
    (("tests",), {"keyword": "one"}, False),
    (("tests",), {"markexpr": "slow"}, False),
    (("tests",), {"ci_shard": "1/2"}, False),
    (("tests",), {"lf": True}, False),
    (("tests",), {"collectonly": True}, False),
    (("tests",), {"usepdb": True}, False),
    (("tests",), {"capture": "no"}, False),
    (("tests",), {"run_live_radio": True}, False),
    (("tests",), {"run_flatpak_build": True}, False),
])
def test_only_complete_local_suites_get_workers(tmp_path, monkeypatch, args, options, expected):
    monkeypatch.delenv("CI", raising=False)
    assert workers.full_local_run(config_for(tmp_path, args, **options)) is expected


def test_ci_is_serial_and_default_count_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("CI", "true")
    assert not workers.full_local_run(config_for(tmp_path))
    monkeypatch.setattr(os, "sched_getaffinity", lambda _: set(range(16)))
    assert workers.default_workers() == 4
    monkeypatch.setattr(os, "sched_getaffinity", lambda _: {0})
    assert workers.default_workers() == 0


@pytest.fixture
def mini_suite(tmp_path):
    """Both controller and workers load the real plugins with an isolated lock."""
    repo = Path(__file__).resolve().parents[1]
    (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")
    (tmp_path / "tests").mkdir()
    lock_path = tmp_path / "owner.lock"
    (tmp_path / "conftest.py").write_text(
        "import importlib.util, sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(repo)!r})\n"
        f"spec = importlib.util.spec_from_file_location('mini_machine_owner', {str(repo / 'conftest.py')!r})\n"
        "plugin = importlib.util.module_from_spec(spec)\n"
        "sys.modules[spec.name] = plugin\nspec.loader.exec_module(plugin)\n"
        f"plugin._LOCK_PATH = Path({str(lock_path)!r})\n"
        "import local_test_workers\nlocal_test_workers.default_workers = lambda: 2\n"
        "pytest_plugins = ['mini_machine_owner']\n"
    )
    for name, other in (("one", "two"), ("two", "one")):
        (tmp_path / "tests" / f"test_{name}.py").write_text(
            "import json, os, time\nfrom pathlib import Path\nimport pytest\n"
            f"NAME = {name!r}\nOTHER = {other!r}\n"
            "@pytest.mark.parametrize('case', [0, 1])\n"
            "def test_probe(case):\n"
            "    if os.environ.get('RENDEZVOUS') and case == 0:\n"
            "        Path(NAME + '.started').touch()\n"
            "        deadline = time.monotonic() + 10\n"
            "        while not Path(OTHER + '.started').exists():\n"
            "            assert time.monotonic() < deadline, 'workers did not overlap'\n"
            "            time.sleep(0.02)\n"
            "    with Path(f'{NAME}-{case}.json').open('x') as out:\n"
            "        json.dump({'pid': os.getpid(), 'threads': os.environ.get('OPENBLAS_NUM_THREADS')}, out)\n"
            "    assert not os.environ.get('FAIL_PROBE')\n"
        )

    def run(*args, **env):
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "xdist.plugin", "-q", *args],
            cwd=tmp_path,
            env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": "",
                 "CI": "", **env}, capture_output=True, text=True, timeout=30,
        )

    return tmp_path, lock_path, run


def test_full_run_executes_every_case_once_on_parallel_file_workers(mini_suite):
    root, lock_path, run = mini_suite
    result = run(RENDEZVOUS="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 passed" in result.stdout
    records = {path.stem: json.loads(path.read_text()) for path in root.glob("*-*.json")}
    assert set(records) == {"one-0", "one-1", "two-0", "two-1"}
    assert records["one-0"]["pid"] == records["one-1"]["pid"]
    assert records["two-0"]["pid"] == records["two-1"]["pid"]
    assert records["one-0"]["pid"] != records["two-0"]["pid"]
    assert all(record["threads"] == "1" for record in records.values())
    assert json.loads(lock_path.read_text())["pid"] not in {record["pid"] for record in records.values()}


@pytest.mark.parametrize("args,env,count", [
    (("tests/test_one.py",), {}, 2),
    (("-n", "0"), {}, 4),
    ((), {"CI": "true"}, 4),
])
def test_focused_override_and_ci_runs_remain_serial(mini_suite, args, env, count):
    root, lock_path, run = mini_suite
    result = run(*args, **env)
    assert result.returncode == 0, result.stdout + result.stderr
    pids = [json.loads(path.read_text())["pid"] for path in root.glob("*-*.json")]
    assert len(pids) == count
    assert set(pids) == {json.loads(lock_path.read_text())["pid"]}


def test_explicit_workers_cannot_parallelize_a_focused_check(mini_suite):
    root, _, run = mini_suite
    result = run("tests/test_one.py", "-n", "2")
    assert result.returncode == 4
    assert "Tests must run serially" in result.stderr
    assert not list(root.glob("*-*.json"))


def test_worker_failures_fail_the_session(mini_suite):
    _, _, run = mini_suite
    result = run(FAIL_PROBE="1")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "4 failed" in result.stdout


def test_lingering_worker_barrier_blocks_admission_before_collection(mini_suite):
    root, lock_path, run = mini_suite
    # Simulate a worker retaining its shared lifetime lock after its owner died.
    lock_path.write_text('{"pid": 123, "command": "former coordinator"}')
    with workers.workers_lock_path(lock_path).open("a+") as barrier:
        fcntl.flock(barrier, fcntl.LOCK_SH)
        result = run("-n", "0")
        assert result.returncode == 4
        assert "Test lock busy" in result.stderr and "former coordinator" in result.stderr
        assert not list(root.glob("*-*.json"))
    result = run("-n", "0")
    assert result.returncode == 0, result.stdout + result.stderr


def test_unowned_worker_cannot_bypass_admission(tmp_path):
    config = SimpleNamespace(workerinput={})
    with pytest.raises(pytest.UsageError, match="no valid local coordinator"):
        workers.join_owner(config, tmp_path / "owner.lock")
