"""Recovered preparation owns every retained range without its producer's files."""
from contextlib import ExitStack, closing
import hashlib
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_metadata_destinations import stage
from tests.core.test_metadata_owner_cutover import seed
from tests.core.test_metadata_preparation import finish
from tests.core.test_metadata_publication import publish as read_publication
from tests import file_custody_support as custody
from tests.prepared_fixture_support import capture_template, roots
from tests.store_fixture_support import install_numbered_input, reuse_reporting_reader
from yt_playlist.core import metadata_delivery_files as files, metadata_destination_files as recovery
from yt_playlist.core import policy_documents, schema_inspection
from yt_playlist.core.migrations import migrate, registry
from yt_playlist.core.preparation import metadata_destinations as destinations, metadata_evidence
from yt_playlist.core.store import Store


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """File recovery does not construct the HTTP lifecycle."""


def prepared(store, *, unfinished=False):
    entry, recording, key = seed(store)
    metadata_evidence.JOB.install(store.conn)
    if unfinished:
        finish(store.conn)
        identity = read_publication(store.conn)
        assert not destinations.step(store.conn, identity=identity, batch_size=2).complete
    else:
        identity = stage(store.conn)
        destinations.publish(store.conn, identity=identity)
    return entry, recording, key, identity


@pytest.fixture(scope='module')
def destination_templates(tmp_path_factory):
    from tests.genre_catalog_support import bound_catalog

    root = tmp_path_factory.mktemp('metadata-destination-templates')
    templates = {}
    with ExitStack() as resources:
        patch = resources.enter_context(pytest.MonkeyPatch.context())
        bound_catalog(patch, entries=(('rock', 'Rock'), ('jazz', 'Jazz'), ('blues', 'Blues')))

        def template(kind):
            if kind not in templates:
                directory = root / kind
                directory.mkdir()
                source = Store(directory / 'source.db')
                resources.callback(source.conn.close)
                source.conn.execute('PRAGMA synchronous=NORMAL')
                if kind == 'native-precedence':
                    result = template('selected').restore(source)
                    migrate(source.conn, registry=registry.MIGRATIONS)
                else:
                    install_numbered_input(source.conn, 79)
                    source.conn.execute('PRAGMA journal_mode=WAL')
                    source._bind_repositories()
                    with reuse_reporting_reader(source, patch), schema_inspection.reuse_tables(source.conn):
                        result = prepared(source, unfinished=kind == 'unfinished')
                source._bind_repositories()
                templates[kind] = capture_template(source, result)
            return templates[kind]
        yield template


@pytest.fixture
def prepared_source(destination_templates, tmp_path):
    sources = []
    with ExitStack() as resources:
        def restore(kind='selected'):
            directory = tmp_path / f'{kind}-{len(sources)}'
            directory.mkdir()
            source = Store(directory / 'source.db')
            sources.append(source)
            resources.callback(source.conn.close)
            result = destination_templates(kind).restore(source)
            source._bind_repositories()
            return source, result
        yield restore


def contents(conn):
    return {row[0]: (row[2], row[3], files.read(*row[1:])) for row in recovery.references(conn)}


def hashes(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.glob('*/*.json')}


@pytest.mark.parametrize('kind', ['selected', 'unfinished', 'native-precedence'])
@pytest.mark.parametrize('original', [False, True], ids=['restored', 'snapshot'])
def test_recovered_ranges_reads_and_continuation_survive_missing_source(prepared_source, tmp_path, kind, original, monkeypatch):
    producer, (_, _, key, identity) = prepared_source(kind)
    expected = producer.enrichment_state.metadata_observations(key)
    sources = {row[0]: producer.musical_profiles.source_input(row[0]) for row in producer.conn.execute(
        'SELECT source_key FROM rec_musical_sources')}
    expected_ranges = contents(producer.conn)
    assert expected and expected_ranges
    source_root = recovery.directory(producer.conn)
    before_files, before_sql = hashes(source_root), list(producer.conn.iterdump())
    history = [tuple(row) for row in producer.conn.execute('SELECT * FROM schema_migrations ORDER BY version')]
    report = custody.snapshot_library(producer._db_path, tmp_path / 'recovered')
    assert hashes(source_root) == before_files
    assert list(producer.conn.iterdump()) == before_sql
    assert 'metadata_preparation' in report['manifests']
    with closing(sqlite3.connect(report['snapshot'])) as backup:
        assert list(backup.iterdump()) == before_sql
        if original:
            database = tmp_path / 'restored.db'
            with closing(sqlite3.connect(database)) as target:
                backup.backup(target)
        else:
            database = Path(report['restored'])
    candidate = Store(database)
    hidden = source_root.with_name(source_root.name + '-unavailable')
    source_root.rename(hidden)
    try:
        if original:
            custody.restore_files(candidate.conn, report['manifests'])
        candidate.init_schema(defer_metadata=True)
        assert contents(candidate.conn) == expected_ranges
        assert [tuple(row) for row in candidate.conn.execute(
            'SELECT * FROM schema_migrations ORDER BY version')] == history
        assert all(Path(row[1]).is_relative_to(recovery.directory(candidate.conn))
                   for row in recovery.references(candidate.conn))
        destinations.validate_installed(candidate.conn)
        if kind == 'native-precedence':
            def wrong_owner(*args, **kwargs):
                pytest.fail('Native metadata reader fell back to prepared delivery owner')
            monkeypatch.setattr(destinations, 'native_rows', wrong_owner)
        assert candidate.enrichment_state.metadata_observations(key) == expected
        assert {source: candidate.musical_profiles.source_input(source) for source in sources} == sources
        if kind == 'unfinished':
            assert destinations.selected(candidate.conn) is None
            for _ in range(100):
                if destinations.step(candidate.conn, identity=identity, batch_size=2).complete:
                    break
            else:
                pytest.fail('Restored metadata preparation could not resume')
            destinations.publish(candidate.conn, identity=identity)
        if kind != 'native-precedence':
            assert destinations.selected(candidate.conn) == identity
        assert candidate.enrichment_state.metadata_observations(key) == expected
        assert not candidate.conn.execute('PRAGMA foreign_key_check').fetchone()
        with pytest.raises(sqlite3.IntegrityError, match='retained'):
            with candidate.conn:
                candidate.conn.execute('UPDATE ' + destinations.table('deliveries') + " SET provider='changed'")
    finally:
        candidate.conn.close()
        hidden.rename(source_root)
    assert hashes(source_root) == before_files
    assert list(producer.conn.iterdump()) == before_sql


