import sqlite3

import pytest

from yt_playlist.core import catalog, metadata
from yt_playlist.providers.waterfall import TrackSink, run_waterfall
from tests.test_waterfall import FakeProv
from tests.metadata_provenance_support import metadata_provenance


def test_catalog_stores_effective_genre_and_audits_edit_refresh_and_reset(store):
    track = store.tracks.upsert_track('video', 'Song', 'Artist', None, 200)
    conn = store.conn
    recording = store.tracks.catalog_subject(track)['recording_id']

    def selected():
        return conn.execute('SELECT effective_genre_key FROM recordings WHERE id=?', (recording,)).fetchone()[0]

    account = store.accounts.upsert_account('Listener', None, True)
    key = store.tracks.identity_key_for_video('video')
    store.history.record_play_event(account, key, 'video', 86400, source_timezone='UTC')
    listening = store.trends.first_play_map('track')
    assert listening == {key: 1}

    with conn:
        metadata.select(conn, track, 'genre', 'Rock', 'lastfm', now=100)
    assert selected() == 'rock'
    store.metadata_choices.choose(track, 'genre', 'Indie Rock', now=101)
    assert selected() == 'indie rock'
    assert store.trends.first_play_map('track') == listening
    with conn:
        metadata.select(conn, track, 'genre', 'Jazz', 'lastfm', now=102)
    assert selected() == 'indie rock'
    with conn:
        catalog.withdraw_choice(conn, track, 'genre', now=103)
    assert selected() == 'jazz'
    assert store.trends.first_play_map('track') == listening
    assert conn.execute("SELECT 1 FROM sqlite_schema WHERE name='entry_field_choices'").fetchone() is None
    history = conn.execute("SELECT operation,source,value,genre_key,changed_at FROM recording_metadata_history "
                           "WHERE entry_id=? AND field='genre' ORDER BY id", (track,)).fetchall()
    assert list(map(tuple, history)) == [
        ('select', 'lastfm', 'Rock', 'rock', 100), ('edit', 'user', 'Indie Rock', 'indie rock', 101),
        ('select', 'lastfm', 'Jazz', 'jazz', 102), ('reset', 'user', 'Jazz', 'jazz', 103),
    ]
    with pytest.raises(sqlite3.OperationalError, match='view'), conn:
        conn.execute('DELETE FROM recording_metadata_history')


def test_provider_refresh_selection_and_blank_override_with_worker_sink(store):
    track = store.tracks.upsert_track('video','Song','Artist',None,200)
    a = FakeProv('musicbrainz',{'Song':{'genre':'Rock','bpm':120}})
    b = FakeProv('discogs',{'Song':{'genre':'Pop','bpm':125}})
    config = [{'name':p.name,'enabled':True} for p in (a,b)]

    def run():
        run_waterfall(store,[store.tracks.track_for_waterfall(track)],config,lambda e:None,
                      registry={p.name:p for p in (a,b)},sink_for=lambda t:TrackSink(store,t))

    run()
    assert store.tracks.get_track_enrichment(track)[0] == 'Rock'
    assert store.tracks.get_track_audio(track)[0] == 120
    a._by_title['Song'] = {'genre':'Electronic','bpm':122}
    run()
    assert store.tracks.get_track_enrichment(track)[0] == 'Electronic'
    assert store.tracks.get_track_audio(track)[0] == 122
    store.tracks.set_track_genre(track,'')
    with store.conn:
        catalog.choose_field(store.conn,track,'bpm',None)
    a._by_title['Song'] = {'genre':'House','bpm':128}
    run()
    fields = metadata_provenance(store, track)['fields']
    assert fields['genre'] == {'value':'','override':True,'source':'user'}
    assert fields['bpm'] == {'value':None,'override':True,'source':'user'}
    from yt_playlist.core import genre_classifications
    recording = store.tracks.catalog_subject(track)['recording_id']
    assert genre_classifications.read(store.conn, 'recordings', recording) == 'House'
    assert tuple(store.conn.execute('SELECT bpm,typeof(bpm) FROM recordings WHERE id=?',
                                    (recording,)).fetchone()) == (128,'real')
    with store.conn:
        catalog.withdraw_choice(store.conn, track, 'genre')
        catalog.withdraw_choice(store.conn, track, 'bpm')
    fields = metadata_provenance(store, track)['fields']
    assert fields['genre'] == {'value':'House','override':False,'source':'musicbrainz'}
    assert store.conn.execute("SELECT COUNT(*) FROM metadata_observations o JOIN metadata_deliveries d "
                              "ON d.id=o.delivery_id WHERE d.provider IN ('musicbrainz','discogs') "
                              "AND o.field IN ('genre','bpm')").fetchone()[0] == 12


def test_resolution_failure_preserves_choice_and_retry_completes(store):
    """A failed receipt preserves the earlier choice; retry finishes the ordered workflow."""
    track = store.tracks.upsert_track('video','Song','Artist',None,200)
    store.enrichment.upsert_conflict(track,'genre',[{'provider':'a','value':'Rock'},
                                                  {'provider':'b','value':'Pop'}])
    store.conn.execute("CREATE TEMP TRIGGER reject_resolution BEFORE UPDATE ON metadata_conflicts "
                       "BEGIN SELECT RAISE(ABORT,'interrupted resolution'); END")
    with pytest.raises(sqlite3.IntegrityError,match='interrupted resolution'):
        store.enrichment.resolve_conflict(track,'genre','')
    assert store.tracks.get_track_enrichment(track)[0] == ''
    assert store.conn.execute('SELECT COUNT(*) FROM track_sources s,json_each(s.choice_observations)').fetchone()[0] == 1
    assert store.conn.execute("SELECT operation,source,value FROM recording_metadata_history WHERE operation='resolve'").fetchone()[:] == (
        'resolve', 'conflict_resolution', '')
    assert store.conn.execute('SELECT COUNT(*) FROM current_runtime_choices').fetchone()[0] == 0
    assert store.conn.execute('SELECT COUNT(*) FROM metadata_decisions').fetchone()[0] == 1
    assert store.conn.execute('SELECT resolved FROM enrichment_conflict').fetchone()[0] == 0
    assert not store.conn.in_transaction
    store.conn.execute('DROP TRIGGER reject_resolution')
    store.enrichment.resolve_conflict(track, 'genre', '')
    assert store.tracks.get_track_enrichment(track)[0] == ''
    assert store.conn.execute('SELECT resolved,resolved_value FROM enrichment_conflict').fetchone()[:] == (1, '')
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
