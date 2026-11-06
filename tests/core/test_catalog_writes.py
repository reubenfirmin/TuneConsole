"""Explicit catalog mutations retain metadata, membership and revision ownership."""
import sqlite3

import pytest

from yt_playlist.core import catalog, genre_identity, metadata
from yt_playlist.core.migrations import current, registry, Migration, MigrationError, migrate
from yt_playlist.core.migrations import v102_catalog_writes
from yt_playlist.core.store import Store


def test_catalog_projections_are_read_only_and_owner_constraints_remain(store):
    track = store.tracks.upsert_track('video','Song','Artist',None,200)
    account = store.accounts.upsert_account('Main','credential',None,True)
    playlist = store.playlists.upsert_playlist(account,'remote','Playlist',1,'hash',1)
    store.playlists.set_playlist_tracks(playlist,[track])
    store.discovery.upsert_discovered_track('song|artist','video','Song','Artist',None,None,None,None,None,1)
    assert not set(v102_catalog_writes.RETIRED) & {r[0] for r in store.conn.execute(
        "SELECT name FROM sqlite_schema WHERE type='trigger'")}
    for sql in ("UPDATE catalog_tracks SET genre='Jazz'", "UPDATE tracks SET title='Other'",
                'UPDATE discovered_tracks SET bpm=120', 'DELETE FROM discovered_tracks',
                'DELETE FROM playlist_tracks', 'INSERT INTO playlist_tracks VALUES (1,1,1)'):
        with pytest.raises(sqlite3.OperationalError,match='view'):
            store.conn.execute(sql)
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute('INSERT INTO playlist_items VALUES (?, ?, -1)',(playlist,'video'))
    store.conn.rollback()
    assert store.playlists.get_playlist_track_ids(playlist)==[track]


def test_selected_fill_metadata_retains_lineage_and_explicit_choices(store):
    entry = store.tracks.upsert_track('a','Song','Artist',None,200)
    other = store.tracks.upsert_track('b','Other','Artist',None,200)
    with store.conn:
        metadata.fill(store.conn, entry, {'bpm': 120, 'danceability': .7, 'loudness': -12}, 'deezer')
        metadata.fill(store.conn, entry, {'bpm': 130}, 'acousticbrainz')
        metadata.fill(store.conn, entry, {'genre': 'Jazz', 'mb_year': '2001'}, 'musicbrainz', blank=True)
    assert store.tracks.get_track_audio(entry) == (120, None, .7)
    with store.conn:
        catalog.choose_field(store.conn,entry,'bpm',None)
        catalog.choose_field(store.conn,entry,'genre','')
        metadata.select(store.conn,entry,'bpm',125,'provider-refresh')
        metadata.select(store.conn,entry,'genre','House','provider-refresh')
    assert store.tracks.get_track_audio(entry)[0] is None
    assert store.tracks.get_track_enrichment(entry)[0]==''
    with store.conn:
        catalog.withdraw_choice(store.conn,entry,'bpm')
        catalog.withdraw_choice(store.conn,entry,'genre')
    assert store.tracks.get_track_audio(entry)[0]==125
    assert store.tracks.get_track_enrichment(entry)==('House','2001')
    rid = store.tracks.catalog_subject(other)['recording_id']
    store.tracks.associate_youtube_item('a',rid,source='verified',evidence={'same':'recording'},now=5,
                                       same_recording=True)
    assert store.tracks.get_track_audio(other)==(125,None,.7)
    assert store.conn.execute('SELECT loudness FROM youtube_items WHERE video_id=\'b\'').fetchone()[0] is None
    assert store.conn.execute('SELECT count(*) FROM recording_metadata_selections WHERE recording_id=?',
                              (rid,)).fetchone()[0]>=3


def test_playlist_replacement_validates_before_deleting_and_preserves_mixed_order(store):
    account = store.accounts.upsert_account('Main','credential',None,True)
    pid = store.playlists.upsert_playlist(account,'remote','Playlist',3,'hash',1)
    known = store.tracks.upsert_track('video','Song','Artist',None,200)
    unresolved = store.tracks.upsert_track(None,'Unresolved','Artist',None,200)
    store.playlists.set_playlist_tracks(pid,[known,unresolved,known])
    assert store.playlists.get_playlist_track_ids(pid)==[known,unresolved]
    before = list(store.conn.iterdump())
    with pytest.raises(ValueError,match='Unknown track reference'):
        store.playlists.set_playlist_tracks(pid,[unresolved,999999])
    assert list(store.conn.iterdump())==before
    store.playlists.set_playlist_tracks(pid,[unresolved,known])
    assert store.playlists.get_playlist_track_ids(pid)==[unresolved,known]
    assert tuple(store.conn.execute('SELECT entry_id,position FROM unresolved_playlist_items').fetchone())==(unresolved,0)
    assert tuple(store.conn.execute('SELECT video_id,position FROM playlist_items').fetchone())==('video',1)


def test_metadata_fill_rolls_back_every_selection_if_one_field_is_invalid(store):
    entry = store.tracks.upsert_track('video','Song','Artist',None,200)
    before = list(store.conn.iterdump())
    with pytest.raises((ValueError,sqlite3.IntegrityError)):
        with store.conn:
            metadata.fill(store.conn, entry, {'bpm': 120, 'instrumental': float('inf')}, 'acousticbrainz')
    assert list(store.conn.iterdump())==before


def test_facade_retirement_upgrade_is_atomic_and_matches_fresh_contract(tmp_path):
    store = Store(tmp_path/'predecessor.db')
    try:
        for sql in current.statements(version=101):
            store.conn.execute(sql)
        genre_identity.seed_catalog(store.conn)
        store.conn.executemany('INSERT INTO settings VALUES (?,?)',current.SEED_SETTINGS.items())
        store.conn.executemany('INSERT INTO schema_migrations VALUES (?,?,1)',
            [(m.version,m.name) for m in registry.MIGRATIONS[:101]])
        store.conn.commit()
        before = list(store.conn.iterdump())
        def interrupted(conn,context):
            v102_catalog_writes.apply(conn,context)
            raise RuntimeError('interrupted facade retirement')
        with pytest.raises(MigrationError,match='interrupted facade retirement'):
            migrate(store.conn,registry=(*registry.MIGRATIONS[:101],Migration(102,'catalog_writes',interrupted)))
        assert list(store.conn.iterdump())==before
        # Routine admission retains a supported predecessor; explicitly exercise
        # the frozen upgrade here, after proving the interrupted step rolled back.
        migrate(store.conn,registry=registry.MIGRATIONS)
        store.init_schema()
        current.validate(store.conn)
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        store.conn.close()


def test_historical_implicit_definition_lookup_is_version_bound_and_always_resets():
    def triggers():
        return {sql.split()[2] for sql in current.statements() if sql.lstrip().startswith('CREATE TRIGGER ')}
    assert not v102_catalog_writes.RETIRED & triggers()
    with pytest.raises(RuntimeError):
        with current.migration_definitions(92):
            assert 'catalog_tracks_update' in triggers()
            raise RuntimeError('interrupted historical lookup')
    assert not v102_catalog_writes.RETIRED & triggers()
