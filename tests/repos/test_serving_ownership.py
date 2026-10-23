"""Reporting detaches its inputs; prepared output commits across named owners."""
import sqlite3

import pytest

from yt_playlist.core.store import Store
from yt_playlist.rec import scorecard


def test_scorecard_inputs_remain_usable_after_all_connections_close(tmp_path):
    store = Store(tmp_path / "report.db")
    store.init_schema()
    try:
        iid = store.identities.upsert_identity("main", "credential", None, True)
        store.tracks.upsert_track("video", "Song", "Artist", None, 200)
        store.rec_ledger.record_served_impressions("shown", "home", [{
            "key": "song|artist", "rank": 1,
            "provenance": {"candidate": {"video_id": "video"}},
        }], 100)
        store.history.record_play_event(iid, "song|artist", "video", 120)
        inputs = store.rec_ledger.scorecard_inputs(90, 110)
        expected = scorecard.recommendation_scorecard(store, 90, 110)
    finally:
        store.conn.close()
    assert scorecard.summarize(inputs, 90, 110) == expected
    assert expected["played"] == 1


@pytest.mark.parametrize("fail", [False, True])
def test_proposals_policies_and_settings_share_the_outer_publication(tmp_path, fail):
    store = Store(tmp_path / "publish.db")
    store.init_schema()
    try:
        store.rec_proposals.put_proposals("mode_bundles", {"generation": 1}, 1)
        store.rec_proposals.put_proposals("fresh_pool", ["old"], 1)
        store.settings.set_setting("publication-marker", "old")
        with store.read_snapshot() as snapshot:
            for owner in ("rec_ledger", "rec_proposals", "rec_signals",
                          "playlist_recipes", "artist_similarity"):
                repo = getattr(snapshot.store, owner)
                assert repo.conn is snapshot.store.conn
                assert repo._lock is snapshot.store._lock

            def publish():
                store.rec_ledger.publish_prepared_policies([("new-policy", "{}", 2)])
                store.rec_proposals.publish_prepared_bundles('{"generation":2}', '["new"]', 2)
                store.settings.publish_values({"publication-marker": "new"})
                if fail:
                    raise sqlite3.IntegrityError("later owner failed")

            if fail:
                with pytest.raises(sqlite3.IntegrityError, match="later owner failed"):
                    snapshot.publish(publish)
            else:
                assert snapshot.publish(publish)
        assert not store.conn.in_transaction
        assert store.rec_proposals.get_proposals("mode_bundles") == {"generation": 1 if fail else 2}
        assert store.rec_proposals.get_proposals("fresh_pool") == ["old" if fail else "new"]
        assert store.settings.get_setting("publication-marker") == ("old" if fail else "new")
        assert bool(store.conn.execute("SELECT 1 FROM rec_serving_policies").fetchone()) is not fail
    finally:
        store.conn.close()
