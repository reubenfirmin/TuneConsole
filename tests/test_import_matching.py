from yt_playlist.library.importing import resolve_track
from yt_playlist.providers.spotify import ImportTrack


class Target:
    def __init__(self, rows): self.rows = rows
    def search(self, query, filter="songs"): return self.rows


SOURCE = ImportTrack("sp", "Time Zero", "Artist", "Album", 200)


def _row(video, title="Time Zero", artist="Artist", duration=200):
    return {"videoId": video, "title": title, "artists": [{"name": artist}],
            "duration_seconds": duration}


def test_duration_supported_match_is_accepted():
    result = resolve_track(Target([_row("right")]), SOURCE)
    assert result.status == "matched" and result.target_video_id == "right"


def test_two_equally_convincing_versions_are_ambiguous():
    result = resolve_track(Target([_row("one"), _row("two")]), SOURCE)
    assert result.status == "ambiguous" and result.target_video_id is None


def test_similar_wrong_duration_is_unmatched():
    result = resolve_track(Target([_row("wrong", title="Time Zero Extended", duration=420)]), SOURCE)
    assert result.status == "unmatched"
