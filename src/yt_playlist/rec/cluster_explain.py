"""Choose and phrase grounded cluster explanations from detached library facts."""
from yt_playlist.repos.cluster_evidence import ConnectionEvidence
from yt_playlist.util import genre_map
from yt_playlist.util.years import decade


def _join_names(names, total):
    if not names:
        return ''
    extra = total - len(names)
    if extra > 0:
        return ', '.join(names) + f' & {extra} more'
    if len(names) == 1:
        return names[0]
    return ', '.join(names[:-1]) + ' & ' + names[-1]


def connection_reasons(evidence: ConnectionEvidence, *, max_artists=3) -> list[dict]:
    """Strongest reasons first; absence of a fact never creates a claim."""
    out = []
    if evidence.same_artist is not None:
        out.append({'kind': 'same_artist',
                    'text': f'Same artist ({evidence.same_artist}) as elsewhere on this branch.'})
    if evidence.playlists:
        artists = []
        for neighbor in evidence.playlists:
            artist = neighbor.artist.strip()
            if artist and artist.lower() != evidence.artist.lower() and artist not in artists:
                artists.append(artist)
        n = len({neighbor.playlist_id for neighbor in evidence.playlists})
        lead = f'In {n} of your playlist' + ('s' if n != 1 else '')
        names = _join_names(artists[:max_artists], len(artists))
        out.append({'kind': 'playlist',
                    'text': f'{lead}, alongside {names}.' if names else f'{lead} on this branch.'})
    if evidence.album is not None:
        out.append({'kind': 'album',
                    'text': f'From the album “{evidence.album.title}”, with {evidence.album.artist}.'})
    if evidence.same_day:
        out.append({'kind': 'session', 'text': 'You played them on the same day.'})
    family = genre_map.family(evidence.genre) if evidence.genre else None
    if family and any(genre_map.family(g) == family for g in evidence.path_genres):
        label = family.split('other:', 1)[-1] if family.startswith('other:') else family
        out.append({'kind': 'genre', 'text': f'Both in your {label} family.'})
    year_decade = decade(evidence.year)
    if year_decade is not None and any(decade(y) == year_decade for y in evidence.path_years):
        out.append({'kind': 'decade', 'text': f'Both from the {year_decade}s.'})
    return out
