"""Cached SQLite observations keep independent mutable containers."""
from contextlib import closing
import sqlite3

import pytest

from yt_playlist.core import schema_definition, schema_inspection


@pytest.mark.parametrize('definition', [
    'CREATE TABLE child(id INTEGER PRIMARY KEY AUTOINCREMENT, '
    'parent INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED, '
    'label TEXT COLLATE NOCASE UNIQUE CHECK(length(label)>0))',
    'CREATE TABLE child(id INTEGER PRIMARY KEY, label TEXT) WITHOUT ROWID, STRICT',
])
def test_cached_table_mappings_are_isolated_from_each_caller(definition, monkeypatch):
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.execute('CREATE TABLE parent(id INTEGER PRIMARY KEY)')
        conn.execute(definition)
        sql = conn.execute("SELECT sql FROM sqlite_schema WHERE name='child'").fetchone()[0]
        expected = schema_definition._inspect_table(conn, 'child', sql)
        inspected = []
        original = schema_definition._inspect_table

        def inspect(*args):
            inspected.append(args[1])
            return original(*args)

        monkeypatch.setattr(schema_definition, '_inspect_table', inspect)
        with schema_inspection.reuse_tables(conn) as observations:
            observations.refresh()
            for _ in range(3):
                actual = schema_definition._table(conn, 'child', sql)
                assert actual == expected
                for part in actual:
                    if isinstance(part, dict):
                        part.clear()
            assert inspected == ['child']
        # A new admission still observes the live database independently.
        assert schema_definition._table(conn, 'child', sql) == expected
        assert inspected == ['child', 'child']

        # The same DDL may be supplied as a new list or generator on each
        # validation. Reuse its reference construction while isolating every
        # returned contract and each table's mutable constraint containers.
        statements = ('CREATE TABLE parent(id INTEGER PRIMARY KEY)', definition)
        schema_definition._reference_contract.cache_clear()
        first = schema_definition._schema_contract(iter(statements))
        assert first['child'] == ('table', expected)
        for _ in range(3):
            copied = schema_definition._schema_contract(list(statements))
            assert copied == first
            for part in copied['child'][1]:
                if isinstance(part, dict):
                    part.clear()
            copied.clear()
        assert schema_definition._schema_contract(statements) == first
        info = schema_definition._reference_contract.cache_info()
        assert (info.misses, info.hits) == (1, 4)
        changed = schema_definition._schema_contract((statements[0], definition.replace(
            'label TEXT', 'label BLOB')))
        assert changed['child'] != first['child']
        assert schema_definition._reference_contract.cache_info().misses == 2


def test_nested_sessions_reuse_only_their_connection_and_restore_the_parent():
    definition = 'CREATE TABLE example(value TEXT UNIQUE)'
    with closing(sqlite3.connect(':memory:')) as first, closing(sqlite3.connect(':memory:')) as other:
        for conn in (first, other):
            conn.execute(definition)
        statements = []
        first.set_trace_callback(statements.append)
        with schema_inspection.reuse_tables(first) as outer:
            outer.refresh()
            expected = schema_definition._table(first, 'example', definition)
            with schema_inspection.reuse_tables(first) as nested:
                assert nested is outer
                actual = schema_definition._table(first, 'example', definition)
                actual[0].clear()
            with schema_inspection.reuse_tables(other) as independent:
                assert independent is not outer
                independent.refresh()
                assert schema_definition._table(other, 'example', definition) == expected
            assert schema_definition._table(first, 'example', definition) == expected
            assert statements.count('PRAGMA main.table_xinfo("example")') == 1
        with schema_inspection.reuse_tables(first) as later:
            assert later is not outer
            later.refresh()
            assert schema_definition._table(first, 'example', definition) == expected
        assert statements.count('PRAGMA main.table_xinfo("example")') == 2


