"""Bounded provider acquisition; provider policies consume returned envelopes."""
import urllib.parse

from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing
from yt_playlist.integrations.base import IntegrationUnavailable
from yt_playlist.util.matching import recording_title, normalize, is_remix


def musicbrainz_search(policy, search, title, artist):
    recs = [row for row in search(title, artist) if policy._matches_recording(row, title, artist)]
    if not recs:
        stripped = policy._strip_parens(title)
        if stripped and stripped != title:
            recs = [row for row in search(stripped, artist) if policy._matches_recording(row, title, artist)]
    return recs


def deezer_lookup(policy, get, title, artist, *, duration=None, isrc=None):
    empty = policy._empty(policy._FIELDS + policy._EXTRA_FIELDS)
    if not recording_title(title) or not normalize(artist):
        return empty
    query = ' '.join(part for part in (f'artist:"{artist}"' if artist else '',
                                       f'track:"{title}"' if title else '') if part)
    try:
        response = get(policy._API + '/search/track?' + urllib.parse.urlencode({'q': query, 'limit': '10'}))
        if not isinstance(response, dict) or response.get('error'):
            return empty
        candidates = response.get('data') or []
        details = {}
        for candidate in candidates[:10]:
            identity = candidate.get('id')
            if identity is None or identity in details or policy._title(candidate) != recording_title(title):
                continue
            detail = get(policy._API + f'/track/{identity}')
            if not isinstance(detail, dict) or detail.get('error'):
                return empty
            details[identity] = detail
        track = policy._select_track(candidates, title, artist, duration, isrc, details=details)
    except (WorkClosing, WorkBudgetExceeded):
        raise
    except Exception as error:
        policy.logger.warning('Deezer track acquisition failed: %s', error)
        return empty
    if track is None:
        return empty
    album = {}
    album_id = (track.get('album') or {}).get('id')
    if album_id is not None:
        try:
            album = get(policy._API + f'/album/{album_id}')
        except (WorkClosing, WorkBudgetExceeded):
            raise
        except Exception as error:
            policy.logger.info('Deezer album lookup failed for %s: %s', album_id, error)
    return policy.project(track, album)


def discogs_enrich(policy, search, details, title, artist, tok, *, observations=None, matcher=None, credits=None):
    query = ' '.join(value for value in (artist, title) if value).strip()
    results = []
    artist_credits = []
    try:
        for candidate in search(query, tok):
            release = None
            if not policy._matches_release(candidate, title, artist):
                if not policy.needs_tracklist(candidate, title, artist):
                    continue
                try:
                    release = details(int(candidate['id']), tok=tok) or {}
                except (WorkClosing, WorkBudgetExceeded):
                    raise
                except Exception as error:
                    policy._record_failure(error)
                    continue
            if policy._matches_release(candidate, title, artist, release=release):
                results.append(candidate)
                if credits is not None:
                    from yt_playlist.providers.artist_credits import discogs
                    if release is None and candidate.get('id'):
                        try:
                            release = details(int(candidate['id']), tok=tok) or {}
                        except (WorkClosing, WorkBudgetExceeded):
                            raise
                        except Exception as error:
                            policy._record_failure(error)
                    artist_credits.append(discogs(release or {}, title))
    except (WorkClosing, WorkBudgetExceeded):
        raise
    except Exception as error:
        policy._record_failure(error)
        policy.logger.warning('Discogs search failed for %r / %r: %s', title, artist, error)
        return None, None
    if credits is not None:
        from yt_playlist.providers.artist_credits import consensus
        credits.extend(consensus(artist_credits))
    return policy.project(results, title, artist, observations=observations, matcher=matcher)


def musicbrainz_genre_year(policy, get, recordings, *, title='', observations=None, matcher=None):
    if not recordings:
        return None, None
    rec = recordings[0]
    full, artist = {}, {}
    mbid = rec.get('id')
    if mbid:
        try:
            full = get(f'recording/{mbid}', {'inc': 'genres+tags+releases+isrcs', 'fmt': 'json'})
        except IntegrationUnavailable:
            return policy.project(recordings, full, artist, observations=observations, matcher=matcher)
        except (WorkClosing, WorkBudgetExceeded):
            raise
        except Exception as error:
            policy.logger.debug('MB lookup failed for %s: %s', mbid, error)
    genre = policy._top_label(full, matcher)
    credit = rec.get('artist-credit') or []
    first = credit[0] if credit else None
    artist_id = (first.get('artist') or {}).get('id') if isinstance(first, dict) else None
    if not genre and not is_remix(title or rec.get('title')) and artist_id:
        try:
            artist = get(f'artist/{artist_id}', {'inc': 'genres+tags', 'fmt': 'json'})
        except (WorkClosing, WorkBudgetExceeded):
            raise
        except Exception as error:
            policy.logger.debug('MB artist lookup failed for %s: %s', artist_id, error)
    # The controller supplies the detail's ISRCs to the outer result; the pure
    # projection never mutates its search envelope.
    if full.get('isrcs'):
        rec['isrcs'] = list(full['isrcs'])
    return policy.project(recordings, full, artist, observations=observations, matcher=matcher)


def lastfm_enrich(policy, get, page, title, artist, key, *, observations=None, matcher=None):
    from yt_playlist.core import provider_queries
    if not provider_queries.lastfm_ready(title, artist):
        return None, None
    common = {'api_key': key, 'format': 'json', 'autocorrect': '1'}
    try:
        info = get({'method': 'track.getinfo', 'artist': artist, 'track': title, **common})
    except (WorkClosing, WorkBudgetExceeded):
        raise
    except Exception as error:
        policy._record_failure(error)
        policy.logger.warning('Last.fm getInfo failed for %r / %r: %s', title, artist, error)
        return None, None
    result = policy.project(info, title, artist, observations=observations, matcher=matcher)
    genre, year = result['genre'], result['year']
    if not genre and artist and not is_remix(title):
        try:
            tags = get({'method': 'artist.gettoptags', 'artist': artist, **common})
            genre = policy.project_artist(tags, result, observations=observations, matcher=matcher)
        except (WorkClosing, WorkBudgetExceeded):
            raise
        except Exception as error:
            policy._record_failure(error)
            policy.logger.warning('Last.fm artist tags failed for %r: %s', artist, error)
    if not year and result['album_url']:
        try:
            year = policy._page_release_year(page(result['album_url']))
        except (WorkClosing, WorkBudgetExceeded):
            raise
        except Exception as error:
            policy._record_failure(error)
            policy.logger.debug('Last.fm album page unavailable: %s', error)
    return genre, year


def acousticbrainz_read(mbid, read, record_failure, logger):
    """Acquire both bounded endpoints independently; retain either successful half."""
    result = []
    for endpoint in ('low-level', 'high-level'):
        data = {}
        if mbid:
            try:
                data = read(mbid, endpoint)
            except (WorkClosing, WorkBudgetExceeded):
                raise
            except Exception as error:
                record_failure(error)
                logger.debug('AcousticBrainz %s miss for %s: %s', endpoint, mbid, error)
        result.append(data)
    return tuple(result)


def lastfm_artist_genre(policy, get, artist, key, *, matcher=None):
    if not key or not artist:
        return None
    try:
        tags = get({'method': 'artist.gettoptags', 'artist': artist, 'api_key': key,
                    'format': 'json', 'autocorrect': '1'})
    except (WorkClosing, WorkBudgetExceeded):
        raise
    except Exception as error:
        policy._record_failure(error)
        policy.logger.warning('Last.fm artist top tags failed for %r: %s', artist, error)
        return None
    return policy.genres.pick_genre(policy._tag_names(tags), matcher)
