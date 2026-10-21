"""Read-only network log (`/network`): the evidence side of the egress guard.

Shows the hard-coded allowlist alongside the most recent entries from the guard's
rotating log, with permitted services grouped by purpose. Enforcement lives in
:mod:`yt_playlist.egress`; this page just surfaces what it recorded.
"""
from fastapi import APIRouter, Request

from yt_playlist.egress import ALLOWED_DOMAINS, ALLOWED_REQUESTS, guard


_DOMAIN_GROUPS = (
    {
        "title": "YouTube Music",
        "description": "Read and manage your YouTube Music library and playlists.",
        "domains": {"youtube.com"},
    },
    {
        "title": "Track Metadata",
        "description": "Genres, release dates, and audio features.",
        "domains": {"musicbrainz.org", "discogs.com", "audioscrobbler.com", "last.fm",
                    "deezer.com", "acousticbrainz.org"},
    },
    {
        "title": "Artist and Genre Summaries",
        "description": "Wikipedia summaries for the Home page’s ‘Into recently’ cards.",
        "domains": {"wikipedia.org"},
    },
    {
        "title": "Cover Art",
        "description": "Deezer supplies covers; Cover Art Archive and archive.org provide fallback art. "
                       "Spotify supplies album thumbnails for import reports.",
        "domains": {"deezer.com", "coverartarchive.org", "archive.org", "spotify.com"},
    },
)


def _parse(line):
    """Turn one log line ('<ts> verdict=.. via=.. method=.. host=.. path=.. ...') into a dict.

    The message is space-separated key=value pairs (none of the values contain spaces:
    paths are percent-encoded, query strings are stripped), so a simple split is safe.
    """
    idx = line.find("verdict=")
    if idx < 0:
        return {"ts": "", "raw": line}
    row = {"ts": line[:idx].strip()}
    for tok in line[idx:].split():
        k, _, v = tok.partition("=")
        row[k] = v
    return row


def build(ctx) -> APIRouter:
    router = APIRouter()
    templates = ctx.templates

    @router.get("/network")
    def network(request: Request):
        rows = [_parse(ln) for ln in reversed(guard().recent(500))]   # newest first
        return templates.TemplateResponse(request, "network.html", {
            "allowlist_groups": [
                {**group, "domains": sorted(group["domains"] & ALLOWED_DOMAINS)}
                for group in _DOMAIN_GROUPS
            ],
            "allowed_requests": sorted(ALLOWED_REQUESTS),
            "rows": rows,
        })

    return router
