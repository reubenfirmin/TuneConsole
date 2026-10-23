"""Only complete, successful provider agreement retires a current metadata conflict."""
import pytest

from tests.test_waterfall import FakeProv, _cfg, _run, _track
from yt_playlist.providers.base import EnrichmentResult
from yt_playlist.util import net


@pytest.fixture
def conflict(store):
    track = _track(store)
    iid = store.identities.upsert_identity("Main", "bridge", None, True)
    playlist = store.playlists.upsert_playlist(iid, "PL", "Library", 1, "", 100)
    store.playlists.set_playlist_tracks(playlist, [track["id"]])
    providers = [FakeProv("musicbrainz", {"Hyperballad": {"genre": "Rock", "year": "2000"}}),
                 FakeProv("discogs", {"Hyperballad": {"genre": "Pop", "year": "2001"}})]
    config = _cfg(("musicbrainz", True), ("discogs", True))
    _run(store, [track], providers, config)
    assert store.enrichment.conflict_count_for_playlist(playlist) == 2
    return track, playlist, providers, config


def _rows(store, track):
    return {row["field"]: dict(row) for row in store.conn.execute(
        "SELECT * FROM enrichment_conflict WHERE track_id=?", (track["id"],))}


def test_agreement_retires_conflicts_but_keeps_historical_evidence(store, conflict):
    track, playlist, providers, config = conflict
    previous = _rows(store, track)
    providers[1]._by_title = providers[0]._by_title
    _run(store, [track], providers, config)
    assert store.enrichment.conflict_count_for_playlist(playlist) == 0
    assert store.enrichment.unresolved_conflicts_for_playlist(playlist) == []
    current = _rows(store, track)
    assert all(row["resolved"] == 2 for row in current.values())
    assert {field: row["candidates"] for field, row in current.items()} == {
        field: row["candidates"] for field, row in previous.items()}
    assert store.conn.execute("SELECT COUNT(*) FROM enrichment_log").fetchone()[0] == 8
    assert store.tracks.get_track_enrichment(track["id"]) == ("Rock", "2000")
    # Even the original options become a current disagreement again after automatic retirement.
    providers[1]._by_title = {"Hyperballad": {"genre": "Pop", "year": "2001"}}
    _run(store, [track], providers, config)
    assert store.enrichment.conflict_count_for_playlist(playlist) == 2


@pytest.mark.parametrize("problem", ["timeout", "breaker", "disabled", "unavailable", "no-match", "missing-field"])
def test_partial_evidence_cannot_retire_previous_disagreements(store, conflict, problem):
    track, playlist, providers, config = conflict
    providers[1]._by_title = {"Hyperballad": {"genre": "Rock", "year": "2000"}}
    if problem == "timeout":
        def probe(track, store):
            net.record_probe_failure("request timed out")
            return EnrichmentResult("discogs", {"genre": "Rock", "year": "2000"})
        providers[1].probe = probe
    elif problem == "breaker":
        providers[1].tripped = lambda: True
    elif problem == "disabled":
        config[1]["enabled"] = False
    elif problem == "unavailable":
        providers[1]._avail = False
    elif problem == "no-match":
        providers[1]._by_title = {}
    else:
        providers[1]._by_title = {"Hyperballad": {"genre": "Rock"}}
    _run(store, [track], providers, config)
    rows = _rows(store, track)
    assert rows["year"]["resolved"] == 0
    assert rows["genre"]["resolved"] == (2 if problem == "missing-field" else 0)


def test_all_previous_candidate_providers_must_return_the_field(store, conflict):
    track, playlist, providers, config = conflict
    providers[1]._by_title = providers[0]._by_title
    store.enrichment.upsert_conflict(track["id"], "genre", [
        {"provider": "musicbrainz", "value": "Rock"}, {"provider": "discogs", "value": "Pop"},
        {"provider": "lastfm", "value": "Jazz"}])
    _run(store, [track], providers, config)
    assert _rows(store, track)["genre"]["resolved"] == 0


def test_partial_disagreement_cannot_drop_an_unrefreshed_candidate_before_later_agreement(store, conflict):
    track, playlist, providers, config = conflict
    store.enrichment.upsert_conflict(track["id"], "genre", [
        {"provider": "musicbrainz", "value": "Rock"}, {"provider": "discogs", "value": "Pop"},
        {"provider": "lastfm", "value": "Jazz"}])
    _run(store, [track], providers, config)  # Last.fm skipped while the others still disagree.
    providers[1]._by_title = providers[0]._by_title
    _run(store, [track], providers, config)
    assert _rows(store, track)["genre"]["resolved"] == 0


def test_agreement_preserves_user_resolution_and_new_value_reopening_rule(store, conflict):
    track, playlist, providers, config = conflict
    store.enrichment.resolve_conflict(track["id"], "genre", "Chosen")
    providers[1]._by_title = providers[0]._by_title
    _run(store, [track], providers, config)
    assert _rows(store, track)["genre"]["resolved"] == 1
    assert store.tracks.get_track_enrichment(track["id"])[0] == "Chosen"
    providers[1]._by_title = {"Hyperballad": {"genre": "Pop", "year": "2000"}}
    _run(store, [track], providers, config)
    assert _rows(store, track)["genre"]["resolved"] == 1
    providers[1]._by_title = {"Hyperballad": {"genre": "Jazz", "year": "2000"}}
    _run(store, [track], providers, config)
    assert _rows(store, track)["genre"]["resolved"] == 0
    assert store.tracks.get_track_enrichment(track["id"])[0] == "Chosen"


def test_agreement_from_an_associated_item_retires_recording_facts_only(store, conflict):
    track, playlist, providers, config = conflict
    other = store.tracks.upsert_track("alternate", "Hyperballad", "Bjork", None, 200)
    store.tracks.associate_youtube_item("alternate", store.tracks.catalog_subject(track["id"])["recording_id"],
        source="user", evidence={"verified": True}, now=200)
    providers[1]._by_title = providers[0]._by_title
    _run(store, [store.tracks.catalog_subject(other)], providers, config)
    rows = _rows(store, track)
    assert rows["genre"]["resolved"] == 2
    assert rows["year"]["resolved"] == 0
