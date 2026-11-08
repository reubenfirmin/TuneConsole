"""Bounded synthetic predecessors for independent catalog migration owners."""
import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from tests.genre_catalog_support import bound_catalog
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.core.migrations import migrate
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.store import Store

FIXTURES = Path(__file__).parent / 'fixtures/migrations'


@pytest.fixture
def catalog_input(request, tmp_path, monkeypatch):
    bound_catalog(monkeypatch, entries=(('house', 'House'), ('techno', 'Techno'),
                                       ('rock', 'Rock'), ('folk', 'Folk')))
    source, target = request.param
    path = tmp_path / 'catalog.db'
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA synchronous=OFF')
        if source == 7:
            migrate(conn, registry=MIGRATIONS[:7], clock=lambda: 1.)
            conn.executescript((FIXTURES / 'recording_catalog.sql').read_text())
            conn.execute("INSERT INTO taste_modes(mode_id,label,families,centroid,size,rep_keys,active,first_seen,last_seen,space) VALUES (1,'House','[]',?,1,?,1,1,1,'old-space')",
                         (b'\x00\x00\x80\x3f', json.dumps(['old spelling|artist'])))
            conn.commit()
        else:
            install_catalog_input(conn, source, tmp_path)
        conn.execute('PRAGMA journal_mode=WAL')
        yield conn, path, source, target


def install_catalog_input(conn, version, root):
    """Supply completed rows/files below the edge, retaining exact shipped DDL."""
    from yt_playlist.core import schema_admission, schema_definition

    document = json.loads((FIXTURES / 'recording_catalog_completed.json').read_text())
    tables = {}
    for checkpoint, changes in document['versions'].items():
        if int(checkpoint) > version:
            break
        for name, rows in changes.items():
            if rows is None:
                tables.pop(name, None)
            else:
                tables[name] = rows
    for relative, text in document['files'].items():
        filename = root / relative
        filename.parent.mkdir(parents=True, exist_ok=True)
        filename.write_text(text)
    statements = tuple(schema_admission.reference()['statements'] if version == 79
                       else schema_definition.statements(version=version))
    programs = []
    # Restore completed producer input before its real capture programs.
    conn.execute('PRAGMA foreign_keys=OFF')
    with conn:
        conn.execute('BEGIN')
        for sql in statements:
            if sql.lstrip().upper().startswith('INSERT '):
                continue  # Completed rows below include the shipped initial data.
            if sql.lstrip().upper().startswith(('CREATE VIEW ', 'CREATE TRIGGER ')):
                programs.append(sql)
            else:
                conn.execute(sql)
        conn.executemany('INSERT INTO schema_migrations VALUES (?,?,1)',
                         [(item.version, item.name) for item in MIGRATIONS[:version]])
        for name, data in tables.items():
            columns = ','.join('"' + column + '"' for column in data['columns'])
            placeholders = ','.join('?' for _ in data['columns'])
            rows = [[(bytes.fromhex(value['sqlite_blob_hex']) if 'sqlite_blob_hex' in value
                      else float(value['sqlite_real'])) if isinstance(value, dict)
                     else value.replace('{root}', str(root)) if isinstance(value, str)
                     else value for value in row] for row in data['rows']]
            conn.executemany(f'INSERT INTO "{name}"({columns}) VALUES ({placeholders})', rows)
        for sql in programs:
            conn.execute(sql)
    conn.execute('PRAGMA foreign_keys=ON')
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()


@contextmanager
def catalog_reader(path, version, *, snapshot_guards=False):
    store = Store(path)
    try:
        # Real consumers bind below admission. Conversion and production
        # admission are exercised independently by their named owners.
        store._schema_compatibility = version < 103
        store._rebuild_source_version = None
        store._schema_recovery_pending = False
        store._bind_repositories()
        if snapshot_guards:
            store._snapshot_revisions = Revisions(store.conn)
        yield store
    finally:
        store.conn.close()
