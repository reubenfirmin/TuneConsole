"""Public release evidence, identity checked before any rating is attributed.

MusicBrainz release-group relationships supply review links and Discogs IDs. A
Discogs community average describes the matched edition, never a critic's score.
All calls use the existing providers' pacing, circuit breakers and egress policy.
"""
import math
import re
from html import unescape
from urllib.parse import urlsplit

from yt_playlist.core.activity import WorkClosing, WorkBudgetExceeded, check_cancellable_work
from yt_playlist.providers import discogs, musicbrainz
from yt_playlist.providers.base import LookupUnavailable
from yt_playlist.util.matching import recording_title


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
            and discogs._artist_key(album['artist']) in {
                discogs._artist_key(artist.get('name')) for artist in row.get('artists') or []})


def _rating(value, count, scale, source, url):
    if (type(value) in (int, float) and math.isfinite(value) and 0 <= value <= scale
            and type(count) is int and count > 0 and public_url(url)):
        return {'value': value, 'count': count, 'scale': scale, 'source': source, 'url': url}
    return None


def fetch(album, store):
    """One bounded release lookup; misses and partial provider outages stay explicit."""
    result = {'links': [], 'rating': None, 'label': None, 'track_count': None, 'description': None,
              'review': None, 'editorial_version': 5,
              'evidence': [], 'retry': False}
    if not album.get('title') or not album.get('artist'):
        return result
    query = (f'releasegroup:"{musicbrainz._lucene_escape(album["title"])}" AND '
             f'artist:"{musicbrainz._lucene_escape(album["artist"])}"')
    release = None
    try:
        if musicbrainz.tripped():
            raise LookupUnavailable('MusicBrainz is backing off')
        check_cancellable_work()
        search = musicbrainz._get('release-group', {'query': query, 'fmt': 'json', 'limit': '5'}, timeout=5)
        matches = [row for row in search.get('release-groups') or [] if _mb_matches(row, album)]
        if matches:
            mbid = matches[0].get('id', '')
            if re.fullmatch(r'[0-9a-f-]{36}', mbid):
                check_cancellable_work()
                full = musicbrainz._get(f'release-group/{mbid}',
                    {'inc': 'artist-credits+url-rels+ratings', 'fmt': 'json'}, timeout=5)
                if _mb_matches(full, album):
                    result['evidence'].append({'provider': 'musicbrainz', 'id': mbid, 'response': full})
                    for relation in full.get('relations') or []:
                        link = public_url((relation.get('url') or {}).get('resource'))
                        if not link:
                            continue
                        if relation.get('type') == 'review':
                            host = urlsplit(link).hostname.removeprefix('www.')
                            result['links'].append({'label': 'Pitchfork review' if host == 'pitchfork.com'
                                                    else f'Review · {host}', 'url': link})
                        if relation.get('type') == 'discogs':
                            matched = re.fullmatch(r'https://(?:www\.)?discogs\.com/(?:[a-z]{2}/)?'
                                                   r'(master|release)/(\d+)(?:-[^/?#]*)?/?', link)
                            if matched:
                                release = matched.groups()
    except (WorkClosing, WorkBudgetExceeded):
        raise
    except Exception:
        result['retry'] = True

    tok = discogs.token(store)
    try:
        if discogs.tripped():
            raise LookupUnavailable('Discogs is backing off')
        # Search requires authentication. Linked public release/master lookups do not.
        if release is None and tok:
            check_cancellable_work()
            search = discogs._get('database/search', {'artist': album['artist'],
                'release_title': album['title'], 'type': 'release', 'per_page': '5'}, tok, timeout=5)
            for row in search.get('results') or []:
                artist, sep, title = (row.get('title') or '').partition(' - ')
                if (sep and discogs._artist_key(artist) == discogs._artist_key(album['artist'])
                        and recording_title(title) == recording_title(album['title'])):
                    release = ('release', str(row.get('id', '')))
                    break
        if release and release[1].isdigit():
            kind, identity = release
            check_cancellable_work()
            if kind == 'master':
                master = discogs._get(f'masters/{identity}', {}, tok, timeout=5)
                identity = str(master.get('main_release') or '')
            if identity.isdigit():
                check_cancellable_work()
                full = discogs._get(f'releases/{identity}', {}, tok, timeout=5)
                if _discogs_matches(full, album):
                    # Keep public facts only; authenticated user_data is not release evidence.
                    facts = {key: full.get(key) for key in ('id', 'title', 'artists', 'year',
                             'labels', 'tracklist', 'community', 'uri', 'notes', 'styles', 'genres')}
                    result['evidence'].append({'provider': 'discogs', 'id': identity, 'response': facts})
                    url = f'https://www.discogs.com/release/{identity}'
                    result['links'].append({'label': 'Discogs', 'url': url})
                    rating = (full.get('community') or {}).get('rating') or {}
                    result['rating'] = (_rating(rating.get('average'), rating.get('count'), 5,
                                                'Discogs community', url) or result['rating'])
                    result['label'] = ' / '.join(dict.fromkeys(
                        label['name'] for label in full.get('labels') or [] if label.get('name')))
                    result['track_count'] = sum(row.get('type_', 'track') == 'track'
                                                for row in full.get('tracklist') or []) or None
                    # A bounded, attributed contributor excerpt, never a critic's review.
                    prose = unescape(re.sub(r'<[^>]*>|\[/?[^]]*\]', '', full.get('notes') or '')).strip()
                    words = prose.split()
                    if len(words) >= 8:
                        result['description'] = {'text': ' '.join(words[:25]) + ('…' if len(words) > 25 else ''),
                            'url': url, 'source': 'Discogs release notes', 'quote': True,
                            'kind': 'release_notes', 'acquisition': 'automatic'}
    except (WorkClosing, WorkBudgetExceeded):
        raise
    except Exception:
        result['retry'] = True
    return result
