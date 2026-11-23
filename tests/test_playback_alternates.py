"""Verified playback replacements belong to their recording."""
import json

import pytest


@pytest.mark.parametrize('difference', ['none', 'version', 'duration', 'artist', 'unknown_duration'])
def test_only_verified_same_recording_uploads_are_preferred(store, difference):
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
    store.tracks.remember_playback_upload(requested, accepted, evidence=evidence, now=100)
    result = store.tracks.preferred_playback_uploads(['original']).get('original')
    assert (result['video_id'] if result else None) == ('playable' if difference == 'none' else None)
    if difference == 'none':
        assert store.tracks.identity_key_for_video('playable') == keys[0]
        audit = store.conn.execute("SELECT h.value->>4 AS evidence FROM track_sources s,json_each(s.association_history) h "
                                  "WHERE s.video_id='playable' AND h.value->>3='youtube_playback_substitution'").fetchone()
        assert json.loads(audit['evidence']) == {**evidence, 'requested_video_id': 'original'}
        assert store.tracks.track_detail('original')['playback_upload']['video_id'] == 'playable'
        assert store.tracks.preferred_playback_uploads(['playable']) == {}
        store.tracks.upsert_track('original', requested['title'], requested['artist'], '', 326, available=False)
        assert store.radio_catalog.launch_seed('original')['video_id'] == 'playable'
        store.tracks.upsert_track('playable', accepted['title'], accepted['artist'], '', 326, available=False)
        assert store.tracks.preferred_playback_uploads(['original']) == {}
    else:
        assert [store.tracks.identity_key_for_video(t['video_id']) for t in (requested, accepted)] == keys
