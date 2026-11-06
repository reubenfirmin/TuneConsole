"""Exercise fixture cleanup with real pytest sessions and the project config."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_BOOTSTRAP = """
import importlib.util
from pathlib import Path
import sys
import os
import tempfile
import pytest

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
spec = importlib.util.spec_from_file_location('artifact_cleanup_plugin', root / 'conftest.py')
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)
original_directory, original_environment = tempfile.gettempdir(), os.environ.get('TMPDIR')
original_sqlite_environment = os.environ.get('SQLITE_TMPDIR')
result = pytest.main(sys.argv[2:], plugins=[plugin])
assert tempfile.tempdir == original_directory
assert os.environ.get('TMPDIR') == original_environment
assert os.environ.get('SQLITE_TMPDIR') == original_sqlite_environment
raise SystemExit(result)
"""


@pytest.fixture
def probe_suite(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "test_probe.py").write_text(
        """import json, os, sqlite3
from contextlib import closing
from pathlib import Path
import pytest

def make_input(factory, name):
    path = factory.mktemp(name)
    with closing(sqlite3.connect(path / 'input.db')) as conn:
        conn.execute('CREATE TABLE probe (value TEXT)')
        conn.execute('INSERT INTO probe VALUES (?)', (name,))
        conn.commit()
    return path

@pytest.fixture(scope='session')
def session_input(tmp_path_factory):
    path = make_input(tmp_path_factory, 'session')
    yield path
    assert (path / 'input.db').is_file()

@pytest.fixture(scope='module')
def module_input(tmp_path_factory):
    path = make_input(tmp_path_factory, 'module')
    yield path
    assert (path / 'input.db').is_file()

@pytest.fixture
def probe_state(tmp_path, session_input, module_input):
    receipt = Path('storage.jsonl')
    previous = [json.loads(line) for line in receipt.read_text().splitlines()] if receipt.exists() else []
    assert all(not Path(row['function']).exists() for row in previous)
    paths = [tmp_path, session_input, module_input]
    (tmp_path / 'fixture.db').write_bytes(b'x' * 1048576)
    Path('created.json').write_text(json.dumps([str(path) for path in paths]))
    for shared in paths[1:]:
        with closing(sqlite3.connect(shared / 'input.db')) as conn:
            assert conn.execute('SELECT value FROM probe').fetchone()
            conn.execute('INSERT INTO probe VALUES (?)', (str(tmp_path),))
            conn.commit()
    base = tmp_path.parent
    usage = os.statvfs(base)
    with receipt.open('a') as stream:
        stream.write(json.dumps({'function': str(tmp_path),
            'bytes': sum(path.stat().st_size for root in paths for path in root.rglob('*') if path.is_file()),
            'free_bytes': usage.f_bavail * usage.f_frsize,
            'directories': len([path for path in base.iterdir() if path.is_dir() and not path.is_symlink()])}) + '\\n')
    print('fixture diagnostic saved outside disposable workspace')
    if os.environ.get('FAIL_PROBE') == 'setup':
        raise RuntimeError('setup diagnostic')
    yield tmp_path
    assert (tmp_path / 'fixture.db').is_file()
    if os.environ.get('FAIL_PROBE') == 'teardown':
        raise RuntimeError('teardown diagnostic')

@pytest.mark.parametrize('iteration', range(8))
def test_probe(probe_state, iteration):
    assert os.environ.get('FAIL_PROBE') != 'call', 'call diagnostic'

def test_after():
    rows = [json.loads(line) for line in Path('storage.jsonl').read_text().splitlines()]
    assert len(rows) == 8
    assert all(not Path(row['function']).exists() for row in rows)
    assert all(row['directories'] == 4 and 1048576 <= row['bytes'] < 1200000 for row in rows)
