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
