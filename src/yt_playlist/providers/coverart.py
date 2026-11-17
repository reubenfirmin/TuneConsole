"""Cover Art Archive: a front cover for a MusicBrainz RELEASE.

Not a Provider. It is the fallback source for cover art, used only when the primary (Deezer) had
none, so it never runs for a track that already has a cover. CAA is keyed by release, not recording,
and the release ids ride along in the recording search MusicBrainz already makes, so resolving one
costs no extra MusicBrainz traffic.

Coverage is patchy (a release often has no art at all, which is a clean 404, not an outage), so we
walk a few candidate releases before giving up. Results are shared across tracks from the same
release; transient errors get a short, increasing retry delay rather than one request per track.
"""
from dataclasses import dataclass

from yt_playlist.integrations.base import AccessState
from yt_playlist.integrations.coverartarchive import CoverArtArchiveIAO, API, ERROR_RETRY
_MAX_RELEASES = 4          # candidates to try before accepting that there is no art
_iao = CoverArtArchiveIAO()


@dataclass(frozen=True)
class CoverResult:
    status: str                         # found, absent, retry
    url: str | None = None
    retry_in: float = 0
    error: str = ""


def access(store=None):
    return _iao.context(AccessState.for_store(store))


def tripped() -> bool:
    return not _iao.availability().ready(_iao.now())


def _get_json(url):
    prefix = API + '/release/'
    if not url.startswith(prefix):
        raise ValueError('Unsupported Cover Art Archive resource')
    return _iao.release(url.removeprefix(prefix))


def _front_url(data):
    for img in (data.get("images") or []):
        if not img.get("front"):
            continue
        # The original image can be several MB; a sized thumbnail is enough for a table or mosaic.
        thumbs = img.get("thumbnails") or {}
        url = thumbs.get("500") or thumbs.get("large") or thumbs.get("250") or img.get("image")
        if url:
            return url.replace("http://", "https://", 1) if url.startswith("http://") else url
    return None


def _release_cover(rid):
    return _front_url(_get_json(f'{API}/release/{rid}'))


def front_cover(release_ids):
    """First cover from up to four distinct releases, including cached hits and misses.

    A host outage defers uncached editions; cached covers remain available. A
    fresh selected cover completes demand even if a previous edition failed.
    """
    from yt_playlist.controllers.cover_art import front_cover as acquire
    return acquire(release_ids, _release_cover, maximum=_MAX_RELEASES)


def lookup(release_ids):
    """Retain the distinction between a confirmed miss and a deferred archive request."""
    from yt_playlist.controllers.cover_art import lookup as acquire
    return acquire(release_ids, front_cover, _iao.now, CoverResult, ERROR_RETRY)
