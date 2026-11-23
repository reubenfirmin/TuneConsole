"""Home reads keep catalog selection and listening provenance while doing less work."""
from collections import Counter

import pytest

from tests.conftest import seed_catalog_track
from tests.historical_captures import retained_capture
from yt_playlist.core import catalog, listening
from yt_playlist.core.catalog_query import PLAYABLE_LIBRARY_SQL
from yt_playlist.rec import taste_analysis


def _legacy_metadata(store, keys):
    placeholders = ','.join('?' for _ in keys)
    return {r['k']: {'title': r['title'], 'artist': r['artist'], 'album': r['album'] or '',
                     'video_id': r['vid'], 'thumbnail': r['thumb']}
            for r in store.conn.execute(
                'SELECT identity_key k,MIN(title) title,MIN(artist) artist,MIN(album) album,'
                f'MIN(video_id) vid,MIN(thumbnail) thumb FROM ({PLAYABLE_LIBRARY_SQL}) '
                f'WHERE identity_key IN ({placeholders}) GROUP BY identity_key', keys)}


def display_metadata(store, keys):
    return {key: {field: value for field, value in row.items() if field != 'artist_ids'}
            for key, row in store.rec_query.tracks_by_keys(keys).items()}


def test_requested_metadata_keeps_playable_representative_and_selected_fields(store):
    unavailable = store.tracks.upsert_track('a', 'Song', 'Artist', 'First', 200, False)
    alternate = store.tracks.upsert_track('b', 'Song', 'Artist', 'Second', 201, True, thumbnail='second-cover')
    recording = store.tracks.catalog_subject(unavailable)['recording_id']
    store.tracks.associate_youtube_item('b', recording, source='verified', evidence={'same': True}, now=1)
    store.tracks.set_track_title(alternate, 'Edited title')
    store.tracks.set_track_artist(alternate, 'Edited artist')
    missing = store.tracks.upsert_track(None, 'Unresolved', 'Artist', None, 200)
    store.discovery.upsert_discovered_track('cold|artist', 'cold', 'Cold', 'Artist', '', None, None, None, None, 1)
    keys = [store.tracks.catalog_subject(tid)['identity_key'] for tid in (unavailable, missing)]
    keys.extend(['cold|artist', 'unknown', *keys])
    expected = _legacy_metadata(store, keys)
    assert display_metadata(store, iter(keys)) == expected
    assert len(expected) == 1
    assert next(iter(expected.values())) == {'title': 'Edited title', 'artist': 'Edited artist',
                                            'album': 'Second', 'video_id': 'b', 'thumbnail': 'second-cover'}
    assert store.rec_query.tracks_by_keys([]) == {}


def _work(store, target):
    steps = [0]
    def progress():
        steps[0] += 100
        return 0
    store.conn.set_progress_handler(progress, 100)
    try:
        result = target()
    finally:
        store.conn.set_progress_handler(None, 0)
    return result, steps[0]


def test_small_metadata_lookup_does_not_scan_unrequested_library(store):
    seed_catalog_track(store, 'wanted', 'Wanted', 'Artist', video_id='wanted')
    before, small = _work(store, lambda: store.rec_query.tracks_by_keys(['wanted']))
    # Grow only unrelated recordings; count VM work rather than wall time.
    with store.conn:
        for n in range(1500):
            rid = store.conn.execute('INSERT INTO recordings(identity_key,title,artist) VALUES (?,?,?)',
                                     (f'unrelated-{n}', 'Other', 'Artist')).lastrowid
            eid = store.conn.execute('INSERT INTO track_sources(video_id,recording_id,title,artist) VALUES (?,?,?,?)',
                                     (f'other-{n}', rid, 'Other', 'Artist')).lastrowid
            store.conn.execute("UPDATE track_sources SET library_origin='fixture',library_added_at=NULL WHERE id=? AND library_origin IS NULL", (eid,))
    after, large = _work(store, lambda: store.rec_query.tracks_by_keys(['wanted']))
    assert after == before
    assert large <= small * 2 + 500
    keys = ['wanted', *(f'unrelated-{n}' for n in range(1500))]
    assert display_metadata(store, keys) == _legacy_metadata(store, keys)


