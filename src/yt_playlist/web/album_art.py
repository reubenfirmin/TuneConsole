"""Album artwork presentation over supplied metadata."""
from yt_playlist.util.thumbnails import is_placeholder_art


def cover_and_mosaic(album, tracks):
    cover = (album or {}).get('thumbnail')
    if cover and not is_placeholder_art(cover):
        return cover, []
    seen = []
    for track in tracks or []:
        art = track.get('thumbnail')
        if art and not is_placeholder_art(art) and art not in seen:
            seen.append(art)
            if len(seen) == 4:
                break
    if len(seen) == 1:
        return seen[0], []
    return (None, seen) if seen else (cover, [])
