"""Integrity checks for real-listening artifacts; these are not listening results."""
from tests.musical_evaluation_support import coverage, holdouts, listening_packet
from yt_playlist.rec import musical_profiles


def test_collaborating_artists_and_shared_albums_cannot_cross_holdouts():
    rows = [
        {'key': 'a', 'artist_ids': [1], 'album_ids': ['one'], 'genre': 'Jazz'},
        {'key': 'b', 'artist_ids': [1, 2], 'album_ids': ['two']},
        {'key': 'c', 'artist_ids': [2], 'album_ids': ['three']},
        {'key': 'd', 'artist_ids': [3], 'album_ids': ['three']},
        {'key': 'unknown'},
    ]
    split = holdouts(rows, ['Jazz'])
    assert split == holdouts(list(reversed(rows)), ['Jazz'])
    assert {split[key]['split'] for key in 'abcd'} == {'genre_holdout'}
    assert len({split[key]['group'] for key in 'abcd'}) == 1
    assert split['unknown']['split'] == 'identity_review'


def test_missing_evidence_counts_unknown_and_blind_packet_never_supplies_labels():
    rows = [{'key': str(i), 'title': f'Song {i}', 'artist': f'Artist {i}', 'video_id': f'v{i}'}
            for i in range(40)]
    profiles = {'0': musical_profiles.resolve([], overrides={'energy': .9})}
    report = coverage(profiles, [row['key'] for row in rows])
    assert report['intensity']['known'] == report['intensity']['explicit'] == 1
    assert report['intensity']['unknown'] == 39
    assert report['tempo']['unknown'] == 40
    packet, manifest = listening_packet(rows, profiles, holdouts(rows))
    assert packet['status'] == 'unlabelled'
    assert 6 * 30 <= len(packet['tracks']) == len(manifest) <= 6 * 40
    assert all(row['fit'] is None and row['intrusion'] is None for row in packet['tracks'])
    assert all(row['more_intense'] is None for row in packet['intensity_pairs'])
    assert not any(set(row) & {'affinity', 'selection', 'profile_digest'} for row in packet['tracks'])
    assert len({row['id'] for row in manifest}) == len(manifest)