@pytest.mark.parametrize('generated', [None, 0, 1])
@pytest.mark.parametrize('independent', [False, True])
def test_indexed_evidence_matches_original_projection(store, generated, independent):
    for account in ('main', 'other'):
        store.accounts.upsert_account(account, None, account == 'main')
    ts = 1792497600  # a fixed UTC noon
    for account in (1, 2):
        store.history.import_listening_events(account, [('song', None, ts), ('song', None, ts + 3600)],
                                               source='spotify', source_timezone='UTC')
        store.history.import_listening_events(account, [('song', None, ts)], source='takeout')
        store.history.record_history_plays(account, ts, [('song', 'Today')], source_timezone='UTC')
        store.history.record_history_plays(account, ts + 86400, [('coarse-only', 'Today')], source_timezone='UTC')
    with store.conn:
        store.conn.execute('UPDATE play_events SET is_generated=? WHERE account_id=1', (generated,))
        if independent:
            day = listening.day_at(ts, 'UTC')
            listening.observe_day(store.conn, 1, 'song', day, 'independent', 'UTC', ts)
        # Evidence whose calendar cannot be established must remain unprojected.
        listening.observe_day(store.conn, 1, 'ambiguous', '1990-01-01', 'independent', 'Pacific/Kiritimati', ts)
    retained_capture(store.conn, 1, ts, ['legacy', 'legacy'])
    retained_capture(store.conn, 1, ts + 86400, [])
    original = """SELECT e.* FROM listening_evidence e
WHERE (e.event_id IS NOT NULL AND e.is_generated=0)
   OR (e.event_id IS NULL AND (e.source<>'ytm_history' OR NOT EXISTS (
       SELECT 1 FROM listening_evidence g WHERE g.event_id IS NOT NULL
       AND g.is_generated=1 AND g.account_id=e.account_id
       AND g.identity_key=e.identity_key AND g.day=e.day)
       OR EXISTS (SELECT 1 FROM listening_evidence organic
           WHERE organic.account_id=e.account_id AND organic.identity_key=e.identity_key
           AND organic.day=e.day AND organic.is_generated=0
           AND (organic.event_id IS NOT NULL OR organic.source<>'ytm_history'))))"""
    expected = Counter(map(tuple, store.conn.execute(original)))
    actual = Counter(map(tuple, store.conn.execute('SELECT * FROM eligible_listening_evidence')))
    assert actual == expected  # includes precision, source multiplicity, unknowns and timestamps
    counts = {r[0]: r[1] for r in store.conn.execute(
        'SELECT identity_key,COUNT(*) FROM listening_days GROUP BY identity_key')}
    assert store.rec_query.play_counts() == counts
    assert counts['song'] == (1 if generated == 1 and not independent else 2)


def test_shared_facets_use_canonical_genre_and_keep_upload_years(store):
    iid = store.accounts.upsert_account('main', None, True)
    a = store.tracks.upsert_track('a', 'Song', 'Artist', 'First', 200)
    b = store.tracks.upsert_track('b', 'Song', 'Artist', 'Second', 200)
    store.tracks.associate_youtube_item('b', store.tracks.catalog_subject(a)['recording_id'],
                                        source='verified', evidence={'same': True}, now=1)
    key = store.tracks.catalog_subject(a)['identity_key']
    store.tracks.set_track_genre(a, 'Techno')
    # The latest genre applies to the recording. Release years belong to the uploads.
    with store.conn:
        catalog.choose_field(store.conn, b, 'genre', 'Ambient')
        catalog.choose_field(store.conn, a, 'mb_year', '1990')
        catalog.choose_field(store.conn, b, 'mb_year', '2000')
    store.history.import_listening_events(iid, [(key, 'a', 1792497600), (key, 'a', 1792497600 + 86400)])
    plays = store.rec_query.play_counts()
    facets = store.rec_query.play_weighted_facets(plays)
    assert facets == {'genres': store.rec_query.genre_play_distribution(),
                      'eras': store.rec_query.era_play_distribution()}
    assert facets == {'genres': {'Ambient': 3}, 'eras': {'1990': 3, '2000': 3}}
    assert taste_analysis.taste_breadth(store, genre_plays=facets['genres']) == taste_analysis.taste_breadth(store)
    assert taste_analysis.era_distribution(store, era_plays=facets['eras']) == taste_analysis.era_distribution(store)
