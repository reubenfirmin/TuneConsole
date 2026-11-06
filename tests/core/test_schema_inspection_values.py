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
