"""Rehearse admission using an isolated copy of an installed genre schema."""
from pathlib import Path
import sqlite3

import pytest

from yt_playlist.core.migrations import current, migrate, registry


def installed_layout_names(version):
    """Select the numbered layout, excluding unrelated optional publications."""
    layout = sqlite3.connect(':memory:')
    try:
        for sql in current.statements(version=version):
            layout.execute(sql)
        return {row[0] for row in layout.execute(
            "SELECT name FROM sqlite_schema WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'")}
    finally:
        layout.close()


def test_installed_genre_schema_and_actual_axes_upgrade(request, tmp_path):
    option = request.config.getoption('--schema-source-db')
    if not option:
        pytest.skip('Supply --schema-source-db for read-only schema/axis capture')
    source = sqlite3.connect(Path(option).resolve(strict=True).as_uri()+'?mode=ro',uri=True)
    source.row_factory = sqlite3.Row
    source.execute('PRAGMA query_only=ON')
    source.execute('BEGIN')
    tables = ('genre_family','genre_catalog','genre_alias','genre_external_id','genre_preferences',
              'taste_axes','taste_axis_legacy','rec_weights','rec_lean','home_hidden_facet','rec_theme',
              'play_gradient_checkpoints','recommendation_gradient_events','schema_migrations')
    try:
        # Retain actual definitions for the installed numbered layout. Optional
        # publication owners require their own captured evidence and revision
        # state; copying their DDL alone constructs an invalid empty fixture.
        version = source.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]
        names = installed_layout_names(version)
        captured = {row[0]: row[1] for row in source.execute(
            "SELECT name,sql FROM sqlite_schema WHERE sql IS NOT NULL ORDER BY rowid") if row[0] in names}
        assert captured.keys() == names, 'The source is missing a numbered layout object'
        schema = list(captured.values())
        rows = {table:[tuple(row) for row in source.execute('SELECT * FROM '+table)] for table in tables}
        settings = [(key,source.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()[0])
                    for key in current.SEED_SETTINGS]
    finally:
        source.rollback()
        source.close()
    conn = sqlite3.connect(tmp_path/'installed-genres.db')
    conn.row_factory = sqlite3.Row
    try:
        for sql in schema:
            conn.execute(sql)
        for table,values in rows.items():
            if values:
                conn.executemany('INSERT INTO '+table+' VALUES ('+','.join('?' for _ in values[0])+')',values)
        conn.executemany('INSERT INTO settings VALUES (?,?)',settings)
        conn.commit()
        conn.execute('PRAGMA foreign_keys=ON')
        before = conn.total_changes
        assert migrate(conn,defer_validation=True)
        assert conn.total_changes == before
        history = [tuple(row) for row in conn.execute('SELECT * FROM schema_migrations ORDER BY version')]
        assert history == rows['schema_migrations']
        migrate(conn)
        assert [tuple(row) for row in conn.execute('SELECT * FROM schema_migrations ORDER BY version')] == history
        # Ordinary startup admits supported installed layouts without forcing
        # bulk conversion. Rehearse the explicit numbered upgrade separately.
        migrate(conn, registry=registry.MIGRATIONS)
        current.validate(conn)
        assert [tuple(row) for row in conn.execute('SELECT * FROM schema_migrations ORDER BY version')][:len(history)] == history
        for table in tables:
            if table == 'schema_migrations':
                continue
            original_width = len(rows[table][0]) if rows[table] else 0
            assert [tuple(row)[:original_width] for row in conn.execute('SELECT * FROM '+table)] == rows[table]
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        print('Installed numbered layout and actual genre/taste rows admitted:',
              {'version':version,'objects':len(schema),'actual_rows':{t:len(v) for t,v in rows.items()},
               'controlled_state':'Empty unrelated owners; seed settings copied from source; '
                   'optional publication schema and state excluded'})
    finally:
        conn.close()
