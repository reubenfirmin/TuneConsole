"""Detached eligibility and fair dealing for clicked Home pools."""
from yt_playlist.rec import mode_rules as mode_surfaces
from yt_playlist.rec.artist_variety import Selection


def library_values(tracks, owned, blocked, muted, weights, axes, parameters, cuts, artist_cap, album_cap):
    tracks = [track for track in tracks if track['key'] in owned and track['key'] not in blocked
              and track['artist'] not in muted
              and weights.get(f"genre:{axes.get(track.get('genre'), ('', None))[0]}", 1.) != 0]
    if parameters['lane'] == 'temporal':
        if not cuts:
            return []
        tracks = [track for track in tracks if mode_surfaces._in_band(
            track.get('year'), parameters['epoch'] % 3, cuts[0], cuts[1])]
    return mode_surfaces._diversify(tracks, artist_cap, album_cap)


def unique_fresh(candidates, blocked, muted):
    unique = {}
    for track in candidates:
        key = track.get('key')
        if key and track.get('video_id') and key not in blocked and track.get('artist') not in muted:
            unique.setdefault(key, track)
    return list(unique.values())


def deal_streams(streams, size, artist_cap):
    """Consume each rank stream only until the requested preview is filled."""
    streams = dict(streams)
    selected = Selection(artist_cap, size, limit=size, minimum=size)
    while streams and len(selected.selected) < size:
        for family, stream in list(streams.items()):
            for track in stream:
                before = len(selected.selected)
                selected.add(track)
                if len(selected.selected) > before:
                    break
            else:
                del streams[family]
            if len(selected.selected) == size:
                break
    return selected.finish()


def fresh_values(pool, axes, weights, include, size, minimum, artist_cap):
    pool = [track for track in pool if weights.get(f"genre:{axes.get(track.get('genre'), ('', None))[0]}", 1.) != 0]
    families = list(dict.fromkeys(include)) or [None]
    streams = {family: iter([track for track in pool if family is None
                            or axes.get(track.get('genre'), ('', None))[0] == family]) for family in families}
    picked = deal_streams(streams, size, artist_cap)
    return picked if complete_fresh(picked, axes, include, minimum) else []


def complete_fresh(picked, axes, include, minimum):
    return len(picked) >= minimum and set(include) <= {
        axes.get(track.get('genre'), ('', None))[0] for track in picked}
