"""Provider replacements retain their evidence and never invent recording equivalence."""
import json

import pytest


@pytest.mark.parametrize('difference', ['none', 'version', 'duration', 'artist', 'unknown_duration'])
def test_only_same_song_alternatives_are_reused_and_accounts_remain_separate(store, difference):
    first = store.identities.upsert_identity('Main', 'ref', None, True)
    second = store.identities.upsert_identity('Other', 'other', None, False)
    requested = {'video_id': 'original', 'title': 'Ride (Vince Clarke Remix)',
                 'artist': 'Dubfire & Miss Kittin', 'duration': 326}
    accepted = {**requested, 'video_id': 'playable', 'artist': 'Dubfire'}
    if difference == 'version':
        accepted['title'] = 'Ride (Original Mix)'
    elif difference == 'duration':
        accepted['duration'] = 600
    elif difference == 'artist':
        accepted['artist'] = 'Unrelated artist'
    elif difference == 'unknown_duration':
        accepted['duration'] = None
    for track in (requested, accepted):
        store.tracks.upsert_track(track['video_id'], track['title'], track['artist'], '', track['duration'])
    keys = [store.tracks.identity_key_for_video(t['video_id']) for t in (requested, accepted)]
    evidence = {'playlist': 'PL', 'set_video_id': 'occurrence'}
    store.tracks.remember_playback_alternate(first, requested, accepted, evidence=evidence, now=100)
    row = store.conn.execute('SELECT * FROM youtube_playback_alternates').fetchone()
    assert json.loads(row['evidence_json']) == evidence
    assert bool(row['same_recording']) == (difference == 'none')
    result = store.tracks.playback_alternate(first, 'original')
    assert (result['video_id'] if result else None) == ('playable' if difference == 'none' else None)
    assert store.tracks.playback_alternate(second, 'original') is None
    assert [store.tracks.identity_key_for_video(t['video_id']) for t in (requested, accepted)] == keys
    if difference == 'none':
        assert store.tracks.track_detail('original')['playback_alternates'][0]['video_id'] == 'playable'
        store.tracks.upsert_track('playable', accepted['title'], accepted['artist'], '', 326, available=False)
        assert store.tracks.playback_alternate(first, 'original') is None
