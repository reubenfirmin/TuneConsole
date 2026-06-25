"""Unit tests for search_versions — the alternate-version search engine behind the playlist
'find alternate versions' modal (#26: added alternates must carry their track time)."""
from yt_playlist.library.executor import search_versions


class _FilterAwareClient:
    """Mimics real YouTube Music search: the UNFILTERED / 'videos' passes often omit duration,
    while the 'songs' pass carries duration_seconds for the same videoId."""
    def search(self, query, filter=None):
        if filter == "songs":
            return [{"videoId": "v1", "title": "Song A (Live)",
                     "artists": [{"name": "Artist X"}], "duration_seconds": 250}]
        # unfiltered (top results) and 'videos': the same track, but with NO duration
        return [{"videoId": "v1", "title": "Song A (Live)", "artists": [{"name": "Artist X"}]}]


def test_search_versions_keeps_duration_from_filtered_pass():
    # Regression for #26. search_versions de-dups candidates by videoId across its three passes
    # (unfiltered, songs, videos). The unfiltered pass runs first and claims v1 with no duration;
    # the later 'songs' pass — which DOES carry the duration — must still fill it in, otherwise the
    # alternate gets added to the playlist with a blank time.
    out = search_versions(_FilterAwareClient(), "Song A", "Artist X", exclude="v0")
    assert len(out) == 1
    assert out[0]["videoId"] == "v1"
    assert out[0]["duration"] == 250
