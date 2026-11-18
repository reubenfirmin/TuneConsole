"""Conservative projections of provider-owned performing artist entities."""


def entities(rows, provider, *, name='name', identity='id'):
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or not row.get(name) or not row.get(identity):
            return []  # A partial credit would silently lose a collaborator.
        key = str(row[identity])
        if key not in seen:
            result.append({'name': row[name], 'id': key, 'provider': provider})
            seen.add(key)
    return result


def consensus(credits):
    """Search editions can share a credit; conflicting artist sets remain unresolved."""
    if not credits or any(not credit for credit in credits):
        return []
    identities = [{(member['provider'], member['id']) for member in credit} for credit in credits]
    return credits[0] if all(value == identities[0] for value in identities) else []


def musicbrainz(recordings):
    credits = []
    for recording in recordings:
        rows = []
        for credit in recording.get('artist-credit') or []:
            if isinstance(credit, str):  # A join phrase, never an artist delimiter to parse.
                continue
            artist = credit.get('artist') or {}
            rows.append({'name': credit.get('name') or artist.get('name'), 'id': artist.get('id')})
        credits.append(entities(rows, 'musicbrainz'))
    return consensus(credits)


def discogs(release, title):
    from yt_playlist.util.matching import recording_title
    matches = []
    def visit(tracks, inherited):
        for track in tracks:
            artists = track.get('artists') or inherited
            if track.get('type_') != 'heading' and recording_title(track.get('title')) == recording_title(title):
                matches.append(entities(artists, 'discogs'))
            visit(track.get('sub_tracks') or [], artists)
    visit(release.get('tracklist') or [], release.get('artists') or [])
    return consensus(matches)


def deezer(track):
    contributors = track.get('contributors') or []
    if contributors:
        # Unknown roles do not establish a complete performing credit.
        if any(row.get('role') not in ('Main', 'Featured', 'Composer', 'Producer', 'Remixer') for row in contributors):
            return []
        rows = [row for row in contributors if row.get('role') in ('Main', 'Featured')]
    else:
        rows = [track['artist']] if track.get('artist') else []
    return entities(rows, 'deezer')
