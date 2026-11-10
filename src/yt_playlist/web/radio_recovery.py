"""Project observed uploads and an explicit restart target without editing the saved queue."""


def observed_queue(tracks, heard):
    observed = {t['_radio_queue_video_id']: t for t in heard if t.get('_radio_queue_video_id')}
    return [observed.get(t['video_id'], t) for t in tracks]


def playback_position(tracks, heard, video):
    return next((i for i, t in enumerate(observed_queue(tracks, heard))
                 if t['video_id'] == video or tracks[i]['video_id'] == video), None)


def native_position(tracks, heard, playlist, state):
    """A single changed queue occurrence proves position, never recording equivalence."""
    native, player = state.get('native') or {}, state.get('player') or {}
    ids = native.get('videoIds') or []
    video = player.get('video_id')
    expected = [t['video_id'] for t in observed_queue(tracks, heard)]
    if (not native.get('available') or player.get('playlist_ytm_id') != playlist
            or (native.get('playlistIds') and native['playlistIds'] != [playlist])
            or not video or ids.count(video) != 1 or len(ids) != len(expected)):
        return None
    position = ids.index(video)
    selected = native.get('selectedIndex')
    if selected is not None and selected != position:
        return None
    if all(actual == expected[i] for i, actual in enumerate(ids) if i != position):
        return position
    return None


def resume_player(tracks, heard, position, *, playlist, title, episode, stopped):
    if stopped or not playlist or not tracks:
        return None
    track = heard[-1] if heard else tracks[position if 0 <= position < len(tracks) else 0]
    if not track.get('video_id'):
        return None
    fields = ('video_id', 'title', 'artist', 'thumbnail', 'likeStatus')
    return {**{key: track[key] for key in fields if key in track},
            'paused': True, 'playlist_ytm_id': playlist, 'radio_session_id': episode,
            'source': {'kind': 'radio', 'title': title, 'url': '/#radio', 'external': False}}
