"""Cluster seed responses, independent of database retrieval."""
from yt_playlist.repos.cluster_evidence import SeedMatches


def search_results(matches: SeedMatches) -> list[dict]:
    out = []
    for kind, groups in (('artist', matches.artists), ('playlist', matches.playlists)):
        for group in groups:
            out.append({'kind': kind, 'label': group.name,
                        'sub': f'{group.count} track' + ('' if group.count == 1 else 's'),
                        'keys': list(group.keys)})
    out.extend({'kind': 'song', 'label': song.title, 'sub': song.artist, 'keys': [song.key]}
               for song in matches.songs)
    return out