@pytest.mark.parametrize('damage', ['missing-source', 'interrupted-export', 'interrupted-restore'])
def test_failed_recovery_never_reports_success_or_changes_source(prepared_source, tmp_path, monkeypatch, damage):
    producer, _ = prepared_source()
    conn = producer.conn
    before = list(conn.iterdump())
    root = recovery.directory(conn)
    before_files = hashes(root)
    hidden = None
    if damage == 'missing-source':
        source = Path(next(iter(recovery.references(conn)))[1])
        hidden = source.with_suffix('.unavailable')
        source.rename(hidden)
    else:
        def fail(*args, **kwargs):
            raise OSError('controlled metadata preparation interruption')
        monkeypatch.setattr(recovery, 'export' if damage == 'interrupted-export' else 'restore', fail)
    output = tmp_path / 'failed-recovery'
    try:
        with pytest.raises(OSError):
            custody.snapshot_library(producer._db_path, output)
        assert list(conn.iterdump()) == before
    finally:
        if hidden is not None:
            hidden.rename(hidden.with_suffix('.json'))
    assert hashes(root) == before_files


def test_restore_rollback_restores_guard_and_all_original_references(prepared_source, tmp_path, monkeypatch):
    producer, _ = prepared_source()
    conn = producer.conn
    with policy_documents.retained_reader(recovery.directory(conn)):
        conn.execute('BEGIN')
        try:
            manifest = recovery.export(conn, tmp_path / 'backup')
        finally:
            conn.rollback()
    before = list(conn.iterdump())
    guard = destinations.table('deliveries_update')
    actual_check, checks = recovery.check_cancellable_work, []
    def interrupt_after_one_update():
        actual_check()
        if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (guard,)).fetchone():
            checks.append(True)
            if len(checks) == 2:
                raise OSError('interrupted after first owner relocation')
    monkeypatch.setattr(recovery, 'check_cancellable_work', interrupt_after_one_update)
    with pytest.raises(OSError, match='first owner relocation'):
        recovery.restore(conn, manifest)
    assert len(checks) == 2
    assert not conn.in_transaction
    assert list(conn.iterdump()) == before
    destinations.validate_installed(conn)
    monkeypatch.setattr(recovery, 'check_cancellable_work', actual_check)
    recovery.restore(conn, manifest)
    destinations.validate_installed(conn)
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('kind', ['selected', 'unfinished', 'native-precedence'])
def test_prepared_sources_keep_distinct_states_and_private_custody(destination_templates, prepared_source, kind):
    template = destination_templates(kind)
    first, result = prepared_source(kind)
    second, other_result = prepared_source(kind)
    assert result == other_result == template.result
    expected = contents(template.source.conn)
    before = list(template.source.conn.iterdump())
    history = [tuple(row) for row in template.source.conn.execute(
        'SELECT * FROM schema_migrations ORDER BY version')]
    assert (history[-1][0] > 79) is (kind == 'native-precedence')
    assert template.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError, match='readonly'):
        template.source.conn.execute("INSERT INTO settings VALUES ('private-template', 'forbidden')")
    template.source.conn.rollback()
    for consumer in (first, second):
        assert contents(consumer.conn) == expected
        assert [tuple(row) for row in consumer.conn.execute(
            'SELECT * FROM schema_migrations ORDER BY version')] == history
        # Full native upgrade withdraws the old prepared selection. An
        # unfinished stage also remains unselected, for a different reason.
        assert destinations.selected(consumer.conn) == (result[-1] if kind == 'selected' else None)
        assert not consumer.conn.execute('PRAGMA foreign_key_check').fetchone()
    owned = [roots(consumer.conn) for consumer in (first, second)]
    copied = 0
    for name, directory in template.files.items():
        assert len({directory, owned[0][name], owned[1][name]}) == 3, name
        for path in directory.rglob('*'):
            if not path.is_file():
                continue
            copies = [path, *(private[name] / path.relative_to(directory) for private in owned)]
            assert all(copy.read_bytes() == path.read_bytes() for copy in copies)
            assert len({(copy.stat().st_dev, copy.stat().st_ino) for copy in copies}) == 3
            copied += 1
    assert copied
    # Even removing the immutable preparation owner cannot make a clone borrow
    # its ranges. A later private file mutation also cannot reach another clone.
    directory = recovery.directory(template.source.conn)
    hidden = directory.with_name(directory.name + '-unavailable')
    directory.rename(hidden)
    try:
        assert contents(first.conn) == contents(second.conn) == expected
    finally:
        hidden.rename(directory)
    private_file = Path(next(iter(recovery.references(first.conn)))[1])
    private_file.write_bytes(b'private damaged retained range')
    first.settings.set_setting('private-fixture', 'first consumer')
    assert contents(second.conn) == contents(template.source.conn) == expected
    assert second.settings.get_setting('private-fixture') is None
    assert list(template.source.conn.iterdump()) == before
