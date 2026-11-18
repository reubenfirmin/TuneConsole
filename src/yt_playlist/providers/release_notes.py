"""Public release evidence, identity checked before any rating is attributed.

MusicBrainz release-group relationships supply review links and Discogs IDs. A
Discogs community average describes the matched edition, never a critic's score.
All vendor requests use their integration access objects and existing egress policy.
"""
import math
import re
from html import unescape
from urllib.parse import urlsplit

from yt_playlist.providers import discogs, musicbrainz
from yt_playlist.util import net
from yt_playlist.util.matching import artist_key, recording_title


def public_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password:
            return value
    except ValueError:
        pass
    return None


def _mb_matches(row, album):
    return musicbrainz._matches_recording(row, album['title'], album['artist'])


def _discogs_matches(row, album):
    return (recording_title(row.get('title')) == recording_title(album['title'])
            and artist_key(album['artist']) in {
                artist_key(artist.get('name')) for artist in row.get('artists') or []})


def _rating(value, count, scale, source, url):
    if (type(value) in (int, float) and math.isfinite(value) and 0 <= value <= scale
            and type(count) is int and count > 0 and public_url(url)):
        return {'value': value, 'count': count, 'scale': scale, 'source': source, 'url': url}
    return None


def fetch(album, store):
    with net.probe_errors() as errors:
        result = _fetch(album, store)
    for error in errors:
        net.record_probe_failure(error)
    if errors:
        result['retry'] = True
        result['retry_at'] = max((getattr(error, 'retry_at', 0) for error in errors), default=0)
    return result


def _fetch(album, store):
    """One bounded release lookup; misses and partial provider outages stay explicit."""
    from yt_playlist.controllers.release_notes_acquisition import fetch as acquire
    from types import SimpleNamespace
    policy = SimpleNamespace(empty=empty, _mb_matches=_mb_matches, project_musicbrainz=project_musicbrainz,
                             select_discogs=select_discogs, project_discogs=project_discogs)

    def search(album):
        query = (f'releasegroup:"{musicbrainz._lucene_escape(album["title"])}" AND '
                 f'artist:"{musicbrainz._lucene_escape(album["artist"])}"')
        with musicbrainz.access(store):
            return musicbrainz._get('release-group', {'query': query, 'fmt': 'json', 'limit': '5'}, timeout=5)

    def recording(identity):
        with musicbrainz.access(store):
            return musicbrainz._get(f'release-group/{identity}',
                {'inc': 'artist-credits+url-rels+ratings', 'fmt': 'json'}, timeout=5)

    return acquire(policy, album, search=search, recording=recording, credential=lambda: discogs.token(store),
        releases=lambda album, tok: discogs.search_releases(artist=album['artist'], title=album['title'],
                                                         store=store, tok=tok, timeout=5),
        master=lambda identity, tok: discogs.master_details(identity, store=store, tok=tok, timeout=5),
        release=lambda identity, tok: discogs.release_details(identity, store=store, tok=tok, timeout=5))


def empty():
    return dict(links=[], rating=None, label=None, track_count=None, description=None, review=None,
                editorial_version=5, evidence=[], retry=False)


def project_musicbrainz(full, album, identity):
    result, release = {'links': [], 'evidence': []}, None
    if not _mb_matches(full, album):
        return result, release
    result['evidence'].append({'provider': 'musicbrainz', 'id': identity, 'response': full})
    for relation in full.get('relations') or []:
        link = public_url((relation.get('url') or {}).get('resource'))
        if not link:
            continue
        if relation.get('type') == 'review':
            host = urlsplit(link).hostname.removeprefix('www.')
            result['links'].append({'label': 'Pitchfork review' if host == 'pitchfork.com' else f'Review · {host}', 'url': link})
        if relation.get('type') == 'discogs':
            matched = re.fullmatch(r'https://(?:www\.)?discogs\.com/(?:[a-z]{2}/)?'
                                  r'(master|release)/(\d+)(?:-[^/?#]*)?/?', link)
            if matched:
                release = matched.groups()
    return result, release


def select_discogs(rows, album):
    for row in rows:
        artist, sep, title = (row.get('title') or '').partition(' - ')
        if (sep and artist_key(artist) == artist_key(album['artist'])
                and recording_title(title) == recording_title(album['title'])):
            return 'release', str(row.get('id', ''))
    return None


def project_discogs(previous, full, album, identity):
    result = {**previous, 'links': list(previous['links']), 'evidence': list(previous['evidence'])}
    if not _discogs_matches(full, album):
        return result
    facts = {key: full.get(key) for key in ('id', 'title', 'artists', 'year', 'labels', 'tracklist',
                                          'community', 'uri', 'notes', 'styles', 'genres')}
    result['evidence'].append({'provider': 'discogs', 'id': identity, 'response': facts})
    url = f'https://www.discogs.com/release/{identity}'
    result['links'].append({'label': 'Discogs', 'url': url})
    rating = (full.get('community') or {}).get('rating') or {}
    result['rating'] = _rating(rating.get('average'), rating.get('count'), 5, 'Discogs community', url) or result['rating']
    result['label'] = ' / '.join(dict.fromkeys(label['name'] for label in full.get('labels') or [] if label.get('name')))
    result['track_count'] = sum(row.get('type_', 'track') == 'track' for row in full.get('tracklist') or []) or None
    words = unescape(re.sub(r'<[^>]*>|\[/?[^]]*\]', '', full.get('notes') or '')).strip().split()
    if len(words) >= 8:
        result['description'] = {'text': ' '.join(words[:25]) + ('…' if len(words) > 25 else ''),
            'url': url, 'source': 'Discogs release notes', 'quote': True, 'kind': 'release_notes', 'acquisition': 'automatic'}
    return result
