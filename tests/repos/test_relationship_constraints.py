import json
from contextlib import closing
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest

from yt_playlist.core.migrations import Migration,MigrationError,migrate
from yt_playlist.core.store import Store
from yt_playlist.core import legacy_evidence
from tests.genre_catalog_support import bound_catalog
from tests.prepared_fixture_support import capture_template


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These relationship owners do not construct an HTTP lifespan."""


@pytest.fixture(scope='module', autouse=True)
def isolated_app_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('relationship-home')))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        patch.delenv('DEEPSEEK_API_KEY', raising=False)
        bound_catalog(patch, entries=(('house', 'House'), ('rock', 'Rock'), ('jazz', 'Jazz')))
        yield


@pytest.fixture(scope='module')
def test_indexes_support_the_affected_queries(store):
    queries = (
        ('SELECT track_source_id FROM playlist_items WHERE playlist_id=? ORDER BY position',
         (1,), 'sqlite_autoindex_playlist_items_2'),
        ('SELECT playlist_id FROM playlist_items WHERE track_source_id=?',(1,),'ix_playlist_item_source'),
        ('SELECT capture_id FROM listening_observations WHERE recording_id=? AND day>=?',(1,'2026-01-01'),'ix_listening_observation_recording_day'),
        ('SELECT id FROM play_events WHERE day>=?',('1970-01-01',),'ix_play_event_day'),
        ('SELECT id FROM play_events WHERE account_id=? AND playlist_ytm_id=? AND played_at>=?',(1,'PL',1),'ix_play_event_account_playlist_time'),
        ('SELECT recording_id FROM track_sources WHERE video_id=?',('a',),'sqlite_autoindex_track_sources_1'),
        ('SELECT recording_id FROM recording_identifiers WHERE scheme=? AND entity_type=? AND value=?',('musicbrainz','recording','id'),'ix_recording_identifier'),
        ('SELECT album_id FROM album_provider_ids WHERE provider=? AND provider_id=?',('youtube','id'),'sqlite_autoindex_album_provider_ids_1'),
    )
    for sql,args,index in queries:
        plan = ' '.join(r[3] for r in store.conn.execute('EXPLAIN QUERY PLAN '+sql,args))
        assert index in plan,(sql,plan)


def test_account_scope_guard_rejects_another_accounts_system_playlist(store):
    a = store.accounts.upsert_account('a','brand',False)
    b = store.accounts.upsert_account('b',None,True)
    first = store.playlists.upsert_playlist(a,'LM','A',0,'h',1)
    second = store.playlists.upsert_playlist(b,'LM','B',0,'h',1)
    ref = store.playlists.get_playlist(second).playlist_scope_id
    with pytest.raises(sqlite3.IntegrityError),store.conn:
        store.conn.execute('UPDATE playlists SET playlist_scope_id=? WHERE id=?',(ref,first))


def test_deleted_playlist_ids_never_attach_old_feedback_to_new_memberships(store):
    from tests.mode_fixtures import seed_modes
    seed_modes(store,1,2)
    owner = store.accounts.upsert_account('Main',None,True)
    first = store.playlists.upsert_playlist(owner,'SAME','First',0,'h',1)
    store.modes.log_pick(first,1,10)
    store.history.record_play_event(owner,'song|artist','video',15,playlist_ytm_id='SAME')
    store.playlists.remove_playlist(first)
    assert store.playlists.get_playlist(first) is None
    second = store.playlists.upsert_playlist(owner,'SAME','Second',0,'h',20)
    assert second!=first
    store.modes.log_pick(second,2,30)
    store.history.record_play_event(owner,'other|artist','other-video',400,playlist_ytm_id='SAME')
    retained = store.conn.execute(
        'SELECT m.id,m.picked_mode_id,m.account_id,s.provider_playlist_id,m.is_current '
        'FROM playlists m JOIN playlist_scopes s ON s.id=m.playlist_scope_id '
        'WHERE m.mode_picked_at IS NOT NULL ORDER BY m.id')
    assert [tuple(row) for row in retained] == [(first,1,owner,'SAME',0),(second,2,owner,'SAME',1)]
    assert [row[0] for row in store.conn.execute(
        'SELECT membership_ref_id FROM play_events ORDER BY played_at')] == [first, second]
    with pytest.raises(sqlite3.IntegrityError),store.conn:
        store.conn.execute('UPDATE playlists SET is_current=1 WHERE id=?', (first,))


def test_implicit_rowid_allocation_cannot_retarget_a_retained_membership(store):
    from yt_playlist.core.playlist_scopes import ensure
    owner = store.accounts.upsert_account('Main',None,True)
    first = store.playlists.upsert_playlist(owner,'FIRST','First',0,'h',1)
    store.playlists.remove_playlist(first)
    ensure(store.conn,'NEXT',owner)
    store.conn.commit()
    with store.conn:
        second = store.conn.execute(
            "INSERT INTO playlists(account_id,title,playlist_scope_id) "
            "VALUES (?,'Next','NEXT')", (owner,)).lastrowid
    assert second>first
