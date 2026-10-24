"""Prepared-region upgrades and metadata enrichment during playback."""
import json
from types import SimpleNamespace

import pytest
from yt_playlist.web import radio_session
from yt_playlist.rec.radio_directions import RadioDirections
from tests.radio_session_support import poc, play

def prepare_radio_styles(controller, pool, monkeypatch):
    """Publish four real prepared regions after a session has already started cold."""
    import numpy as np
    from yt_playlist.rec import embedding_vectors as embed
    from yt_playlist.rec.radio_candidates import RadioCandidates
    from tests.model_support import build_content_model

    store = controller.ctx.store
    ids = store.tracks.track_ids_for_videos([t["video_id"] for t in pool])
    bundles, modes = {}, []
    genres = ("Ambient", "Techno", "Jazz", "Rock")
    for index, track in enumerate(pool):
        store.tracks.set_track_enrichment(ids[track["video_id"]], genres[index // 8], "2001")
    build_content_model(store)
    keys = [pool[index * 8]["key"] for index in range(4)]
    vectors = store.rec_model.content_vectors_by_keys(keys)
    for index, genre in enumerate(genres):
        mid = index + 1
        tracks = [{**t, "genre": genre, "year": "2001"} for t in pool[index * 8:(index + 1) * 8]]
        bundles[str(mid)] = {"wheelhouse": tracks}
        modes.append({"mode_id": mid, "label": genre, "families": [[genre.casefold(), 1]],
            "centroid": np.frombuffer(vectors[keys[index]], dtype=np.float32), "size": 8,
            "rep_keys": [keys[index]], "space": embed.content_space_id(store)})
    store.modes.replace_modes(modes, retired_ids=[], now=1234)
    store.rec_proposals.put_proposals("mode_bundles", bundles, 1234)
    monkeypatch.setattr(radio_session, "RadioCandidates", RadioCandidates)


def test_monitor_upgrades_fallback_to_distinct_prepared_streams_without_changing_playback(poc, monkeypatch):
    from yt_playlist.rec import content_model, embedding_training as training, embedding_vectors as embed

    c, remote, frames, _, pool = poc
    c.start()
    play(c, "v2")
    s, original = c.session, c.session.candidates
    tracks, heard, choices = list(s.tracks), list(s.heard), dict(s.choices)
    active = s.directions.slots[s.directions.active]
    queue = list(active["tracks"])
    prepare_radio_styles(c, pool, monkeypatch)

    def forbidden(*args, **kwargs):
        pytest.fail("Radio recovery must reuse prepared pools without loading or fitting the full model")

    monkeypatch.setattr(embed, "load_content_vectors", forbidden)
    monkeypatch.setattr(training, "build_vectors_from_baskets", forbidden)
    monkeypatch.setattr(content_model, "build_library_content", forbidden)
    monkeypatch.setattr(c.ctx.store.rec_model, "content_generation", forbidden)
    waits = iter([False, True])
    monkeypatch.setattr(s.stop, "wait", lambda _: next(waits))
    c._monitor(s)

    assert s.candidates is not original and s.candidates is s.directions.candidates
    assert len(s.candidates.modes) == 4
    assert len({b["cluster_id"] for b in c.status()["directions"]}) == 4
    assert c.status()["active"]
    assert s.tracks == tracks and s.heard == heard and s.choices == choices
    assert s.directions.slots[s.directions.active]["id"] == active["id"]
    assert s.directions.slots[s.directions.active]["tracks"] == queue
    assert c.ctx.bridge.now_playing["video_id"] == "v2"
    assert remote.ids == [t["video_id"] for t in tracks]
    assert not remote.adds and not remote.removes and len(frames) == 1
    saved = json.loads(c.ctx.store.settings.get_setting("radio_current_session"))
    assert saved["fingerprint"] == s.candidates.fingerprint


def test_pool_recovery_waits_for_an_explicit_stream_switch_to_finish(poc, monkeypatch):
    c, remote, _, _, pool = poc
    c.start()
    play(c, "v2")
    s, original = c.session, c.session.candidates
    branch = c.status()["directions"][2]
    c.select(branch["id"], c.status()["menu_id"])
    requested = s.requested_direction
    prepare_radio_styles(c, pool, monkeypatch)
    c._refresh_candidates(s)
    assert s.candidates is original and s.requested_direction is requested
    c.advance(s)
    assert s.requested_direction is None and s.replacement is None
    assert s.directions.active == 2
    selected = list(s.directions.slots[2]["tracks"])
    saved_ids = list(remote.ids)
    c._refresh_candidates(s)
    assert s.candidates is not original
    assert s.directions.active == 2 and s.directions.slots[2]["tracks"] == selected
    assert remote.ids == saved_ids
    assert c.ctx.bridge.now_playing["video_id"] == "v2"


def test_missing_pools_keep_the_menu_stable_and_request_preparation_once(poc):
    c, remote, *_ = poc
    c.start()
    play(c, "v2")
    s, original = c.session, c.session.candidates
    menu = c.status()["menu_id"]
    requested = []
    c.ctx.rec_worker = SimpleNamespace(busy=False, trigger_mode_refresh=lambda: requested.append(True))
    c._refresh_candidates(s)
    s.candidates_checked_at = 0
    c._refresh_candidates(s)
    assert requested == [True]
    assert s.candidates is original and c.status()["menu_id"] == menu
    assert remote.ids == ["v1", "v2", "v3"] and not remote.adds and not remote.removes


def test_failed_pool_upgrade_preserves_the_existing_streams_until_retry(poc, monkeypatch):
    c, remote, _, _, pool = poc
    c.start()
    play(c, "v2")
    s, candidates = c.session, c.session.candidates
    directions, menu = s.directions, c.status()["menu_id"]
    prepare_radio_styles(c, pool, monkeypatch)
    update = RadioDirections.update

    def fail(*args, **kwargs):
        raise RuntimeError("Temporary preparation failure")

    monkeypatch.setattr(RadioDirections, "update", fail)
    with pytest.raises(RuntimeError, match="Temporary preparation failure"):
        c._refresh_candidates(s)
    assert s.candidates is candidates and s.directions is directions
    assert c.status()["menu_id"] == menu
    assert len(c.status()["directions"]) == 4
    monkeypatch.setattr(RadioDirections, "update", update)
    s.candidates_checked_at = 0
    c._refresh_candidates(s)
    assert s.candidates is not candidates and len(s.candidates.modes) == 4
    assert remote.ids == ["v1", "v2", "v3"] and not remote.adds and not remote.removes


def test_metadata_enrichment_is_requested_only_after_render_and_bad_fit_is_replaced(poc, monkeypatch):
    c, *_ = poc
    requests = []
    c.ctx.enrich_worker = SimpleNamespace(prioritize=lambda keys: requests.append(list(keys)))
    c.start()
    play(c, "v1")
    assert not requests
    s = c.session
    c.seen(s.directions.menu_id)
    c.seen(s.directions.menu_id)
    assert len(requests) == 1
    assert requests[0][0] == s.tracks[0]["key"]
    old = s.directions.slots[1]["tracks"][0]
    monkeypatch.setattr(s.candidates, "refresh_metadata", lambda tracks: old.update(genre="Metal", year="2000") or True)
    monkeypatch.setattr(s.candidates, "fits", lambda track, anchor: track["video_id"] != old["video_id"])
    c.listening.refresh_metadata(s)
    assert s.metadata_revision == 1
    c.advance(s)
    assert all(old["video_id"] not in [t["video_id"] for t in b["tracks"]] for b in s.directions.slots)
    assert s.observed_video == "v1"


def test_radio_refreshes_metadata_for_current_and_played_table_rows(poc, monkeypatch):
    from yt_playlist.rec.radio_candidates import RadioCandidates

    c, *_ = poc
    requests = []
    c.ctx.enrich_worker = SimpleNamespace(prioritize=lambda keys: requests.append(list(keys)))
    c.start()
    play(c, "v1")
    play(c, "v2")
    s = c.session
    c.seen(s.directions.menu_id)
    assert requests[0][:2] == [s.tracks[1]["key"], s.tracks[0]["key"]]
    ids = c.ctx.store.tracks.track_ids_for_videos([t["video_id"] for t in s.tracks[:2]])
    for track in s.tracks[:2]:
        tid = ids[track["video_id"]]
        c.ctx.store.tracks.set_track_enrichment(tid, "Techno", "2001")
    # Use the production catalog refresh against the fixture's deterministic sampler.
    s.candidates.store = c.ctx.store
    s.candidates.affinities = {}
    monkeypatch.setattr(s.candidates, "_energy", lambda tracks: None, raising=False)
    monkeypatch.setattr(s.candidates, "refresh_metadata",
                        lambda tracks: RadioCandidates.refresh_metadata(s.candidates, tracks))
    c.listening.refresh_metadata(s)
    state = c.status()
    assert state["metadata_revision"] == 1
    assert all(t["genre"] == "Techno" and t["year"] == "2001" for t in state["tracks"][:2])
    assert state["position"] == 1


