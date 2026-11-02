"""Exercise fixture cleanup with real pytest sessions and the project config."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_BOOTSTRAP = """
import importlib.util
from pathlib import Path
import sys
import pytest

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
spec = importlib.util.spec_from_file_location('artifact_cleanup_plugin', root / 'conftest.py')
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)
raise SystemExit(pytest.main(sys.argv[2:], plugins=[plugin]))
"""


@pytest.fixture
def probe_suite(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "test_probe.py").write_text(
        "import json, os\nfrom pathlib import Path\n"
        "def test_probe(tmp_path, tmp_path_factory):\n"
        "    shared = tmp_path_factory.mktemp('shared')\n"
        "    (tmp_path / 'fixture.db').write_bytes(b'disposable')\n"
        "    (shared / 'fixture.db').write_bytes(b'disposable')\n"
        "    Path('created.json').write_text(json.dumps([str(tmp_path), str(shared)]))\n"
        "    assert not os.environ.get('FAIL_PROBE')\n",
        encoding="utf-8",
    )
    temproot = tmp_path / "temp"
    temproot.mkdir()

    def run(*args, fail=False, start=False, role='probe'):
        arguments = dict(
            cwd=checkout,
            env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": "",
                 "PYTEST_DEBUG_TEMPROOT": str(temproot), "FAIL_PROBE": "1" if fail else "",
                 "SESSION_ROLE": role},
        )
        command = (
            [sys.executable, "-c", _BOOTSTRAP, str(_ROOT),
             "-c", str(_ROOT / "pyproject.toml"), "--confcutdir", str(checkout),
             "-o", f"cache_dir={checkout / 'cache'}",
             "test_probe.py", *args]
        )
        if start:
            return subprocess.Popen(command, **arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return subprocess.run(command, **arguments, capture_output=True, text=True, timeout=30)

    return checkout, run


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("fail", [False, True])
def test_temporary_databases_are_removed_after_passing_and_failing_runs(probe_suite, explicit, fail):
    checkout, run = probe_suite
    basetemp = checkout / ".pytest_cache" / "dated-run"
    if explicit:
        basetemp.parent.mkdir()
    args = ["--basetemp", str(basetemp)] if explicit else []
    result = run(*args, fail=fail)
    assert result.returncode == (1 if fail else 0), result.stdout + result.stderr
    created = json.loads((checkout / "created.json").read_text())
    assert len(created) == 2
    assert all(not Path(path).exists() for path in created)
    assert all(not Path(path).parent.exists() for path in created)
    if explicit:
        assert not basetemp.exists()


def test_passing_run_accepts_pytest_removing_its_workspace_first(probe_suite):
    checkout, run = probe_suite
    result = run('-o', 'tmp_path_retention_policy=failed')
    assert result.returncode == 0, result.stdout + result.stderr
    created = json.loads((checkout / 'created.json').read_text())
    assert all(not Path(path).parent.exists() for path in created)


@pytest.mark.parametrize('explicit', [False, True])
@pytest.mark.parametrize('fail', [False, True])
def test_concurrent_focused_sessions_preserve_live_sqlite_and_clean_their_own_workspaces(probe_suite, explicit, fail):
    checkout, run = probe_suite
    (checkout / 'test_probe.py').write_text(
        "import json, os, sqlite3, time\n"
        "from contextlib import closing\nfrom pathlib import Path\n"
        "def test_probe(tmp_path, tmp_path_factory):\n"
        "    role = os.environ['SESSION_ROLE']\n"
        "    database = tmp_path / 'live.db'\n"
        "    shared = tmp_path_factory.mktemp('shared')\n"
        "    (shared / 'fixture.db').write_bytes(b'disposable')\n"
        "    with closing(sqlite3.connect(database)) as conn:\n"
        "        conn.execute('CREATE TABLE probe (value TEXT)')\n"
        "        conn.execute(\"INSERT INTO probe VALUES ('before')\")\n"
        "        conn.commit()\n"
        "        report = Path(role + '-ready.json')\n"
        "        report.with_suffix('.tmp').write_text(json.dumps([str(tmp_path), str(shared)]))\n"
        "        report.with_suffix('.tmp').replace(report)\n"
        "        if role == 'A':\n"
        "            deadline = time.monotonic() + 60\n"
        "            while not Path('resume-A').exists():\n"
        "                assert time.monotonic() < deadline, 'other session did not finish'\n"
        "                time.sleep(.02)\n"
        "            assert database.is_file(), 'other session deleted the live database'\n"
        "            assert list(conn.execute('SELECT value FROM probe')) == [('before',)]\n"
        "            conn.execute(\"INSERT INTO probe VALUES ('after')\")\n"
        "            conn.commit()\n"
        "            with closing(sqlite3.connect(database)) as reopened:\n"
        "                assert list(reopened.execute('SELECT value FROM probe')) == [('before',), ('after',)]\n"
        "            Path('A-resumed').touch()\n"
        "    assert not os.environ.get('FAIL_PROBE')\n",
        encoding='utf-8',
    )
    args_a = ['--basetemp', str(checkout / 'temp-A')] if explicit else []
    args_b = ['--basetemp', str(checkout / 'temp-B')] if explicit else []
    first = run(*args_a, start=True, role='A', fail=fail)
    try:
        deadline = time.monotonic() + 20
        while not (checkout / 'A-ready.json').exists():
            assert first.poll() is None, first.communicate()
            assert time.monotonic() < deadline, 'first session did not create its database'
            time.sleep(.02)
        active = json.loads((checkout / 'A-ready.json').read_text())
        second = run(*args_b, role='B', fail=fail)
        assert second.returncode == (1 if fail else 0), second.stdout + second.stderr
        assert all(Path(path).is_dir() for path in active)
        finished = json.loads((checkout / 'B-ready.json').read_text())
        assert all(not Path(path).parent.exists() for path in finished)
        (checkout / 'resume-A').touch()
        output, error = first.communicate(timeout=20)
        assert first.returncode == (1 if fail else 0), output + error
        assert (checkout / 'A-resumed').is_file()
        assert all(not Path(path).parent.exists() for path in active)
    finally:
        if first.poll() is None:
            first.terminate()
            first.communicate(timeout=10)


@pytest.mark.parametrize("args", [("--collect-only",), ("-k", "missing")])
def test_unused_explicit_basetemp_is_left_untouched(probe_suite, args):
    checkout, run = probe_suite
    basetemp = checkout / "unused"
    basetemp.mkdir()
    sentinel = basetemp / "existing.db"
    sentinel.write_bytes(b"untouched")
    result = run("--basetemp", str(basetemp), *args)
    assert result.returncode == (0 if args == ("--collect-only",) else 5), result.stdout + result.stderr
    assert not (checkout / "created.json").exists()
    assert sentinel.read_bytes() == b"untouched"


def test_oversized_parameter_ids_are_short_distinct_and_stable(probe_suite):
    checkout, run = probe_suite
    (checkout / "test_probe.py").write_text(
        "import pytest\n"
        "@pytest.mark.parametrize('value', ['a' * 1048576, 'b' * 1048576,\n"
        "    b'a' * 1048576, 'tiny', pytest.param('x' * 1048576, id='named')])\n"
        "def test_probe(value):\n"
        "    pass\n",
        encoding="utf-8",
    )
    result = run("--collect-only", "-o", "addopts=", "-q")
    assert result.returncode == 0, result.stdout + result.stderr
    nodeids = [line for line in result.stdout.splitlines() if "::test_probe[" in line]
    assert len(nodeids) == len(set(nodeids)) == 5
    assert all(len(nodeid) < 150 for nodeid in nodeids)
    assert sum("1048576chars" in nodeid for nodeid in nodeids) == 2
    assert sum("1048576bytes" in nodeid for nodeid in nodeids) == 1
    assert any("[tiny]" in nodeid for nodeid in nodeids)
    assert any("[named]" in nodeid for nodeid in nodeids)
    repeated = run("--collect-only", "-o", "addopts=", "-q")
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert [line for line in repeated.stdout.splitlines() if "::test_probe[" in line] == nodeids
