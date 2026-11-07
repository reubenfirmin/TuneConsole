"""Historical SQL reuse preserves real layouts, admission and private writes."""
from contextlib import ExitStack, closing
import sqlite3

import pytest

from yt_playlist.core import file_owners
from yt_playlist.core.migrations import registry, runner
from yt_playlist.core.store import Store


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


@pytest.mark.parametrize('version', [69, 82, 83, 79, 93, 102, 107, 97])
def test_installed_template_matches_genuine_replay_and_keeps_admitted_clones_private(
        _pristine_installed_schema, tmp_path, version):
    def bind_consumer(store):
        if version < 79:
            # These predecessors need explicit later migrations. Match their
            # historical fixture binding without ordinary startup admission.
            store._bind_repositories()
            store.conn.execute('PRAGMA journal_mode=WAL')
        else:
            store.init_schema(defer_metadata=True)

    with ExitStack() as resources:
        fresh = Store(tmp_path / 'fresh.db')
        resources.callback(fresh.conn.close)
        runner.migrate(fresh.conn, registry=registry.MIGRATIONS[:version])
        template = _pristine_installed_schema(version)
        assert state(template) == state(fresh.conn)
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            template.execute("INSERT INTO settings VALUES ('private','forbidden')")
        original = list(template.iterdump())
        clones = []
        for name in ('first', 'second'):
            path = tmp_path / (name + '.db')
            with closing(sqlite3.connect(path)) as writer:
                template.backup(writer)
                assert state(writer) == state(fresh.conn)
            clone = Store(path)
            resources.callback(clone.conn.close)
            bind_consumer(clone)
            clones.append(clone)
        bind_consumer(fresh)
        assert state(clones[0].conn) == state(clones[1].conn) == state(fresh.conn)
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
