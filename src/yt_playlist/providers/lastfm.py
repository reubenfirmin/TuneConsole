"""Last.fm enrichment: pin a clean genre from a track's (or artist's) top tags.

Last.fm has dense, crowd-sourced tags where MusicBrainz genres are sparse, but the tags are noisy
(moods, decades, "seen live"...). We match them against genres.py's whitelist and take the highest-
count tag that is a recognized genre. Year comes from a year tag or the album page's Release Date.
Matching stays here; credentials, transport, caching and retry admission belong to LastFmIAO.

Needs a (free) Last.fm API key: set $LASTFM_API_KEY, or `lastfm_api_key = "..."` in config.toml.
"""
import logging
import re
from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing
from yt_playlist.core import provider_queries
from yt_playlist.integrations.base import CredentialedAccessState, IntegrationUnavailable
from yt_playlist.controllers.access_configuration import AccessConfiguration, resolve
from yt_playlist.integrations.lastfm import LastFmIAO, TIMEOUT_S
from yt_playlist.providers import descriptive_tags, genres
from yt_playlist.util.matching import is_remix, normalize, recording_title
from yt_playlist.providers.base import EnrichmentResult
from yt_playlist.util import net

name = "lastfm"

logger = logging.getLogger(__name__)

_iao = LastFmIAO()


def access(store=None):
    source = AccessConfiguration(getattr(store, 'settings', None)).credential_source('LASTFM_API_KEY', 'lastfm_api_key')
    return _iao.context(CredentialedAccessState.for_store(store, source))


def api_key(store=None, config_path=None):
    """Resolve the Last.fm API key: $LASTFM_API_KEY, then the value saved via the UI (settings
    table), then `lastfm_api_key` in config.toml. Returns None if none is set."""
    if store is None and config_path is None and _iao._scope.get() is not None:
        return _iao.api_key()
    return resolve(env_name='LASTFM_API_KEY', setting_name='lastfm_api_key', store=store, config_path=config_path)


def available(store=None) -> bool:
    return api_key(store) is not None


def tripped() -> bool:
    return not _iao.availability().ready(_iao.now())


def probe(track, store=None, key=None, *, matcher=None) -> EnrichmentResult:
    """Read-only lookup: genre (and a year-ish tag/page year). Empty if no API key."""
    reason = provider_queries.deferred_reason(name, track)
    if reason:
        return EnrichmentResult(name, deferred_reason=reason)
    key = key or api_key(store)
    if not key:
        return EnrichmentResult("lastfm", {})
    if matcher is None and store is not None:
        matcher = genres.snapshot(store)
    observations = []
    credits = []
    with access(store):
        genre, year = enrich(track["title"], track["artist"], key, observations=observations, matcher=matcher, credits=credits)
    fields = {}
    if credits:
        fields['artist_credits'] = credits
    if genre:
        fields["genre"] = genre
    if year:
        fields["year"] = year
    return EnrichmentResult("lastfm", fields, tuple(observations))


def _get(params, *, timeout=TIMEOUT_S):
    return _iao.get(params, timeout=timeout)


def _fetch_text(url):
    return _iao.page(url)


def _record_failure(exc):
    # The IAO already records typed failures, including stale response reuse.
    if not isinstance(exc, IntegrationUnavailable):
        net.record_probe_failure(exc)


def _page_release_year(html):
    """Pull the release year from a Last.fm track page's 'Release Date' metadata field."""
    m = re.search(r"Release Date\s*</dt>\s*<dd[^>]*>([^<]+)", html or "")
    if not m:
        return None
    ym = re.search(r"\b(1[89]\d\d|20\d\d)\b", m.group(1))
    return ym.group(1) if ym else None


def _tag_names(payload):
    """Tag names from a *.getTopTags response, highest-count first (the API already sorts them)."""
    tags = ((payload or {}).get("toptags") or {}).get("tag") or []
    if isinstance(tags, dict):                # the API returns a bare object for a single tag
        tags = [tags]
    return [t.get("name", "") for t in tags if t.get("name")]


def _tags(payload):
    tags = ((payload or {}).get('toptags') or {}).get('tag') or []
    return [tags] if isinstance(tags, dict) else tags


def _year_from_tags(names):
    """Last.fm tags often include the release year (e.g. '1979'). Return the first plausible one."""
    for name in names:
        n = name.strip()
        if len(n) == 4 and n.isdigit() and 1900 <= int(n) <= 2099:
            return n
    return None