def test_nested_session_refresh_observes_external_commits_and_own_ddl(tmp_path):
    definition = 'CREATE TABLE example(value TEXT UNIQUE)'
    with closing(sqlite3.connect(tmp_path / 'observations.db')) as conn:
        with conn:
            conn.execute(definition)
        statements = []
        conn.set_trace_callback(statements.append)
        with schema_inspection.reuse_tables(conn) as outer:
            outer.refresh()
            expected = schema_definition._table(conn, 'example', definition)
            with closing(sqlite3.connect(tmp_path / 'observations.db')) as other, other:
                other.execute("INSERT INTO example VALUES ('external commit')")
            with schema_inspection.reuse_tables(conn) as nested:
                assert nested is outer
                nested.refresh()
                assert schema_definition._table(conn, 'example', definition) == expected
                assert statements.count('PRAGMA main.table_xinfo("example")') == 2
                with conn:
                    conn.execute('ALTER TABLE example ADD COLUMN added INTEGER')
                changed = conn.execute("SELECT sql FROM sqlite_schema WHERE name='example'").fetchone()[0]
                actual = schema_definition._table(conn, 'example', changed)
                assert set(actual[0]) == {'value', 'added'}
                assert statements.count('PRAGMA main.table_xinfo("example")') == 3
            assert schema_definition._table(conn, 'example', changed) == actual
            assert statements.count('PRAGMA main.table_xinfo("example")') == 3


def test_reference_reuse_across_layouts_preserves_changed_constraints_and_live_observations(monkeypatch):
    parent = 'CREATE TABLE parent(id INTEGER PRIMARY KEY)'
    child = ('CREATE TABLE child(id INTEGER PRIMARY KEY, parent INTEGER REFERENCES parent(id) '
             'DEFERRABLE INITIALLY DEFERRED, label TEXT COLLATE NOCASE UNIQUE CHECK(length(label)>0))')
    schema_definition._reference_table.cache_clear()
    inspected = []
    connections = []
    inspect = schema_definition._inspect_table

    def observe(conn, name, sql):
        inspected.append(name)
        connections.append(conn)
        return inspect(conn, name, sql)

    monkeypatch.setattr(schema_definition, '_inspect_table', observe)
    first = schema_definition._schema_contract((parent, child))
    assert inspected == ['parent', 'child']
    assert connections[0] is connections[1]
    with pytest.raises(sqlite3.ProgrammingError, match='closed database'):
        connections[0].execute('SELECT 1')
    later = schema_definition._schema_contract((parent, child, 'CREATE TABLE later(value TEXT)'))
    assert first['child'] == later['child']
    assert inspected.count('child') == 1
    for part in later['child'][1]:
        if isinstance(part, dict):
            part.clear()
    assert schema_definition._schema_contract((parent, child)) == first
    changed = child.replace('INITIALLY DEFERRED', 'INITIALLY IMMEDIATE').replace('length(label)>0', 'length(label)>1')
    assert schema_definition._schema_contract((parent, changed))['child'] != first['child']
    assert inspected.count('child') == 2

    # Warming any reference cannot supply observations of an installed owner.
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.execute(parent)
        conn.execute(changed)
        assert schema_definition._table(conn, 'child', changed) != first['child'][1]
        assert inspected.count('child') == 3


def test_compiled_historical_layout_observes_current_source_text(tmp_path, monkeypatch):
    source = tmp_path / 'schema.sql'
    source.write_text('CREATE TABLE example(value TEXT);\n')
    monkeypatch.setattr(schema_definition, 'SOURCE_DEFINITIONS', (source,))
    monkeypatch.setattr(schema_definition, 'DEFINITION', source)
    first = tuple(schema_definition.assembled_statements(version=85))
    assert first == ('CREATE TABLE example(value TEXT);\n',)
    assert tuple(schema_definition.assembled_statements(version=85)) == first
    source.write_text('CREATE TABLE example(value INTEGER);\n')
    assert tuple(schema_definition.assembled_statements(version=85)) != first
    source.write_text('CREATE TABLE example(value TEXT\n')
    with pytest.raises(schema_definition.MigrationError, match='Incomplete current schema definition'):
        tuple(schema_definition.assembled_statements(version=85))
