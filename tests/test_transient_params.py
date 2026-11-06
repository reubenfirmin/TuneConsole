import pytest

from yt_playlist.rec import actions, rec_params, transient
from yt_playlist.util.matching import identity_key


def _store_with_play_history(s, genre="Techno", n=10, now=1000.0):
    """A fresh store whose recent history is n distinct tracks of one genre (a dominant family).
    History dedupes by key, so distinctness (not repeats) is what builds a facet's play lean."""
    iid = s.accounts.upsert_account("me", "cred", None, True)
    keys = []
    for i in range(n):
        tid = s.tracks.upsert_track(f"v{i}", f"song{i}", "band", None, None)
        s.tracks.set_track_genre(tid, genre)
        keys.append(identity_key(f"song{i}", "band"))
    s.history.record_history_plays(iid, now, keys, source_timezone="UTC")
    return s, now


def test_sync_status_uses_shared_stale_duration():
    assert rec_params.SYNC_STALE_S == 24 * 3600
    assert actions.SYNC_STALE_S == rec_params.SYNC_STALE_S      # status and parameter registry use the same duration


def test_play_facet_leans_isolates_plays(store):
    # Recent history dominated by one genre -> that family carries the largest play lean.
    s, now = _store_with_play_history(store, "Techno", n=10)
    leans = transient.play_facet_leans(s, now)
    assert leans, "expected at least one played facet"
    assert all(v > 0 for v in leans.values()), "plays only ever push positive"
    top = max(leans, key=leans.get)
    assert top.startswith("genre:")
    assert leans[top] == pytest.approx(3.0)
    rec_params.set_param(s, "play_transient_w", .8)
    rec_params.set_param(s, "recent_play_limit", 5)
    assert transient.play_facet_leans(s, now)[top] == pytest.approx(4.0)
    rec_params.reset_param(s, "play_transient_w")
    rec_params.reset_param(s, "recent_play_limit")
    assert transient.play_facet_leans(s, now) == leans
