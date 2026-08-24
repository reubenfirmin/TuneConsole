from yt_playlist.library.importing import existing_overlaps, ImportResolution, resolve_track
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


def test_existing_overlap_is_scoped_to_target_and_exact_requires_full_resolution(store):
    i1 = store.upsert_identity("one", "c", None, True)
    i2 = store.upsert_identity("two", "c", None, False)
    tid = store.upsert_track("yt", "Time Zero", "Artist", None, 200)
    p1 = store.upsert_playlist(i1, "P1", "Existing", 1, "h", 1)
    p2 = store.upsert_playlist(i2, "P2", "Other identity", 1, "h", 1)
    store.set_playlist_tracks(p1, [tid]); store.set_playlist_tracks(p2, [tid])
    matched = ImportResolution(SOURCE, "matched", "yt", "Time Zero", "Artist", 1.0)
    overlaps = existing_overlaps(store, i1, [matched])
    assert [(o.title, o.exact) for o in overlaps] == [("Existing", True)]
    partial = ImportResolution(SOURCE, "unmatched", None, None, None, None)
    assert existing_overlaps(store, i1, [partial])[0].exact is False
