"""Historical SQL reuse preserves real layouts, admission and private writes."""
from contextlib import ExitStack, closing
import sqlite3

import pytest

from yt_playlist.core import file_owners, schema_inspection
from yt_playlist.core.migrations import registry, runner
from yt_playlist.core.store import Store


# Account renaming and original-custody takeover have distinct real owners.
# Reuse their completed SQL when constructing the final independent replay.
VERSIONS = (69, 79, 82, 83, 93, 97, 102, 103, 104, 107)


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Schema and clone checks do not construct an HTTP application."""


def bind_consumer(store, version):
    if version < 79:
        # These predecessors need explicit later migrations. Match their
        # historical fixture binding without ordinary startup admission.
        store._bind_repositories()
        store.conn.execute('PRAGMA journal_mode=WAL')
    else:
        with schema_inspection.reuse_tables(store.conn):
            store.init_schema(defer_metadata=True)


@pytest.fixture(scope='module')
def genuine_replays():
    """Independent real replay, with each immutable predecessor owned below."""
    inputs = {}
    states = {}
    with ExitStack() as resources:
        def get(version):
            if version not in inputs:
                conn = resources.enter_context(closing(sqlite3.connect(':memory:')))
                conn.row_factory = sqlite3.Row
                conn.execute('PRAGMA foreign_keys=ON')
                previous = max((old for old in inputs if old < version), default=None)
                if previous is not None:
                    inputs[previous].backup(conn)
                runner.migrate(conn, registry=registry.MIGRATIONS[:version])
                conn.execute('PRAGMA query_only=ON')
                inputs[version] = conn
                states[version] = state(conn)
            return inputs[version]

        get.state = lambda version: states[version]
        get.release = lambda version: inputs[version].close()
        yield get


@pytest.fixture(scope='module')
def admitted_replays(genuine_replays, tmp_path_factory):
    root = tmp_path_factory.mktemp('genuine-admitted-replays')
    inputs = {}
    states = {}
    with ExitStack() as resources:
        def get(version):
            if version not in inputs:
                store = Store(root / f'{version}.db')
                resources.callback(store.conn.close)
                genuine_replays(version).backup(store.conn)
                bind_consumer(store, version)
                store.conn.execute('PRAGMA query_only=ON')
                inputs[version] = store
                states[version] = state(store.conn)
            return inputs[version]

        get.state = lambda version: states[version]
        get.release = lambda version: inputs[version].conn.close()
        yield get


def state(conn):
    result = {}
    for name, kind, sql in conn.execute("SELECT name,type,sql FROM sqlite_schema"):
        if kind != 'table':
            result[name] = kind, sql
            continue
        table = '"' + name.replace('"', '""') + '"'
        cursor = conn.execute('SELECT * FROM ' + table)
        columns = [column[0] for column in cursor.description]
        retained = [i for i, column in enumerate(columns)
                    if column != 'applied_at' or name not in ('schema_migrations', 'schema_changes')]
        result[name] = kind, sql, sorted((tuple(row[i] for i in retained) for row in cursor), key=repr)
    return result


@pytest.mark.parametrize('version', [79, 93, 102, 107])
def test_bounded_publication_input_matches_real_admission_and_preserves_private_guards(
        _bounded_installed_schema, tmp_path, monkeypatch, version):
    from tests.genre_catalog_support import bound_catalog
    from tests.store_fixture_support import install_numbered_input, revision_definitions
    from yt_playlist.core.migrations import regular

    bound_catalog(monkeypatch)
    with ExitStack() as resources:
        admitted = Store(tmp_path / 'genuinely-admitted.db')
        resources.callback(admitted.conn.close)
        # Match the previous builder's input, then let actual startup determine
        # its SQL, compatibility flags and guards. Full historical replay has
        # separate owners above; shipped assembled DDL can differ textually.
        with closing(sqlite3.connect(':memory:')) as numbered:
            install_numbered_input(numbered, version)
            numbered.backup(admitted.conn)
        admitted.init_schema(defer_metadata=True)
        template = _bounded_installed_schema(version)
        assert state(template.source.conn) == state(admitted.conn)
        assert template.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
        flags = ('_schema_compatibility', '_rebuild_source_version', '_schema_recovery_pending')
        assert [getattr(template.source, name) for name in flags] == [
            getattr(admitted, name) for name in flags]
        # Empty numbered input must not claim that pending regular work ran.
        assert regular.validate(template.source.conn) == regular.validate(admitted.conn) == 0
        first = resources.enter_context(template.open(tmp_path / 'first.db'))
        second = resources.enter_context(template.open(tmp_path / 'second.db'))
        guards = tuple(map(tuple, revision_definitions(admitted.conn)))
        for clone in (first, second):
            assert state(clone.conn) == state(admitted.conn)
            assert tuple(map(tuple, revision_definitions(clone.conn))) == guards
            assert clone.conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
            assert clone.conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
            assert not clone.conn.execute('PRAGMA foreign_key_check').fetchone()
        first.settings.set_setting('private-input', 'original')
        with first.read_snapshot() as snapshot:
            assert snapshot.store.settings.get_setting('private-input') == 'original'
            first.settings.set_setting('private-input', 'changed')
            assert snapshot.store.settings.get_setting('private-input') == 'original'
            assert not snapshot.publish(lambda: pytest.fail('Published changed input'))
        with first.read_snapshot() as snapshot:
            assert snapshot.store.settings.get_setting('private-input') == 'changed'
            with pytest.raises(RuntimeError, match='rollback input'):
                with first.conn:
                    first.conn.execute("UPDATE settings SET value='rolled back' WHERE key='private-input'")
                    raise RuntimeError('rollback input')
            assert snapshot.publish(lambda: None)
        assert second.settings.get_setting('private-input') is None
        assert template.source.settings.get_setting('private-input') is None


@pytest.mark.parametrize('version', [20, 92])
def test_genuine_history_ignores_and_restores_a_consumers_catalog(
        _pristine_installed_schema, genuine_replays, version):
    from tests.genre_catalog_support import bound_catalog
    from yt_playlist.util import genre_catalog, genre_map

    genuine_replays(version)
    expected = genuine_replays.state(version)
    with pytest.MonkeyPatch.context() as patch:
        bound_catalog(patch, entries=(('fixture only', 'Fixture Only'),))
        assert genre_map.family('Fixture Only') == 'fixture only'
        private_families = genre_map._load()
        source = _pristine_installed_schema(version)
        assert state(source) == expected
        # Building the shared source must also restore the consumer's private
        # vocabulary and its derived cache, rather than exposing shipped state.
        assert set(genre_catalog.catalog()[0]) == {'fixture only'}
        assert genre_map.family('Fixture Only') == 'fixture only'
        assert genre_map._load() is private_families
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            source.execute("INSERT INTO settings VALUES ('private','forbidden')")


@pytest.mark.parametrize(('version', 'chain'), [(version, chain) for version in VERSIONS
                                             for chain in ('genuine', 'fixture')])
def test_installed_sql_input_matches_independent_real_replay(
        _pristine_installed_schema, genuine_replays, version, chain):
    replay = genuine_replays(version) if chain == 'genuine' else _pristine_installed_schema(version)
    if chain == 'fixture':
        assert state(replay) == genuine_replays.state(version)
    assert replay.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == version
    assert not replay.execute('PRAGMA foreign_key_check').fetchone()
    with pytest.raises(sqlite3.OperationalError, match='readonly'):
        replay.execute("INSERT INTO settings VALUES ('private','forbidden')")


@pytest.mark.parametrize('version', VERSIONS)
def test_genuine_replay_input_keeps_real_admission_and_read_only_ownership(admitted_replays, version):
    fresh = admitted_replays(version)
    assert fresh.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == version
    assert fresh.conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    assert fresh.conn.execute('PRAGMA journal_mode').fetchone()[0].lower() == 'wal'
    assert not fresh.conn.execute('PRAGMA foreign_key_check').fetchone()
    with pytest.raises(sqlite3.OperationalError, match='readonly'):
        fresh.conn.execute("INSERT INTO settings VALUES ('private','forbidden')")


@pytest.mark.parametrize('version', [69, 82, 83, 79, 93, 102, 107, 97])
def test_installed_template_matches_genuine_replay_and_keeps_admitted_clones_private(
        _pristine_installed_schema, genuine_replays, admitted_replays, tmp_path, version):
    with ExitStack() as resources:
        genuine_replays(version)
        fresh = admitted_replays(version)
        template = _pristine_installed_schema(version)
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            template.execute("INSERT INTO settings VALUES ('private','forbidden')")
        original = list(template.iterdump())
        clones = []
        for name in ('first', 'second'):
            path = tmp_path / (name + '.db')
            with closing(sqlite3.connect(path)) as writer:
                template.backup(writer)
                assert state(writer) == genuine_replays.state(version)
            clone = Store(path)
            resources.callback(clone.conn.close)
            bind_consumer(clone, version)
            clones.append(clone)
        assert state(clones[0].conn) == state(clones[1].conn) == admitted_replays.state(version)
        assert len({clone._evidence_key for clone in (*clones, fresh)}) == 3
        for module in file_owners.owners().values():
            assert len({module.directory(clone.conn) for clone in (*clones, fresh)}) == 3
        for clone in clones:
            assert clone.conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
            assert clone.conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
            assert clone.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == version
            assert not clone.conn.execute('PRAGMA foreign_key_check').fetchall()
        clones[0].tracks.upsert_track('private', 'Private', 'Artist', None, 10)
        clones[0].settings.set_setting('private', 'first')
        assert clones[1].tracks.track_ids_for_videos(['private']) == {}
        assert clones[1].settings.get_setting('private') is None
        assert list(template.iterdump()) == original


@pytest.mark.parametrize('version', VERSIONS)
def test_installed_replay_inputs_release_their_connections(genuine_replays, admitted_replays, version):
    fresh = admitted_replays(version)
    replay = genuine_replays(version)
    admitted_replays.release(version)
    genuine_replays.release(version)
    for conn in (fresh.conn, replay):
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            conn.execute('SELECT 1')
