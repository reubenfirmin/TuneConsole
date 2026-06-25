"""EnrichmentRepo: log rows, conflict upsert/reopen rule, resolution overwrite, scoped queries."""
import json


def _track(store, title="Hyperballad", artist="Bjork", genre=None, year=None):
    tid = store.upsert_track("v_" + title, title, artist, None, 200)
    if genre or year:
        store.set_track_enrichment(tid, genre, year)
    return tid


def test_log_enrichment_rows(store):
    tid = _track(store)
    store.log_enrichment(tid, "run1", "musicbrainz", "genre", "Electronic", now=1.0)
    store.log_enrichment(tid, "run1", "discogs", "genre", "Art Pop", now=1.0)
    rows = store.conn.execute("SELECT provider, field, value FROM enrichment_log WHERE track_id=? "
                              "ORDER BY id", (tid,)).fetchall()
    assert [(r["provider"], r["field"], r["value"]) for r in rows] == [
        ("musicbrainz", "genre", "Electronic"), ("discogs", "genre", "Art Pop")]


def test_upsert_conflict_then_resolve_overwrites_column(store):
    tid = _track(store, genre="Electronic")
    cands = [{"provider": "musicbrainz", "value": "Electronic"},
             {"provider": "discogs", "value": "Art Pop"}]
    store.upsert_conflict(tid, "genre", cands)
    assert store.conflict_count_for_playlist  # method exists
    store.resolve_conflict(tid, "genre", "Art Pop")
    # canonical column overwritten...
    assert store.conn.execute("SELECT genre FROM tracks WHERE id=?", (tid,)).fetchone()["genre"] == "Art Pop"
    # ...and the conflict marked resolved
    row = store.conn.execute("SELECT resolved, resolved_value FROM enrichment_conflict "
                             "WHERE track_id=? AND field=?", (tid, "genre")).fetchone()
    assert row["resolved"] == 1 and row["resolved_value"] == "Art Pop"


def test_resolved_conflict_stays_resolved_on_same_rerun(store):
    tid = _track(store)
    cands = [{"provider": "musicbrainz", "value": "Electronic"},
             {"provider": "discogs", "value": "Art Pop"}]
    store.upsert_conflict(tid, "genre", cands)
    store.resolve_conflict(tid, "genre", "Art Pop")
    store.upsert_conflict(tid, "genre", cands)            # same candidates again
    assert store.conn.execute("SELECT resolved FROM enrichment_conflict WHERE track_id=? AND field=?",
                              (tid, "genre")).fetchone()["resolved"] == 1


def test_resolved_conflict_reopens_on_new_value(store):
    tid = _track(store)
    store.upsert_conflict(tid, "genre", [{"provider": "musicbrainz", "value": "Electronic"},
                                         {"provider": "discogs", "value": "Art Pop"}])
    store.resolve_conflict(tid, "genre", "Art Pop")
    store.upsert_conflict(tid, "genre", [{"provider": "musicbrainz", "value": "Electronic"},
                                         {"provider": "lastfm", "value": "Trip Hop"}])  # new option
    assert store.conn.execute("SELECT resolved FROM enrichment_conflict WHERE track_id=? AND field=?",
                              (tid, "genre")).fetchone()["resolved"] == 0


def test_conflict_count_and_list_scoped_to_playlist(store):
    iid = store.upsert_identity("main", "cred", None, True)
    tid = _track(store, title="A")
    other = _track(store, title="B")
    plid = store.upsert_playlist(iid, "PL", "Mix", 1, "h", 1000.0)
    store.set_playlist_tracks(plid, [tid])               # attach only `tid`
    store.upsert_conflict(tid, "genre", [{"provider": "a", "value": "x"}, {"provider": "b", "value": "y"}])
    store.upsert_conflict(other, "genre", [{"provider": "a", "value": "x"}, {"provider": "b", "value": "y"}])
    assert store.conflict_count_for_playlist(plid) == 1   # only the attached track
    lst = store.unresolved_conflicts_for_playlist(plid)
    assert len(lst) == 1 and lst[0]["field"] == "genre"
    assert [c["value"] for c in lst[0]["candidates"]] == ["x", "y"]


def test_set_track_field_rejects_unknown_field(store):
    tid = _track(store)
    import pytest
    with pytest.raises(ValueError):
        store.set_track_field(tid, "not_a_field", "x")
