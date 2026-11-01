"""Exercise fixture cleanup with real pytest sessions and the project config."""

import json
import os
from pathlib import Path
import subprocess
import sys

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

    def run(*args, fail=False):
        return subprocess.run(
            [sys.executable, "-c", _BOOTSTRAP, str(_ROOT),
             "-c", str(_ROOT / "pyproject.toml"), "--confcutdir", str(checkout),
             "-o", f"cache_dir={checkout / 'cache'}",
             "test_probe.py", *args],
            cwd=checkout,
            env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": "",
                 "PYTEST_DEBUG_TEMPROOT": str(temproot), "FAIL_PROBE": "1" if fail else ""},
            capture_output=True, text=True, timeout=30,
        )

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
