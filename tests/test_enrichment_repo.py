"""EnrichmentRepo: log rows, conflict upsert/reopen rule, resolution overwrite, scoped queries."""
import json


def _track(store, title="Hyperballad", artist="Bjork", genre=None, year=None):
    tid = store.tracks.upsert_track("v_" + title, title, artist, None, 200)
    if genre or year:
        store.tracks.set_track_enrichment(tid, genre, year)
    return tid


def test_log_enrichment_rows(store):
    tid = _track(store)
    store.enrichment_state.log_enrichment(tid, "run1", "musicbrainz", "genre", "Electronic", now=1.0)
    store.enrichment_state.log_enrichment(tid, "run1", "discogs", "genre", "Art Pop", now=1.0)
    rows = store.conn.execute("SELECT provider, field, value FROM enrichment_log WHERE track_id=? "
                              "ORDER BY id", (tid,)).fetchall()
    assert [(r["provider"], r["field"], r["value"]) for r in rows] == [
        ("musicbrainz", "genre", "Electronic"), ("discogs", "genre", "Art Pop")]


def test_genre_provenance_returns_latest_value_per_provider_and_current(store):
    tid = _track(store, genre="Art Pop")
    store.enrichment_state.log_enrichment(tid, "run1", "lastfm", "genre", "Electronic", now=1.0)
    store.enrichment_state.log_enrichment(tid, "run2", "lastfm", "genre", "Art Pop", now=2.0)
    store.enrichment_state.log_enrichment(tid, "run2", "discogs", "genre", None, now=2.0)

    result = store.enrichment_reports.genre_provenance(tid)

    assert result["current"] == "Art Pop"
    assert result["candidates"] == [
        {"provider": "lastfm", "value": "Art Pop"},
        {"provider": "discogs", "value": ""},
    ]


def test_genre_provenance_falls_back_to_retained_conflict_candidates(store):
    tid = _track(store, genre="Electronic")
    store.enrichment.upsert_conflict(tid, "genre", [{"provider": "musicbrainz", "value": "Electronic"},
                                          {"provider": "discogs", "value": "Art Pop"}])
    assert store.enrichment_reports.genre_provenance(tid)["candidates"] == [
        {"provider": "musicbrainz", "value": "Electronic"},
        {"provider": "discogs", "value": "Art Pop"},
    ]


def test_upsert_conflict_then_resolve_overwrites_column(store):
    tid = _track(store, genre="Electronic")
    cands = [{"provider": "musicbrainz", "value": "Electronic"},
             {"provider": "discogs", "value": "Art Pop"}]
    store.enrichment.upsert_conflict(tid, "genre", cands)
    assert store.enrichment_reports.conflict_count_for_playlist  # method exists
    store.enrichment.resolve_conflict(tid, "genre", "Art Pop")
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
    store.enrichment.upsert_conflict(tid, "genre", cands)
    store.enrichment.resolve_conflict(tid, "genre", "Art Pop")
    store.enrichment.upsert_conflict(tid, "genre", cands)            # same candidates again
    assert store.conn.execute("SELECT resolved FROM enrichment_conflict WHERE track_id=? AND field=?",
                              (tid, "genre")).fetchone()["resolved"] == 1


def test_resolved_conflict_reopens_on_new_value(store):
    tid = _track(store)
    store.enrichment.upsert_conflict(tid, "genre", [{"provider": "musicbrainz", "value": "Electronic"},
                                         {"provider": "discogs", "value": "Art Pop"}])
    store.enrichment.resolve_conflict(tid, "genre", "Art Pop")
    store.enrichment.upsert_conflict(tid, "genre", [{"provider": "musicbrainz", "value": "Electronic"},
                                         {"provider": "lastfm", "value": "Trip Hop"}])  # new option
    assert store.conn.execute("SELECT resolved FROM enrichment_conflict WHERE track_id=? AND field=?",
                              (tid, "genre")).fetchone()["resolved"] == 0


def test_conflict_count_and_list_scoped_to_playlist(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    tid = _track(store, title="A")
    other = _track(store, title="B")
    plid = store.playlists.upsert_playlist(iid, "PL", "Mix", 1, "h", 1000.0)
    store.playlists.set_playlist_tracks(plid, [tid])               # attach only `tid`
    store.enrichment.upsert_conflict(tid, "genre", [{"provider": "a", "value": "x"}, {"provider": "b", "value": "y"}])
    store.enrichment.upsert_conflict(other, "genre", [{"provider": "a", "value": "x"}, {"provider": "b", "value": "y"}])
    assert store.enrichment_reports.conflict_count_for_playlist(plid) == 1   # only the attached track
    lst = store.enrichment_reports.unresolved_conflicts_for_playlist(plid)
    assert len(lst) == 1 and lst[0]["field"] == "genre"
    assert [c["value"] for c in lst[0]["candidates"]] == ["x", "y"]


def test_set_track_field_rejects_unknown_field(store):
    tid = _track(store)
    import pytest
    with pytest.raises(ValueError):
        store.enrichment.set_track_field(tid, "not_a_field", "x")


def test_conflict_current_value_keeps_historical_provider_evidence(store):
    tid = _track(store, genre="Electronic")
    store.enrichment_state.log_enrichment(tid, "old-run", "musicbrainz", "genre", "Electronic")
    store.enrichment.upsert_conflict(tid, "genre", [{"provider": "lastfm", "value": "Rock"},
                                         {"provider": "discogs", "value": "Pop"}])
    conflict = store.enrichment_reports.enrichment_conflict_detail(tid, "genre")
    current, *alternatives = conflict["options"]
    assert current["value"] == "Electronic" and current["current"]
    assert current["providers"] == ["musicbrainz"]
    assert current["source_note"] == ""
    assert [o["value"] for o in alternatives] == ["Rock", "Pop"]


def test_current_numeric_option_merges_matching_provider_values(store):
    tid = _track(store)
    store.tracks.set_track_audio(tid, bpm=120.0)
    store.enrichment.upsert_conflict(tid, "bpm", [{"provider": "deezer", "value": 120},
                                       {"provider": "acousticbrainz", "value": 124}])
    options = store.enrichment_reports.enrichment_conflict_detail(tid, "bpm")["options"]
    assert len(options) == 2
    assert options[0]["current"] and options[0]["providers"] == ["deezer"]
    assert store.enrichment.resolve_enrichment_candidate(tid, "bpm", options[0]["value"])
    assert store.tracks.get_track_audio(tid)[0] == 120.0


def test_unset_current_value_is_an_explicit_option(store):
    tid = _track(store)
    store.enrichment.upsert_conflict(tid, "year", [{"provider": "musicbrainz", "value": "1995"},
                                        {"provider": "discogs", "value": "1996"}])
    option = store.enrichment_reports.enrichment_conflict_detail(tid, "year")["options"][0]
    assert option["value"] == "" and option["label"] == "Not set" and option["current"]
    assert store.enrichment.resolve_enrichment_candidate(tid, "year", "")
    assert store.tracks.get_track_enrichment(tid)[1] in (None, "")