""",
        encoding="utf-8",
    )
    temproot = tmp_path / "temp"
    temproot.mkdir()

    def run(*args, fail=False, start=False, role='probe'):
        arguments = dict(
            cwd=checkout,
            env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": "",
                 "PYTEST_DEBUG_TEMPROOT": str(temproot), "FAIL_PROBE": str(fail) if fail else "",
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
@pytest.mark.parametrize("fail", [False, 'setup', 'call', 'teardown'])
def test_temporary_databases_are_removed_after_passing_and_failing_runs(
        probe_suite, explicit, fail, record_testsuite_property):
    checkout, run = probe_suite
    basetemp = checkout / ".pytest_cache" / "dated-run"
    if explicit:
        basetemp.parent.mkdir()
    args = ["--basetemp", str(basetemp)] if explicit else []
    result = run(*args, '--junitxml', str(checkout / 'junit.xml'), '-o', 'junit_logging=all', fail=fail)
    assert result.returncode == (1 if fail else 0), result.stdout + result.stderr
    created = json.loads((checkout / "created.json").read_text())
    assert len(created) == 3
    assert all(not Path(path).exists() for path in created)
    assert all(not Path(path).parent.exists() for path in created)
    samples = [json.loads(line) for line in (checkout / 'storage.jsonl').read_text().splitlines()]
    record_testsuite_property(f'workspace-{fail}-{explicit}', json.dumps({
        'samples': len(samples), 'peak_bytes': max(row['bytes'] for row in samples),
        'peak_directories': max(row['directories'] for row in samples),
        'min_free_bytes': min(row['free_bytes'] for row in samples)}))
    if explicit:
        assert not basetemp.exists()
    reports = ET.parse(checkout / 'junit.xml').findall('.//testcase')
    assert len(reports) == 9
    assert reports[-1].get('name') == 'test_after'
    assert reports[-1].find('failure') is None and reports[-1].find('error') is None
    if fail:
        diagnostics = [entry for report in reports for entry in report if entry.tag in ('failure', 'error')]
        assert len(diagnostics) == 8
        assert all(f'{fail} diagnostic' in (entry.text or '') for entry in diagnostics)
        assert all('fixture diagnostic saved outside disposable workspace' in report.findtext('system-out', '')
                   for report in reports[:-1])


def test_passing_run_accepts_pytest_removing_its_workspace_first(probe_suite):
    checkout, run = probe_suite
    result = run('-o', 'tmp_path_retention_policy=failed')
    assert result.returncode == 0, result.stdout + result.stderr
    created = json.loads((checkout / 'created.json').read_text())
    assert all(not Path(path).parent.exists() for path in created)


def test_default_fixture_storage_uses_the_checkout_filesystem(probe_suite):
    checkout, run = probe_suite
    result = run()
    assert result.returncode == 0, result.stdout + result.stderr
    created = [Path(path) for path in json.loads((checkout / 'created.json').read_text())]
    assert all(path.is_relative_to(_ROOT / 'build' / 'pytest') for path in created)
    assert all(not path.parent.exists() for path in created)


def test_fixture_root_allocates_unique_cleaned_session_directories(probe_suite):
    checkout, run = probe_suite
    root = checkout / 'disk-fixtures'
    bases = []
    for _ in range(2):
        result = run('--fixture-root', str(root))
        assert result.returncode == 0, result.stdout + result.stderr
        created = [Path(path) for path in json.loads((checkout / 'created.json').read_text())]
        bases.append(created[0].parent)
        assert all(path.parent.parent == root for path in created)
        assert all(not path.parent.exists() for path in created)
        (checkout / 'storage.jsonl').unlink()
    assert bases[0] != bases[1]


def test_explicit_basetemp_takes_precedence_over_fixture_root(probe_suite):
    checkout, run = probe_suite
    root, base = checkout / 'unused-root', checkout / 'chosen-base'
    result = run('--fixture-root', str(root), '--basetemp', str(base))
    assert result.returncode == 0, result.stdout + result.stderr
    created = [Path(path) for path in json.loads((checkout / 'created.json').read_text())]
    assert all(path.parent == base for path in created)
    assert not base.exists() and not root.exists()


@pytest.mark.parametrize('placement', ['default', 'fixture-root', 'basetemp'])
@pytest.mark.parametrize('outcome', ['pass', 'fail', 'interrupt'])
def test_implicit_temporaries_and_memory_store_files_follow_the_owned_factory(
        probe_suite, placement, outcome):
    checkout, run = probe_suite
    (checkout / 'test_probe.py').write_text('''import json, os, sqlite3, tempfile
from contextlib import closing
from pathlib import Path
from yt_playlist.core import fao
from yt_playlist.core.store import Store

def test_storage(tmp_path, tmp_path_factory, request):
    base = tmp_path_factory.getbasetemp()
    implicit = Path(tempfile.mkdtemp(prefix='left-by-test-'))
    explicit_root = tmp_path / 'explicit'
    explicit_root.mkdir()
    with tempfile.TemporaryDirectory(dir=explicit_root) as explicit:
        store = Store(':memory:')
        try:
            managed = fao.root_for_database(None, store._evidence_key, 'recommendations')
            fao.write_bytes(managed / 'proof', b'private fixture')
            paths = [implicit, managed, Path(tempfile.gettempdir())]
            assert all(path.is_relative_to(base) for path in paths)
            assert Path(explicit).parent == explicit_root
            assert os.environ['TMPDIR'] == tempfile.gettempdir()
            sqlite_directory = request.config._sqlite_test_storage
            assert Path(os.environ['SQLITE_TMPDIR']) == sqlite_directory
            assert sqlite_directory.stat().st_dev == base.stat().st_dev
            # A small cache forces real SQLite VFS temporary I/O. An
            # environment-only session fixture passes the assertions above
            # while SQLite still opens these deleted files on /tmp.
            with closing(sqlite3.connect('')) as temporary:
                temporary.execute('PRAGMA cache_size=-16')
                temporary.execute('CREATE TABLE probe(value BLOB)')
                temporary.executemany('INSERT INTO probe VALUES (zeroblob(65536))', [()] * 8)
                temporary.commit()
                if Path('/proc/self/fd').is_dir():
                    files = []
                    for descriptor in Path('/proc/self/fd').iterdir():
                        try:
                            target = str(descriptor.readlink())
                        except FileNotFoundError:
                            continue
                        if 'etilqs_' in target:
                            files.append(target.removesuffix(' (deleted)'))
                    assert files, 'SQLite temporary database never reached disk'
                    assert all(Path(path).is_relative_to(sqlite_directory) for path in files), files
            Path('placement.json').write_text(json.dumps({
                'base': str(base), 'paths': list(map(str, paths)),
                'sqlite_storage': str(sqlite_directory),
                'lock': str(__import__('artifact_cleanup_plugin')._LOCK_PATH)}))
            (implicit / 'disposable').write_bytes(b'left for owning factory cleanup')
            outcome = os.environ['SESSION_ROLE']
            if outcome == 'interrupt':
                raise KeyboardInterrupt('controlled storage interruption')
            assert outcome != 'fail', 'controlled storage failure'
        finally:
            store.conn.close()
''', encoding='utf-8')
    # A different ambient TMPDIR must not override the selected fixture volume.
    root, base = checkout / 'chosen-root', checkout / 'chosen-base'
    args = (['--fixture-root', str(root)] if placement == 'fixture-root' else
            ['--fixture-root', str(root), '--basetemp', str(base)] if placement == 'basetemp' else [])
    if placement == 'default' and outcome == 'pass':
        config = checkout / 'coverage.ini'
        config.write_text('[run]\ncore = ctrace\ndata_file = ' + str(checkout / 'coverage.sqlite3') + '\n')
        args += ['-p', 'pytest_cov.plugin', '--cov=yt_playlist.core.store', '--cov-context=test',
                 '--cov-report=', '--cov-config=' + str(config)]
    result = run(*args, role=outcome)
    assert result.returncode == {'pass': 0, 'fail': 1, 'interrupt': 2}[outcome], result.stdout + result.stderr
    receipt = json.loads((checkout / 'placement.json').read_text())
    actual_base = Path(receipt['base'])
    assert all(Path(path).is_relative_to(actual_base) and not Path(path).exists()
               for path in receipt['paths'])
    assert not actual_base.exists()
    sqlite_storage = Path(receipt['sqlite_storage'])
    assert not sqlite_storage.parent.exists()
    assert sqlite_storage.parent.parent == actual_base.parent
    assert receipt['lock'] == f'/tmp/tuneconsole-tests-{os.getuid()}.lock'
    if placement == 'fixture-root':
        assert actual_base.parent == root
    elif placement == 'basetemp':
        assert actual_base == base and not root.exists()
    else:
        assert actual_base.parent == _ROOT / 'build' / 'pytest'


@pytest.mark.parametrize('args', [('--collect-only',), ('-k', 'missing')])
def test_unused_fixture_root_does_not_allocate_a_session_base(probe_suite, args):
    checkout, run = probe_suite
    root = checkout / 'unused-fixtures'
    result = run('--fixture-root', str(root), *args)
    assert result.returncode == (0 if args == ('--collect-only',) else 5), result.stdout + result.stderr
    assert not list(root.iterdir())


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
    # The checkout/temp prefix can be long; bound the parametrized test name.
    assert all(len(nodeid.partition('::')[2]) < 150 for nodeid in nodeids)
    assert sum("1048576chars" in nodeid for nodeid in nodeids) == 2
    assert sum("1048576bytes" in nodeid for nodeid in nodeids) == 1
    assert any("[tiny]" in nodeid for nodeid in nodeids)
    assert any("[named]" in nodeid for nodeid in nodeids)
    repeated = run("--collect-only", "-o", "addopts=", "-q")
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert [line for line in repeated.stdout.splitlines() if "::test_probe[" in line] == nodeids
