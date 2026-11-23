"""Keyless Wikipedia summaries for the 'into recently' Home card.

Resolve a page for an artist or genre (search, biased with a little context so 'Mercury' lands on
the band, not the planet), then fetch the REST summary. Only a real article (type 'standard' with a
non-empty extract) counts. Confirmed misses return None; request/parse failures raise
LookupUnavailable so callers can retain stale content and retry shortly.
"""
import re
import unicodedata
import urllib.parse
import urllib.error
from html import unescape

from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing
from yt_playlist.integrations.base import AccessState
from yt_playlist.integrations.wikipedia import WikipediaIAO, SEARCH, SUMMARY
from yt_playlist.providers.base import LookupUnavailable
from yt_playlist.util import genre_catalog, net
from yt_playlist.util.wikipedia_subject import matches_subject as subject_matches

name = "wikipedia"

# Words that carry no identity (added as search context, or generic): ignored when checking that a
# resolved page is actually about the subject.
_STOPWORDS = {"band", "musician", "music", "genre", "the", "and", "of", "a"}
_MUSICIAN = re.compile(r'\b(musician|musical|band|singer|songwriter|dj|composer|rapper|'
                       r'guitarist|bassist|drummer|pianist|vocalist|orchestra|'
                       r'music producer|record producer)\b', re.I)
_MAX_CANDIDATES = 5

_SEARCH, _SUMMARY = SEARCH, SUMMARY
_iao = WikipediaIAO()

_CONTEXT = {"artist": "band musician", "genre": "music genre"}
# Search disambiguation, not an equivalence in the genre catalog. The library's
# electronic "minimal" tag must not resolve to the classical composition article.
_GENRE_SEARCH_NAMES = {"minimal": "minimal techno"}


def _get_json(url):
    return _iao.get_url(url)


def matches_subject(kind, display, title):
    return subject_matches(kind, display, title, canonical_name=genre_catalog.canonical_name)


def access(store=None):
    return _iao.context(AccessState.for_store(store))


def _project(fetch, *args):
    errors = []
    try:
        with net.probe_errors() as errors:
            try:
                return fetch(*args)
            except LookupUnavailable as exc:
                exc.retry_at = max([net.retry_after(exc.__cause__, _iao.now()),
                    *(net.retry_after(error, _iao.now()) for error in errors)])
                raise
    finally:
        # Replay after leaving the inner capture, including usable stale results.
        for error in errors:
            net.record_probe_failure(error)


def _tokens(text):
    """Identity-bearing lowercased words in `text` (drops short/stopword tokens like 'band')."""
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) >= 3 and w not in _STOPWORDS]


def _search_name(kind, display):
    return _GENRE_SEARCH_NAMES.get(display.casefold(), display) if kind == 'genre' else display


def _plain_name(text):
    return ' '.join(re.findall(r'[^\W_]+', unicodedata.normalize('NFKC', text).casefold()))


def _search_titles(kind, display):
    term = f"{_search_name(kind, display)} {_CONTEXT.get(kind, '')}".strip()
    params = {"action": "query", "format": "json", "list": "search",
              "srlimit": str(_MAX_CANDIDATES), "srsearch": term}
    data = _get_json(_SEARCH + "?" + urllib.parse.urlencode(params))
    hits = data["query"]["search"]
    if not isinstance(hits, list):
        raise ValueError("Invalid Wikipedia search response")
    if any(not isinstance(h, dict) or not isinstance(h.get("title"), str) for h in hits):
        raise ValueError("Invalid Wikipedia search titles")
    return [h["title"] for h in hits if h["title"]]


def _summary(title):
    url = _SUMMARY + urllib.parse.quote(title.replace(" ", "_"), safe='') + "?redirect=true"
    try:
        return _get_json(url)
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            return {"type": "absent"}
        raise


def fetch_summary(kind, display, *, artist_genres=None):
    """Resolve a matching article; stale evidence also reports pending access."""
    return _project(_fetch_summary, kind, display, artist_genres)


def _fetch_summary(kind, display, artist_genres):
    """Return a summary or confirmed miss; raise LookupUnavailable on retryable failures.

    Story lookups pass recent recording genres to resolve same-name articles."""
    from yt_playlist.controllers.wikipedia_acquisition import summary
    from types import SimpleNamespace
    policy = SimpleNamespace(matches_subject=matches_subject, project_summary=project_summary)
    select = None if artist_genres is None else lambda candidates, ambiguous: _select_artist_summary(
        display, artist_genres, candidates, ambiguous)
    return summary(policy, _search_titles, _summary, kind, display, select=select)


