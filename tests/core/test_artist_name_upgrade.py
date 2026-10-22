import sqlite3

import pytest

from yt_playlist.core.migrations import Migration,MigrationError,migrate
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.migrations import v029_artist_provisional_names as upgrade
from yt_playlist.core.store import Store


REGISTRY = (*MIGRATIONS[:28],Migration(29,'artist_provisional_names',upgrade.apply))


def seed(s):
    s.conn.executescript("""
        INSERT INTO artists(id,name,provisional) VALUES (1,'Artist',1),(2,'Artist',0);
        INSERT INTO artist_provider_ids VALUES ('youtube','verified',2);
        INSERT INTO artist_aliases VALUES (1,'Artist','legacy'),(2,'Artist','youtube');
        INSERT INTO artist_redirects VALUES (1,2,'youtube',1);
        INSERT INTO artist_scans VALUES ('@artist:2',10,2);
    """)


def test_populated_v28_upgrade_preserves_rows_and_allows_a_new_unidentified_name(tmp_path):
    path = tmp_path/'state.db'
    s = Store(path)
    migrate(s.conn,registry=MIGRATIONS[:28])
    seed(s)
    tables = [r[0] for r in s.conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                          "AND name NOT LIKE 'sqlite_%' AND name<>'schema_migrations'")]
    before = {table:[tuple(r) for r in s.conn.execute(f'SELECT * FROM {table}')] for table in tables}
    migrate(s.conn,registry=REGISTRY)
    upgrade.validate(s.conn)
    for table,rows in before.items():
        assert [tuple(r) for r in s.conn.execute(f'SELECT * FROM {table}')]==rows
    s._bind_repositories()
    tid = s.tracks.upsert_track('unknown','Unknown','Artist',None,200)
    candidate = s.artists.track_credits(tid)[0]['artist_id']
    assert candidate not in {1,2}
    assert s.artists.get_artist(candidate)['provisional']==1
    assert s.artists.resolve(1)==2
    state = list(s.conn.iterdump())
    s.conn.close()
    s = Store(path)
    migrate(s.conn,registry=REGISTRY)
    upgrade.validate(s.conn)
    assert list(s.conn.iterdump())==state
    assert s.conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    s.conn.close()
    backups = list((tmp_path/'backups').glob('*.db'))
    assert backups
    with sqlite3.connect(backups[-1]) as conn:
        assert conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]==28
        assert 'UNIQUE' in conn.execute("SELECT sql FROM sqlite_master WHERE name='ix_artist_provisional_name'").fetchone()[0]


def test_failed_v29_upgrade_restores_original_unique_index_and_rows(tmp_path):
    s = Store(tmp_path/'state.db')
    migrate(s.conn,registry=MIGRATIONS[:28])
    seed(s)
    before = list(s.conn.iterdump())
    def fail(conn,context):
        upgrade.apply(conn,context)
        raise RuntimeError('interrupted artist name upgrade')
    with pytest.raises(MigrationError,match='interrupted artist name'):
        migrate(s.conn,registry=(*MIGRATIONS[:28],Migration(29,'artist_provisional_names',fail)))
    assert list(s.conn.iterdump())==before
    s.conn.close()


def test_fresh_creation_has_the_current_artist_name_contract(tmp_path):
    s = Store(tmp_path/'state.db')
    migrate(s.conn,registry=REGISTRY)
    upgrade.validate(s.conn)
    assert not s.conn.execute('PRAGMA foreign_key_check').fetchall()
    s.conn.close()
