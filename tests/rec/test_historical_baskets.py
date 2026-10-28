"""Past training admits only reconstructable relationships and eligible recordings."""

import pytest


def test_later_album_and_artist_enrichment_cannot_change_historical_baskets(store):
    tracks = [store.tracks.upsert_track(f'v{i}', f'Song {i}', f'Artist {i}', None, 180,
                                       created_at=10) for i in range(3)]
    before, coverage = store.rec_evidence.rec_baskets_as_of(100)
    assert before == []
    assert coverage['undated_relationship_baskets_excluded'] == ['album', 'artist']

    keys = [store.tracks.identity_key_for_video(f'v{i}') for i in range(3)]
    store.collection.cache_album_tracklist('later-album', 'Album', 'Shared artist', None,
                                           keys, entries=[{'entry_id': track} for track in tracks])
    for i in range(3):
        store.tracks.upsert_track(f'v{i}', f'Song {i}', f'Artist {i}', None, 180,
                                 credits=[{'name': 'Shared artist', 'id': 'shared-artist'}])
    assert store.rec_evidence.rec_baskets()  # The present-day model can use this evidence.
    assert store.rec_evidence.rec_baskets_as_of(100)[0] == before


@pytest.mark.parametrize('dated', [False, True])
def test_playlist_existence_proof_does_not_bypass_recording_eligibility(store, dated):
    identity = store.identities.upsert_identity('Main', 'credential', None, True)
    rows = []
    for video, duration, video_type in (
            ('valid-a', 180, None), ('valid-b', None, None),
            ('ugc', 180, 'MUSIC_VIDEO_TYPE_UGC'), ('long', 100000, None)):
        rows.append(store.tracks.upsert_track(video, video, 'Artist', None, duration,
                    video_type=video_type, created_at=10 if dated else None))
    playlist = store.playlists.upsert_playlist(identity, 'stable', 'Stable', 4, 'hash', 20)
    store.playlists.set_playlist_tracks(playlist, rows)
    baskets, coverage = store.rec_evidence.rec_baskets_as_of(100)
    expected = {store.tracks.identity_key_for_video(video) for video in ('valid-a', 'valid-b')}
    assert [set(basket) for basket in baskets] == [expected]
    assert coverage['eligible_tracks'] == 2
