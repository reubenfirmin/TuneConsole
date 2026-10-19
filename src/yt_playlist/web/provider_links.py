"""Public metadata-provider lookups for inspecting enrichment disagreements."""
from urllib.parse import quote, urlencode


def provider_search_url(provider, title, artist, mb_recording_id=None):
    """Link older findings by artist/title; AcousticBrainz only supports recording IDs."""
    query = " ".join(part for part in (artist, title) if part)
    if provider == "musicbrainz":
        return "https://musicbrainz.org/search?" + urlencode(
            {"query": query, "type": "recording", "method": "indexed"})
    if provider == "lastfm":
        return "https://www.last.fm/search/tracks?" + urlencode({"q": query})
    if provider == "discogs":
        return "https://www.discogs.com/search/?" + urlencode({"q": query, "type": "release"})
    if provider == "deezer":
        return "https://www.deezer.com/search/" + quote(query, safe="") + "/track"
    if provider == "acousticbrainz":
        return "https://acousticbrainz.org/" + quote(mb_recording_id or "", safe="")
    return None
