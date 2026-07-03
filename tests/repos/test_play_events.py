"""#75 live play events: timestamped, identity-attributed play stream from the extension.
Consecutive re-reports of the same track (likeStatus changes) merge; replays after the
dedup window, or with another track in between, are new events."""


def test_record_and_read_back(store):
    iid = store.upsert_identity("me", "c", None, True)
    assert store.record_play_event(iid, "song|artist", "v1", 1000.0,
                               playlist_ytm_id="PL123", like_status="INDIFFERENT") is True
    evs = store.play_events_since(0)
    assert len(evs) == 1
    e = evs[0]
    assert e["identity_id"] == iid and e["identity_key"] == "song|artist"
    assert e["video_id"] == "v1" and e["played_at"] == 1000.0
    assert e["playlist_ytm_id"] == "PL123" and e["like_status"] == "INDIFFERENT"


def test_same_track_rereport_merges_and_updates_like_status(store):
    # content.js re-reports the same track when likeStatus changes; that is one play, not two
    iid = store.upsert_identity("me", "c", None, True)
    assert store.record_play_event(iid, "song|artist", "v1", 1000.0, like_status="INDIFFERENT") is True
    assert store.record_play_event(iid, "song|artist", "v1", 1030.0, like_status="LIKE") is False
    evs = store.play_events_since(0)
    assert len(evs) == 1
    assert evs[0]["like_status"] == "LIKE"
    assert evs[0]["played_at"] == 1000.0          # merge keeps the original play time


def test_replay_after_window_is_a_new_event(store):
    iid = store.upsert_identity("me", "c", None, True)
    store.record_play_event(iid, "song|artist", "v1", 1000.0)
    assert store.record_play_event(iid, "song|artist", "v1", 1000.0 + 1801) is True
    assert len(store.play_events_since(0)) == 2


def test_a_b_a_records_three_events(store):
    iid = store.upsert_identity("me", "c", None, True)
    assert store.record_play_event(iid, "a|x", "v1", 1000.0) is True
    assert store.record_play_event(iid, "b|y", "v2", 1200.0) is True
    assert store.record_play_event(iid, "a|x", "v1", 1400.0) is True
    assert len(store.play_events_since(0)) == 3


def test_merge_backfills_playlist_but_never_clears_it(store):
    iid = store.upsert_identity("me", "c", None, True)
    store.record_play_event(iid, "a|x", "v1", 1000.0, playlist_ytm_id=None)
    store.record_play_event(iid, "a|x", "v1", 1010.0, playlist_ytm_id="PL9")
    assert store.play_events_since(0)[0]["playlist_ytm_id"] == "PL9"
    store.record_play_event(iid, "a|x", "v1", 1020.0, playlist_ytm_id=None)
    assert store.play_events_since(0)[0]["playlist_ytm_id"] == "PL9"


def test_since_filter_and_ordering(store):
    iid = store.upsert_identity("me", "c", None, True)
    store.record_play_event(iid, "a|x", "v1", 1000.0)
    store.record_play_event(iid, "b|y", "v2", 2000.0)
    evs = store.play_events_since(1500.0)
    assert [e["identity_key"] for e in evs] == ["b|y"]


def test_identities_are_independent_for_dedup(store):
    # the merge check is per identity: another identity playing the same track is its own event
    iid1 = store.upsert_identity("me1", "c", None, True)
    iid2 = store.upsert_identity("me2", "c", None, True)
    assert store.record_play_event(iid1, "a|x", "v1", 1000.0) is True
    assert store.record_play_event(iid2, "a|x", "v1", 1010.0) is True
    assert len(store.play_events_since(0)) == 2
