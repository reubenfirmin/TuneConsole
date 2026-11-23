"""Manager lifetime tests on tiny simulated storage; CI needs no Btrfs/Podman."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
import fcntl
import json
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace
import uuid

import pytest

from tests.library_image import manager as image
from tests.library_image.schema_coordinates import read_schema


def write_schema(database, version, *, regular=None):
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY)')
        connection.execute('INSERT INTO schema_migrations VALUES (?)', (version,))
        if regular is not None:
            connection.execute('CREATE TABLE schema_changes(version INTEGER PRIMARY KEY)')
            connection.execute('INSERT INTO schema_changes VALUES (?)', (regular,))


@pytest.fixture
def backend(tmp_path, monkeypatch):
    repository = tmp_path / 'repository'
    repository.mkdir()
    monkeypatch.setattr(image, 'ROOT', repository)
    volume = tmp_path / 'volume'
    volume.mkdir()
    flags, calls, containers = {}, [], set()
    failures = set()

    def command(*arguments, check=True, **options):
        arguments = tuple(str(arg) for arg in arguments)
        calls.append(arguments)
        result = SimpleNamespace(returncode=0, stdout='', stderr='')
        if arguments[:2] == ('stat', '-f'):
            result.stdout = 'btrfs\n'
        elif arguments[:3] == ('btrfs', 'subvolume', 'create'):
            path = Path(arguments[-1])
            path.mkdir()
            flags[path] = False
        elif arguments[:3] == ('btrfs', 'subvolume', 'snapshot'):
            source, path = map(Path, arguments[-2:])
            shutil.copytree(source, path)
            flags[path] = False
            if 'snapshot' in failures:
                raise RuntimeError('controlled partial snapshot failure')
        elif arguments[:4] == ('btrfs', 'property', 'set', '-ti'):
            assert arguments[-2:] == ('compression', 'zstd')
        elif arguments[:4] == ('btrfs', 'property', 'set', '-ts'):
            flags[Path(arguments[4])] = arguments[-1] == 'true'
        elif arguments[:3] == ('btrfs', 'property', 'get'):
            result.stdout = 'ro=' + str(flags[Path(arguments[4])]).lower()
        elif arguments[:3] == ('btrfs', 'subvolume', 'delete'):
            path = Path(arguments[-1])
            assert not flags[path], 'Frozen subvolume must be thawed only for deletion'
            shutil.rmtree(path)
            del flags[path]
        elif arguments[:3] == ('btrfs', 'filesystem', 'sync'):
            pass
        elif arguments[:2] == ('podman', 'run'):
            bindings = [arg for arg in arguments if ':/library:' in arg]
            target = Path(bindings[0].split(':/library:')[0])
            if arguments[-2] == 'tests.library_image.capture':
                if 'capture' in failures:
                    raise RuntimeError('controlled capture failure')
                shutil.copyfile(arguments[-1], target / 'state.db')
                with closing(sqlite3.connect(target / 'state.db')) as connection:
                    (target / '.capture.json').write_text(json.dumps(read_schema(connection)))
        elif arguments[:3] == ('podman', 'image', 'inspect'):
            result.stdout = 'sha256:controlled-runtime'
        elif arguments[:2] == ('podman', 'rm'):
            containers.discard(arguments[-1])
        elif arguments[:3] == ('podman', 'container', 'exists'):
            result.returncode = 0 if arguments[-1] in containers else 1
        else:
            raise AssertionError(arguments)
        return result

    monkeypatch.setattr(image, 'command', command)
    source = tmp_path / 'native.db'
    write_schema(source, 123)
    manager = image.Manager(volume).initialize()
    reports = repository / 'build/test-reports/checks'
    return SimpleNamespace(manager=manager, source=source, calls=calls, flags=flags,
                           failures=failures, containers=containers, root=reports)


def test_concurrent_preparation_captures_once_and_reuses_frozen_identity(backend):
    donor = backend.source.read_bytes()
    with ThreadPoolExecutor(2) as workers:
        values = list(workers.map(lambda _: backend.manager.prepare(backend.source), range(2)))
    assert values[0]['id'] == values[1]['id']
    captures = [call for call in backend.calls if call[:2] == ('podman', 'run')]
    assert len(captures) == 1
    baseline = backend.manager.volume / 'baselines' / values[0]['id']
    compression = ('btrfs', 'property', 'set', '-ti', str(baseline), 'compression', 'zstd')
    assert backend.calls.index(compression) < backend.calls.index(captures[0])
    assert backend.flags[baseline] is True
    assert not list((backend.manager.volume / 'runs').iterdir())
    assert backend.source.read_bytes() == donor


def test_acquisition_reuses_the_frozen_baseline(backend):
    metadata = backend.manager.prepare(backend.source)
    backend.calls.clear()
    with backend.manager.workspace(baseline=metadata['id'], report=backend.root / 'report') as run:
        (run['path'] / 'state.db').write_bytes(b'private mutation')
        assert (backend.manager.volume / 'baselines' / metadata['id'] / 'state.db').read_bytes() == backend.source.read_bytes()
    assert not [call for call in backend.calls if call[:2] == ('podman', 'run')]
    assert not list((backend.manager.volume / 'workspaces').iterdir())
    assert (backend.root / 'report/workspace.json').is_file()


def test_refresh_deletes_the_previous_baseline_before_copying(backend):
    original = backend.manager.prepare(backend.source)
    previous = backend.manager.volume / 'baselines' / original['id']
    backend.calls.clear()
    backend.failures.add('capture')
    with pytest.raises(RuntimeError, match='capture failure'):
        backend.manager.prepare(backend.source, refresh=True)
    deletion = backend.calls.index(('btrfs', 'subvolume', 'delete', str(previous)))
    capture = next(index for index, call in enumerate(backend.calls) if call[:2] == ('podman', 'run'))
    assert deletion < capture
    with pytest.raises(ValueError, match='No prepared baseline'):
        backend.manager.resolve(source=backend.source)
    assert not list((backend.manager.volume / 'baselines').iterdir())
    assert not list((backend.manager.volume / 'runs').iterdir())


def test_failed_partial_acquisition_removes_workspace_and_receipt(backend):
    metadata = backend.manager.prepare(backend.source)
    backend.failures.add('snapshot')
    with pytest.raises(RuntimeError, match='partial snapshot'):
        with backend.manager.workspace(baseline=metadata['id'], report=backend.root / 'report'):
            pytest.fail('yielded a failed snapshot')
    assert not list((backend.manager.volume / 'runs').iterdir())
    assert not list((backend.manager.volume / 'workspaces').iterdir())


def test_refresh_deletes_the_baseline_while_a_private_snapshot_is_in_use(backend):
    first = backend.manager.prepare(backend.source)
    with backend.manager.workspace(baseline=first['id'], report=backend.root / 'active') as active:
        latest = backend.manager.prepare(backend.source, refresh=True)
        assert (active['path'] / 'state.db').read_bytes() == backend.source.read_bytes()
        assert not (backend.manager.volume / 'baselines' / first['id']).exists()
    assert latest['id'] != first['id']
    assert backend.manager.resolve(source=backend.source)['id'] == latest['id']


def test_refresh_keeps_other_sources(backend):
    other = backend.source.with_name('other.db')
    write_schema(other, 124)
    separate = backend.manager.prepare(other)
    backend.manager.prepare(backend.source)
    backend.manager.prepare(backend.source, refresh=True)
    assert backend.manager.resolve(source=other)['id'] == separate['id']


def test_refresh_allows_btrfs_to_reclaim_the_deleted_baseline(backend, monkeypatch):
    backend.manager.prepare(backend.source)
    backend.calls.clear()
    blocks = (backend.source.stat().st_size + 4095) // 4096
    availability = iter([0, blocks])
    monkeypatch.setattr(image.os, 'statvfs', lambda path: SimpleNamespace(f_bavail=next(availability), f_frsize=4096))
    backend.manager.prepare(backend.source, refresh=True)
    deletion = next(index for index, call in enumerate(backend.calls) if call[:3] == ('btrfs', 'subvolume', 'delete'))
    sync = next(index for index, call in enumerate(backend.calls) if call[:3] == ('btrfs', 'filesystem', 'sync'))
    capture = next(index for index, call in enumerate(backend.calls) if call[:2] == ('podman', 'run'))
    assert deletion < sync < capture


def test_gc_previews_orphan_container_and_never_stops_live_owner(backend):
    baseline = backend.manager.prepare(backend.source)
    with backend.manager.workspace(baseline=baseline['id'], report=backend.root / 'live') as active:
        backend.containers.add(active['name'])
        assert backend.manager.collect(keep=1, delete=True) == []
        assert active['name'] in backend.containers
    identity = uuid.uuid4().hex
    target = backend.manager.volume / 'workspaces' / identity
    target.mkdir()
    backend.flags[target] = False
    owner = dict(image.process_identity(), pid=999999999)
    name = 'tuneconsole-library-run-' + identity
    backend.containers.add(name)
    image.save(backend.manager.volume / 'runs' / (identity + '.json'),
               dict(id=identity, baseline=baseline['id'], container=name, kind='workspace', owner=owner))
    assert backend.manager.collect(delete=False) == ['container:' + name]
    assert name in backend.containers and target.is_dir()
    assert backend.manager.collect(delete=True) == ['container:' + name, str(target)]
    assert name not in backend.containers and not target.exists()


def test_runtime_mounts_hide_other_workspaces_and_only_preparation_gets_donor(backend):
    normal = backend.manager.container_args(backend.root / 'run', backend.root / 'report', 'name')
    assert str(backend.source.parent) + ':' + str(backend.source.parent) + ':ro' not in normal
    assert '/worktree/build:rw,size=256m,mode=1777,nodev,nosuid,notmpcopyup' in normal
    capture = backend.manager.container_args(backend.root / 'run', backend.root / 'report', 'name', donor=backend.source)
    assert str(backend.source.parent) + ':' + str(backend.source.parent) + ':ro' in capture


def test_unmanaged_adapters_keep_ci_copy_behavior_without_image_tools(monkeypatch):
    from tests.library_image.fixtures import managed_database
    monkeypatch.delenv('TUNECONSOLE_IMAGE_RUN', raising=False)
    monkeypatch.setenv('TUNECONSOLE_TEST_LIBRARY', '/does/not/exist')
    assert managed_database() is None


def test_generation_identity_rejects_path_escape(backend):
    with pytest.raises(ValueError, match='identity'):
        backend.manager.metadata('../native.db')


def test_select_generation_by_installed_regular_schema(backend):
    manager = backend.manager
    old = manager.prepare(backend.source)
    first, second = (backend.source.with_name(name) for name in ('first.db', 'second.db'))
    write_schema(first, 107, regular=174)
    write_schema(second, 107, regular=175)
    c174, c175 = manager.prepare(first), manager.prepare(second)

    assert {item['schema']: (item['schema_migrations'], item['schema_changes'])
            for item in manager.generations()} == {123: (123, None), 174: (107, 174), 175: (107, 175)}
    assert manager.resolve(schema=123)['id'] == old['id']
    assert manager.resolve(schema=174)['id'] == c174['id']
    assert manager.resolve(source=second, schema=175)['id'] == c175['id']
    with pytest.raises(ValueError, match='No prepared generation'):
        manager.resolve(source=first, schema=175)
    with pytest.raises(ValueError, match='different schema'):
        manager.resolve(baseline=c175['id'], schema=174)


def test_old_capture_reports_original_schema_after_private_run_migrates(backend, monkeypatch):
    from tests.library_image import fixtures

    manager = backend.manager
    source = backend.source.with_name('older-capture.db')
    write_schema(source, 107, regular=134)
    generation = manager.prepare(source)['id']
    base = manager.volume / 'baselines' / generation
    receipt = manager.volume / 'metadata' / (generation + '.json')
    marker = base / '.baseline.json'
    for path in (receipt, marker):
        original = json.loads(path.read_text())
        original['schema'] = original.pop('schema_migrations')
        del original['schema_changes']
        path.write_text(json.dumps(original))
    frozen = (receipt, marker, base / 'state.db')
    before = [path.read_bytes() for path in frozen]

    assert manager.generations()[0]['schema'] == 134
    with manager.workspace(schema=134, report=backend.root / 'old') as run:
        copied = run['path'] / 'state.db'
        with closing(sqlite3.connect(copied)) as connection, connection:
            connection.execute('INSERT INTO schema_changes VALUES (175)')
        monkeypatch.setenv('TUNECONSOLE_IMAGE_RUN', '1')
        monkeypatch.setenv('TUNECONSOLE_TEST_LIBRARY', str(copied))
        monkeypatch.setattr(fixtures, 'report_file', lambda path: run['report'] / path)
        report = {}
        assert fixtures.managed_database(report=report) == copied
        assert report['managed_library']['id'] == generation
        assert report['source_version'] == 134
        assert report['source_schema'] == dict(schema=134, schema_migrations=107, schema_changes=134)
    assert [path.read_bytes() for path in frozen] == before


@pytest.fixture
def capture_paths(tmp_path):
    source, target = tmp_path / 'source.db', tmp_path / 'candidate'
    target.mkdir()
    with sqlite3.connect(source) as connection:
        connection.execute('CREATE TABLE schema_migrations(version INTEGER)')
        connection.execute('INSERT INTO schema_migrations VALUES (123)')
    return source, target


def test_capture_ignores_live_file_locks_and_missing_files(capture_paths):
    from tests.library_image.capture import capture
    from yt_playlist.core import fao, playback_inbox_file, wiki_cache
    from yt_playlist.library.playback_inbox_composition import inbox_for_database
    source, target = capture_paths
    queue = inbox_for_database(source, 'controlled')
    identity = queue.capture('player', 1, {'videoId': 'original'}, 1)
    evidence = source.with_suffix('.db.evidence')
    evidence.mkdir()
    (evidence / '.budget').write_text('0')
    notes = source.with_suffix('.db.notes')
    notes.mkdir()
    note = notes / 'present.json'
    note.write_text('present')
    missing = notes / 'absent.json'
    with sqlite3.connect(source) as connection:
        connection.execute('CREATE TABLE notes(filename TEXT)')
        connection.executemany('INSERT INTO notes VALUES (?)', [(str(note),), (str(missing),)])
        connection.execute("CREATE TRIGGER immutable_note BEFORE UPDATE ON notes "
                           "BEGIN SELECT RAISE(ABORT,'immutable'); END")
    before = source.read_bytes(), queue.path.read_bytes()
    with ExitStack() as stack:
        for lock in (notes / '.readers.lock', evidence / '.budget', queue.path.with_suffix('.lock')):
            handle = stack.enter_context(lock.open('a+b'))
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = capture(source, target)
    with sqlite3.connect(target / 'state.db') as connection:
        assert list(connection.execute('SELECT filename FROM notes ORDER BY rowid')) == [
            ('/library/state.db.notes/present.json',), ('/library/state.db.notes/absent.json',)]
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            connection.execute('UPDATE notes SET filename=?', ('replaced',))
    assert (target / 'state.db.notes/present.json').read_text() == 'present'
    copied = inbox_for_database(target / 'state.db', 'controlled')
    assert copied.next()['capture_id'] == identity
    assert copied.next()['body'] == {'videoId': 'original'}
    assert fao.read(copied.path).bytes == fao.read(queue.path).bytes
    assert before == (source.read_bytes(), queue.path.read_bytes())
    assert copied.path == playback_inbox_file.inbox_path(target / 'state.db')
    assert not wiki_cache.path_for(target / 'state.db').exists()
    assert not list(target.glob('*.sqlite3*'))
    assert report['playback_inbox_rows'] == 1
    assert report['skipped_files'] == 1


def test_capture_keeps_wikipedia_cards_for_offline_story_preparation(file_backed_store, tmp_path, monkeypatch):
    from tests.conftest import seed_catalog_track
    from tests.library_image.capture import capture
    from yt_playlist.controllers.story_preparation import StoryPreparation
    from yt_playlist.core import wiki_cache
    from yt_playlist.core.store import Store
    from yt_playlist.rec import into_recently

    source = file_backed_store
    now = 1_800_000_000
    subject = {'kind': 'artist', 'subject': 'artist:Band', 'display': 'Band', 'recording_genres': []}
    summary = {'title': 'Band', 'extract': 'A complete story about the band.',
               'thumbnail': 'http://portrait', 'url': 'http://wiki/Band', 'matched_genres': []}
    seed_catalog_track(source, 'song', 'Song', 'Band', video_id='song')
    source.wiki.put(subject['subject'], subject['kind'], subject['display'], summary, now)
    source_cache = wiki_cache.path_for(source._db_path)
    target = tmp_path / 'captured'
    target.mkdir()
    capture(source._db_path, target)
    database = target / 'state.db'
    private_cache = wiki_cache.path_for(database)
    assert private_cache.read_bytes() == source_cache.read_bytes()
    source_cache.unlink()

    monkeypatch.setattr(StoryPreparation, 'ranked', lambda *_: [subject])
    private = Store(database)
    with closing(private.conn):
        private.init_schema()
        assert private.rec_proposals.get_proposals(into_recently.SURFACE) is None
        assert not into_recently.prepare(private, now, fetch_fn=lambda *_: pytest.fail('Fetched a cached summary'))
        cards, pending = into_recently.load(private, now)
        assert not pending
        assert [(card['subject'], card['extract']) for card in cards] == [(subject['subject'], summary['extract'])]


@pytest.mark.parametrize('publish_after_sql', [False, True], ids=['before-sql', 'after-sql'])
def test_capture_keeps_only_numeric_evidence_selected_by_sql(tmp_path, monkeypatch, publish_after_sql):
    from tests.library_image import capture
    from yt_playlist.core.evidence_ledger import EvidenceLedger
    from yt_playlist.core.store import Store

    source = tmp_path / 'source.db'
    target = tmp_path / 'candidate'
    target.mkdir()
    retained = {'context': 'retained by the capture'}
    concurrent = {'context': 'written around the SQL copy'}
    installed = {'context': 'stored by digest before SQL receipts'}

    store = Store(source)
    with closing(store.conn):
        store.init_schema()
        ledger = store.rec_ledger.evidence()
        digest_ledger = EvidenceLedger(ledger.directory, ledger.byte_budget)
        installed_digest = digest_ledger.artifact(installed)
        retained_digest = ledger.artifact(retained)
        if publish_after_sql:
            copy_database = capture.copy_database

            def copy_then_publish(donor, destination):
                copy_database(donor, destination)
                if donor == source:
                    ledger.artifact(concurrent)

            monkeypatch.setattr(capture, 'copy_database', copy_then_publish)
        else:
            ledger.artifact(concurrent)
        capture.capture(source, target)
        concurrent_digest = ledger.artifact(concurrent)

    snapshot = Store(target / 'state.db')
    with closing(snapshot.conn):
        snapshot.init_schema()
        ledger = snapshot.rec_ledger.evidence()
        assert ledger.get('artifact', installed_digest) == installed
        assert ledger.get('artifact', retained_digest) == retained
        assert ledger.get('artifact', concurrent_digest) == (None if publish_after_sql else concurrent)
        private = {'context': 'first write in the private snapshot'}
        assert ledger.get('artifact', ledger.artifact(private)) == private


@pytest.mark.parametrize('regular', [None, 174, 175])
def test_capture_reads_committed_wal_without_copying_it(capture_paths, regular):
    from tests.library_image.capture import capture

    source, target = capture_paths
    with closing(sqlite3.connect(source)) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        with writer:
            writer.execute('INSERT INTO schema_migrations VALUES (124)')
            if regular is not None:
                writer.execute('CREATE TABLE schema_changes(version INTEGER PRIMARY KEY)')
                writer.execute('INSERT INTO schema_changes VALUES (?)', (regular,))
        assert source.with_name(source.name + '-wal').stat().st_size > 0
        result = capture(source, target)
        assert result['schema'] == (regular if regular is not None else 124)
        assert result['schema_migrations'] == 124
        assert result['schema_changes'] == regular
    assert not (target / 'state.db-wal').exists()


@pytest.mark.parametrize('split_provenance', [True, False], ids=['split', 'folded'])
def test_capture_expires_superseded_model_provenance(capture_paths, split_provenance):
    from tests.library_image.capture import capture
    from yt_playlist.core import vibe_model_files, vibe_provenance
    from yt_playlist.core.migrations import prediction_storage

    source, target = capture_paths
    selected, superseded, head = ('a' * 64, 'b' * 64, 'c' * 64)
    rows = [(1, bytes.fromhex(selected)), (129, bytes.fromhex(superseded)), (257, None)]
    evidence = source.with_suffix('.db.evidence')
    evidence.mkdir()
    (evidence / '.budget').write_text('0')
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
        connection.execute('INSERT INTO settings VALUES (?,?)', (vibe_model_files.HEAD_PREFIX + 'test', head))
        connection.execute('CREATE TABLE recording_vibes(prediction_id INTEGER)')
        connection.execute('INSERT INTO recording_vibes VALUES (1),(NULL)')
        if split_provenance:
            connection.execute('CREATE TABLE predictions(id INTEGER PRIMARY KEY)')
            connection.execute('CREATE TABLE prediction_model_generations(prediction_id INTEGER,generation BLOB)')
            connection.execute('CREATE TABLE prediction_provenance(prediction_id INTEGER,state TEXT)')
            connection.executemany('INSERT INTO predictions VALUES (?)', [(identity,) for identity, _ in rows])
            connection.executemany('INSERT INTO prediction_model_generations VALUES (?,?)', rows[:2])
            connection.executemany("INSERT INTO prediction_provenance VALUES (?,'ready')",
                                   [(identity,) for identity, _ in rows])
            connection.execute(dict(vibe_provenance.guards())['prediction_provenance_expiry'])
            states = 'SELECT prediction_id,state FROM prediction_provenance ORDER BY prediction_id'
        else:
            connection.execute('CREATE TABLE predictions(id INTEGER PRIMARY KEY,model_generation BLOB, '
                               'provenance_state TEXT,provenance_file_id INTEGER,provenance_fingerprint BLOB, '
                               'provenance_bytes INTEGER)')
            connection.executemany("INSERT INTO predictions(id,model_generation,provenance_state) VALUES (?,?,'ready')",
                                   rows)
            guards = dict(prediction_storage._guards())
            connection.execute(guards['predictions_provenance_immutable'])
            connection.execute(guards['predictions_provenance_expiry'])
            states = 'SELECT id,provenance_state FROM predictions ORDER BY id'

    for generation in (selected, superseded, head):
        path = source.with_suffix('.db.vibe-models') / vibe_model_files.relative(generation)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(generation)
    for identity, _ in rows:
        path = source.with_suffix('.db.vibe-provenance') / vibe_provenance.relative(identity)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(identity))

    with closing(sqlite3.connect(source)) as connection:
        definitions = connection.execute('SELECT name,sql FROM sqlite_schema ORDER BY name').fetchall()
        before = connection.execute(states).fetchall()
    report = capture(source, target)
    assert report['expired_vibe_predictions'] == 1
    assert report['skipped_files'] == 0
    with closing(sqlite3.connect(source)) as connection:
        assert connection.execute(states).fetchall() == before
    with closing(sqlite3.connect(target / 'state.db')) as connection:
        assert connection.execute(states).fetchall() == [(1, 'ready'), (129, 'expired'), (257, 'ready')]
        assert connection.execute('SELECT name,sql FROM sqlite_schema ORDER BY name').fetchall() == definitions
        assert connection.execute('SELECT prediction_id FROM recording_vibes').fetchall() == [(1,), (None,)]
    copied_models = target / 'state.db.vibe-models'
    assert {path.stem for path in copied_models.rglob('*.json')} == {selected, head}
    copied_provenance = target / 'state.db.vibe-provenance'
    assert {path.relative_to(copied_provenance) for path in copied_provenance.rglob('*.json')} == {
        vibe_provenance.relative(1), vibe_provenance.relative(257)}


@pytest.mark.parametrize('installed_layout', [True, False], ids=['before-c121', 'after-c121'])
def test_capture_copies_runtime_metadata_from_installed_layout(capture_paths, installed_layout):
    import time

    from tests.library_image.capture import capture
    from yt_playlist.core import metadata_file_contract, metadata_runtime_files

    source, target = capture_paths
    fetched = time.time()
    root = metadata_runtime_files.root_for_database(source, None)
    references = [
        ('alternatives', (5, 7), fetched), ('native', ('controlled-native',), 4),
        ('choices', (12,), 1), ('choices', (12,), 2), ('conflicts', (9,), 3),
    ]
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.executescript('''
            CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE metadata_runtime_state(id INTEGER,active INTEGER);
            INSERT INTO metadata_runtime_state VALUES (1,1);
            CREATE TABLE enrichment_provider_state(recording_id INTEGER,provider TEXT,at REAL,status TEXT);
            CREATE TABLE rec_musical_sources(source_key TEXT,revision INTEGER);
            INSERT INTO rec_musical_sources VALUES ('controlled-native',4);
            CREATE TABLE metadata_runtime_choices(id INTEGER,revision INTEGER);
            INSERT INTO metadata_runtime_choices VALUES (12,2);
            CREATE TABLE metadata_file_references(kind TEXT,owner_key TEXT,revision_key TEXT,file_id INTEGER);
        ''')
        connection.execute('INSERT INTO enrichment_provider_state VALUES (5,?,?,?)', ('provider', fetched, 'done'))
        if installed_layout:
            connection.executescript('''
                CREATE TABLE metadata_runtime_providers(id INTEGER,name TEXT);
                INSERT INTO metadata_runtime_providers VALUES (7,'provider');
                CREATE TABLE metadata_runtime_conflicts(id INTEGER,revision INTEGER);
                INSERT INTO metadata_runtime_conflicts VALUES (9,3);
            ''')
        else:
            connection.executescript('''
                CREATE TABLE enrichment_provider_config(provider TEXT,metadata_provider_id INTEGER);
                INSERT INTO enrichment_provider_config VALUES ('provider',7);
                CREATE TABLE metadata_conflicts(runtime_id INTEGER,runtime_revision INTEGER);
                INSERT INTO metadata_conflicts VALUES (9,3);
            ''')
        for identity, (kind, owner, revision) in enumerate(references, start=1):
            connection.execute('INSERT INTO metadata_file_references VALUES (?,?,?,?)', (
                kind, metadata_file_contract.owner_key(kind, owner),
                metadata_file_contract.revision_key(revision), identity))
            path = metadata_file_contract.record_path(root, identity)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(kind)
    capture(source, target)
    copied = metadata_runtime_files.root_for_database(target / 'state.db', None)
    for identity, (kind, _, _) in enumerate(references, start=1):
        assert metadata_file_contract.record_path(copied, identity).read_text() == kind


def test_capture_reads_closed_wal_database_from_readonly_directory(capture_paths):
    from tests.library_image.capture import capture

    source, target = capture_paths
    donor = source.parent / 'donor'
    donor.mkdir()
    source = source.rename(donor / source.name)
    with closing(sqlite3.connect(source)) as writer:
        assert writer.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        with writer:
            writer.execute('INSERT INTO schema_migrations VALUES (124)')
    assert list(donor.iterdir()) == [source]
    before = source.stat().st_mtime_ns
    donor.chmod(0o555)
    try:
        assert capture(source, target)['schema'] == 124
        assert list(donor.iterdir()) == [source]
        assert source.stat().st_mtime_ns == before
    finally:
        donor.chmod(0o755)


def test_playback_inbox_snapshot_reads_the_last_published_generation(capture_paths, monkeypatch):
    from tests.library_image.capture import capture
    from yt_playlist.core import fao
    from yt_playlist.library.playback_inbox_composition import inbox_for_database
    source, target = capture_paths
    queue = inbox_for_database(source, 'controlled')
    identity = queue.capture('player', 1, {'videoId': 'original'}, 1)

    def interrupted(*args):
        raise OSError('interrupted capture')

    with monkeypatch.context() as patch:
        patch.setattr(fao._PreparedFile, 'publish', interrupted)
        with pytest.raises(OSError, match='interrupted capture'):
            queue.capture('player', 1, {'videoId': 'interrupted'}, 2)
    before = queue.path.read_bytes()
    report = capture(source, target)
    copied = inbox_for_database(target / 'state.db', 'controlled')
    assert report['playback_inbox_rows'] == copied.pending() == 1
    assert copied.next()['capture_id'] == identity
    assert copied.next()['body'] == {'videoId': 'original'}
    copied.acknowledge(identity)
    assert queue.path.read_bytes() == before