def artist_genre(artist, key, *, matcher=None):
    """Best whitelisted genre from an artist's Last.fm top tags, or None. For #18: tagging a new
    (unowned) discovered artist so the facet overlay can steer it. Best-effort: None on any failure."""
    from yt_playlist.controllers.provider_acquisition import lastfm_artist_genre
    from types import SimpleNamespace
    policy = SimpleNamespace(_record_failure=_record_failure, logger=logger, genres=genres,
                             _tag_names=_tag_names)
    return lastfm_artist_genre(policy, _get, artist, key, matcher=matcher)


def enrich(title, artist, key, *, observations=None, matcher=None, credits=None):
    """Return (genre, year) from Last.fm. One track.getInfo gives the tags (genre, via the whitelist)
    and the canonical page URL; the release year is read off that page's metadata (Last.fm exposes it
    as primary metadata, not in the API). Genre falls back to artist tags; year to a year-like tag."""
    from types import SimpleNamespace
    from yt_playlist.controllers.provider_acquisition import lastfm_enrich
    def project_with_credit(*args, **kwargs):
        result = project(*args, **kwargs)
        if credits is not None and result['match']['title_artist_match']:
            from yt_playlist.providers.artist_credits import entities
            credits.extend(entities([result['credit']], 'musicbrainz', identity='mbid'))
        return result
    policy = SimpleNamespace(_record_failure=_record_failure, logger=logger, project=project_with_credit,
                             project_artist=project_artist, _page_release_year=_page_release_year)
    return lastfm_enrich(policy, _get, _fetch_text, title, artist, key, observations=observations, matcher=matcher)


def project(info, title, artist, *, observations=None, matcher=None):
    """Project supplied track information and attributable matching evidence."""
    track = (info or {}).get("track") or {}
    credit = track.get('artist') or {}
    credit = credit if isinstance(credit, dict) else {'name': credit}
    match = {'method': 'name_query', 'input_title': title, 'input_artist': artist,
             'returned_title': track.get('name'), 'returned_artist': credit.get('name'),
             'title_artist_match': bool(recording_title(track.get('name')) == recording_title(title)
                                       and normalize(credit.get('name')) == normalize(artist)
                                       and normalize(artist))}
    descriptive_tags.retain(observations, scope='recording',
        entity_id=track.get('mbid') or track.get('url'), tags=_tags(track),
        source_path='track.getinfo:track.toptags', match_evidence=match)
    track_tags = _tag_names(track)   # _tag_names guards the single-tag-as-dict case the API returns
    return {'genre': genres.pick_genre(track_tags, matcher), 'year': _year_from_tags(track_tags),
            'album_url': (track.get('album') or {}).get('url'), 'match': match, 'credit': credit}


def project_artist(tags, track, *, observations=None, matcher=None):
    if track['match']['title_artist_match']:
        descriptive_tags.retain(observations, scope='artist',
            entity_id=track['credit'].get('mbid') or track['credit'].get('url'), tags=_tags(tags),
            source_path='artist.gettoptags:toptags', match_evidence=track['match'])
    return genres.pick_genre(_tag_names(tags), matcher)


def tag_top_artists(tag, key, limit=25):
    """Last.fm tag.getTopArtists -> [artist_name], most-listened first. [] on error/no key.

    The ranking is what makes this useful: "who are the big alternative rock artists" is a question
    Last.fm's own listening data answers well, and far better than a text search for the genre name.
    """
    if not tag or not key:
        return []
    try:
        data = _get({"method": "tag.getTopArtists", "tag": tag, "api_key": key,
                     "format": "json", "limit": limit})
    except (WorkClosing, WorkBudgetExceeded):
        raise
    except Exception as e:  # noqa: BLE001 - retry state accompanies an unavailable result
        _record_failure(e)
        logger.warning("Last.fm tag.getTopArtists failed for %r: %s", tag, e)
        return []
    out = []
    for a in _artists(data.get("topartists")):
        nm = (a.get("name") or "").strip()
        if nm and nm not in out:
            out.append(nm)
    return out


def similar_artists(name, key, limit=50):
    """Last.fm artist.getSimilar -> [(artist_name, match_0_to_1)], most similar first. [] on error."""
    if not name or not key:
        return []
    try:
        data = _get({"method": "artist.getSimilar", "artist": name, "api_key": key,
                     "format": "json", "limit": limit, "autocorrect": 1})
    except (WorkClosing, WorkBudgetExceeded):
        raise
    except Exception as e:  # noqa: BLE001
        _record_failure(e)
        return []
    out = []
    for a in _artists(data.get("similarartists")):
        nm = (a.get("name") or "").strip()
        try:
            match = float(a.get("match") or 0.0)
        except (TypeError, ValueError):
            match = 0.0
        if nm:
            out.append((nm, match))
    return out


def _artists(payload):
    rows = (payload or {}).get('artist') or []
    return [rows] if isinstance(rows, dict) else rows
