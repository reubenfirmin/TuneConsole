"""The bounded production source for rebuild tests.

The frozen 64-migration route was compared with v0.1.18 commit
c679eb2e201e06d941d71581d22cb0c66a14a73e using that release's own code.
All 491 schema objects and the complete migration ledger matched. Pinning its
DDL here prevents later bootstrap changes from silently changing this fixture.
This is a small populated released-layout fixture, not a full-library backup.
"""
from contextlib import closing
import hashlib
import json
import sqlite3

import pytest

from yt_playlist.core import genre_identity
from yt_playlist.core.db_rebuild import build, source
from yt_playlist.core.migrations import current, registry, runner


PRODUCTION_DDL_SHA256 = '95036d09b6a69976b6b6898bd94d1eef88b6b670b089198c9092491f00bfa8b7'


def build_production64(conn):
    """Build the unchanged, genuinely migrated and populated release source."""
    with conn:
        runner.migrate(conn, registry=registry.MIGRATIONS[:64], clock=lambda: 1000.)
        objects = conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema "
            "WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name").fetchall()
        fingerprint = hashlib.sha256(json.dumps(objects, separators=(',', ':')).encode()).hexdigest()
        assert fingerprint == PRODUCTION_DDL_SHA256, 'Fixture no longer matches the released schema'
        conn.execute('PRAGMA foreign_keys=ON')
        with conn:
            populate_catalog(conn)
        assert conn.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()


def populate_catalog(conn):
    """The same captured identities below either admitted layout's converters."""
    conn.executemany('INSERT INTO identities(id,label,credential_ref,is_master) VALUES(?,?,?,?)',
        [(11, 'Main', 'main.json', 1), (29, 'Other account', 'other.json', 0)])
    for identity in (3, 8, 19):
        video, key = 'video-'+str(identity), 'song '+str(identity)+'|artist'
        conn.execute('INSERT INTO recordings(id,identity_key,title,artist) VALUES(?,?,?,?)',
                     (identity, key, 'Song '+str(identity), 'Artist'))
        conn.execute('INSERT INTO youtube_items(video_id,recording_id,duration_s,available) VALUES(?,?,200,1)',
                     (video, identity))
        conn.execute('INSERT INTO catalog_entries(id,recording_id,video_id) VALUES(?,?,?)',
                     (identity+100, identity, video))
        conn.execute("INSERT INTO library_memberships VALUES(?,'released fixture',1000)", (identity+100,))
    for account, playlist in ((11, 41), (29, 73)):
        scope = f'@account:{account}:LM'
        conn.execute("INSERT INTO playlist_scopes(ref,provider_id,identity_id,kind) VALUES(?,'LM',?,'account')",
                     (scope, account))
        conn.execute('INSERT INTO playlists(id,identity_id,ytm_playlist_id,title,scope_ref) VALUES(?,?,?,?,?)',
                     (playlist, account, 'LM', 'Favorites', scope))
        conn.executemany('INSERT INTO playlist_items VALUES(?,?,?)',
                         [(playlist, 'video-'+str(identity), position)
                          for position, identity in enumerate((19, 3, 8))])
    conn.execute("INSERT INTO item_ratings VALUES(11,'video-8','LIKE','api',1001)")
    conn.execute('INSERT INTO play_events(id,identity_id,identity_key,video_id,played_at,recording_id) '
                 "VALUES(91,11,'song 8|artist','video-8',1002,8)")
    conn.execute("INSERT INTO play_event_sources VALUES(91,'live')")


@pytest.fixture
def production64(tmp_path, production64_template):
    path = tmp_path/'production64.db'
    with closing(sqlite3.connect(path)) as conn:
        production64_template.backup(conn)
    return path


def build_development93(conn, released):
    """Produce the explicit populated v93 input, before consumer mutations."""
    conn.row_factory = sqlite3.Row
    build._configure(conn)
    conn.execute('BEGIN IMMEDIATE')
    for sql in current.statements(version=93):
        conn.execute(sql)
    genre_identity.seed_catalog(conn)
    conn.executemany('INSERT INTO settings VALUES (?,?)', current.SEED_SETTINGS.items())
    conn.executemany('INSERT INTO schema_migrations VALUES (?,?,?)',
                     [(m.version, m.name, 2000.) for m in registry.MIGRATIONS[:93]])
    conn.execute('INSERT INTO group_retention_state VALUES (1,0)')
    for table in ('identities', 'recordings', 'youtube_items', 'catalog_entries', 'library_memberships',
                  'playlist_scopes', 'playlists', 'playlist_items', 'item_ratings', 'play_events', 'play_event_sources',
                  'rec_change_queue', 'rec_preparation', 'rec_group_clock', 'rec_vibe_pending'):
        fields = source.columns(released, table)
        mapped = ['unresolved_genre' if field == 'genre' else field for field in fields]
        conn.execute('DELETE FROM ' + table)
        conn.executemany('INSERT INTO ' + table + '(' + ','.join(mapped) + ') VALUES (' +
                         ','.join('?' for _ in fields) + ')',
                         released.execute('SELECT ' + ','.join(fields) + ' FROM ' + table))
    conn.commit()
    runner.validate_version(conn, 93)


@pytest.fixture(scope='module')
def completed_destination_sql():
    """An actual empty destination below tests of converters and recovery.

    Keep the real initializer and its validation in this measured fixture.
    Consumers copy its completed SQL rather than bootstrap and validate an
    unrelated empty database for every source-selection and construction step.
    """
    from yt_playlist.core.db_rebuild import destination

    with closing(sqlite3.connect(':memory:')) as template:
        build._configure(template)
        template.execute('BEGIN')
        destination.initialize(template)
        template.commit()
        statements = tuple(template.iterdump())
    assert statements[0] == 'BEGIN TRANSACTION;' and statements[-1] == 'COMMIT;'
    return statements[1:-1]


@pytest.fixture
def completed_destination_schema(completed_destination_sql, monkeypatch):
    from yt_playlist.core.db_rebuild import destination

    def initialize(conn):
        assert conn.in_transaction
        for sql in completed_destination_sql:
            conn.execute(sql)

    monkeypatch.setattr(destination, 'initialize', initialize)


def admitted_rebuild_format(conn):
    """Completed shape admission beneath converter/compaction coordination.

    Cold schema/installed-format owners perform the actual shape comparison.
    Retain real immutable numbered/regular receipts here; SQL integrity and FKs
    are still checked by the real builder and installer.
    """
    from yt_playlist.core.migrations import regular

    version = runner.history(conn, registry.MIGRATIONS)
    assert version == current.VERSION
    assert regular.validate(conn) == len(regular.CHANGES)
    return version
