"""Keep full fetched album track lists for measured listening coverage."""
from yt_playlist.util.thumbnails import best_thumb
from yt_playlist.library.album_fetch import shareable_album_tracks
from yt_playlist.util.duration import parse_duration


def remember_album(store, browse_id, album, fallback=None):
    """Retain fetched occurrences; publish only a complete response."""
    fallback = fallback or {}
    tracks = (album or {}).get("tracks") or []
    if not tracks or not all(isinstance(t,dict) for t in tracks):
        return
    advertised = album.get("trackCount")
    expected = int(str(advertised).replace(',','')) if str(advertised).replace(',','').isdigit() else None
    complete = (not album.get('continuations') and not album.get('continuation') and
                (advertised is None or expected==len(tracks)))
    def artists(value):
        return ", ".join(a.get("name", "") for a in value.get("artists", []) or [] if a.get("name"))

    artist = artists(album) or fallback.get("artist") or ""
    title = album.get("title") or fallback.get("title")
    if not title or not artist:
        return
    def positive(value):
        return int(value) if str(value).isdigit() and int(value)>0 else None

    try:
        video_ids = [track["videoId"] for track in shareable_album_tracks(album)]
    except ValueError:
        video_ids = None
    # Another completed response can reconcile these IDs. Keep proof acceptance
    # and its full cache publication together so an older accepted response
    # cannot overwrite the newer shared release after that reconciliation.
    with store._lock:
        if complete and not store.collection.observe_album_representation(browse_id, album):
            return
        entries = []
        for track in tracks:
            captured = dict(disc_number=positive(track.get('discNumber',track.get('disc_number'))),
                            track_number=positive(track.get('trackNumber',track.get('track_number'))))
            if track.get('title'):
                captured['entry_id'] = store.tracks.observe_track_entry(
                    track.get('videoId'),track['title'],artists(track) or artist,album=title,
                    album_browse_id=browse_id,credits=track.get('artists') or album.get('artists'),
                    duration_s=parse_duration(track.get('duration')),
                    thumbnail=best_thumb(track.get('thumbnails')) or best_thumb(album.get('thumbnails')))
            else:
                # A provider's unavailable placeholder is still an occurrence.
                # It supplies no metadata from which to invent a recording.
                captured.update(raw_source_ref=track.get('videoId'),captured_credit=artists(track) or None,unavailable=True)
            entries.append(captured)
        if not complete:
            store.collection.cache_partial_album(browse_id,title,artist,best_thumb(album.get('thumbnails')),entries,expected_count=expected)
            return
        store.collection.cache_album_tracklist(
            browse_id, title, artist, best_thumb(album.get("thumbnails")) or fallback.get("thumbnail"), [],
            video_ids=video_ids,entries=entries,credits=album.get('artists'),
            year=album.get('year'),album_type=album.get('type'))