def _select_artist_summary(display, genres, candidates, ambiguous):
    article = candidates[0]
    introduction = re.split(r'[.!?]\s|\n', article['extract'], maxsplit=1)[0]
    if (not ambiguous and subject_matches('artist', display, article['title'], allow_qualified_artist=False)
            and _MUSICIAN.search(introduction)):
        return {**article, 'matched_genres': []}
    if not genres:
        return None

    # A category describes the article itself. An extract can mention another
    # artist by the same name without describing that artist's genre.
    titles = [article['title'] for article in candidates]
    params = {'action': 'query', 'format': 'json', 'prop': 'categories',
              'clshow': '!hidden', 'cllimit': '200', 'titles': '|'.join(titles)}
    pages = _get_json(_SEARCH + '?' + urllib.parse.urlencode(params))['query']['pages']
    categories = {page['title']: [_plain_name(c['title'].removeprefix('Category:'))
                                 for c in page.get('categories', ())] for page in pages.values()}
    patterns = {genre: re.compile(rf'\b{re.escape(_plain_name(genre))} (?:music(?:al)? )?'
                                  r'(?:groups|bands|musicians|singers|rappers|djs)\b') for genre in genres}
    matched = []
    for article in candidates:
        labels = categories.get(article['title'], ())
        confirmed = [genre for genre, pattern in patterns.items() if any(pattern.search(label) for label in labels)]
        if confirmed:
            matched.append({**article, 'matched_genres': confirmed})
    return matched[0] if len(matched) == 1 else None


def project_summary(s, kind, display, title):
    if not isinstance(s, dict) or 'type' not in s:
        raise ValueError('Invalid Wikipedia summary response')
    extract = (s.get('extract') or '').strip()
    if s.get('title') is not None and not isinstance(s['title'], str):
        raise ValueError('Invalid Wikipedia title')
    if s.get('type') != 'standard' or not extract or not matches_subject(kind, display, s.get('title') or title):
        return None
    return dict(display=display, title=s.get('title') or title, extract=extract,
                thumbnail=(s.get('thumbnail') or {}).get('source'),
                url=((s.get('content_urls') or {}).get('desktop') or {}).get('page'))


def fetch_artwork(display):
    """Resolve attributed musician photography, retaining subject and license checks."""
    return _project(_fetch_artwork, display)


def _fetch_artwork(display):
    """Resolve a musician's lead image and its file attribution, without biography copy.

    Require a matching musician article and original file identity; unrelated
    people, disambiguation pages and unattributed files cannot become scenery.
    """
    from yt_playlist.controllers.wikipedia_acquisition import artwork
    from types import SimpleNamespace
    policy = SimpleNamespace(matches_subject=matches_subject, artwork_filename=artwork_filename,
                             project_artwork=project_artwork)
    return artwork(policy, _search_titles, _summary, _image_info, display)


def _image_info(filename):
    params = {'action': 'query', 'format': 'json', 'prop': 'imageinfo',
              'titles': 'File:' + filename, 'iiprop': 'url|extmetadata', 'iiurlwidth': '1200'}
    return _get_json(_SEARCH + '?' + urllib.parse.urlencode(params))


def artwork_filename(summary, display, title):
    if (summary.get('type') != 'standard'
            or not matches_subject('artist', display, summary.get('title') or title)):
        return None
    context = summary.get('description') or summary.get('extract') or ''
    if not _MUSICIAN.search(context):
        return None
    parsed = urllib.parse.urlsplit((summary.get('originalimage') or {}).get('source') or '')
    if parsed.scheme == 'https' and parsed.netloc == 'upload.wikimedia.org':
        return urllib.parse.unquote(parsed.path.rsplit('/', 1)[-1])
    return None


def project_artwork(data, display):
    for page in data['query']['pages'].values():
        for info in page.get('imageinfo') or []:
            meta = info.get('extmetadata') or {}
            def plain(key):
                return ' '.join(unescape(re.sub('<[^>]+>', '', (meta.get(key) or {}).get('value') or '')).split())
            author, license_name = plain('Artist'), plain('LicenseShortName')
            source, image = info.get('descriptionurl') or '', info.get('thumburl') or info.get('url') or ''
            nonfree = (meta.get('NonFree') or {}).get('value')
            if (author and license_name and str(nonfree).casefold() not in ('true', '1')
                    and 'fair use' not in license_name.casefold() and source.startswith('https://')
                    and image.startswith('https://upload.wikimedia.org/')):
                return {'url': image, 'source_url': source, 'kind': 'artist',
                        'credit': f'{display} · {author} · {license_name}'}
    return None
