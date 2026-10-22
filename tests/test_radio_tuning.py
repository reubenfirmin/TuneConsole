"""Shared controls, bounded NOW evidence and staggered radio alternatives."""
from types import SimpleNamespace

import numpy as np
import pytest

from yt_playlist.rec import embed, radio_tuning, rec_params
from yt_playlist.rec.radio_candidates import RadioCandidates, CLUSTER_POOL_LIMIT
from yt_playlist.rec.radio_directions import RadioDirections


@pytest.fixture
def prepared(store):
    modes, bundles, tracks = [], {}, []
    for mid, genre in enumerate(("ambient", "techno"), 1):
        vector = np.eye(2, dtype=np.float32)[mid - 1]
        modes.append(dict(mode_id=mid, label=genre, families=[[genre, 1]], centroid=vector,
                          size=20, rep_keys=[]))
        familiar, fresh = [], []
        for i in range(12):
            vid = f"m{mid}-{i}"
            tid = store.tracks.upsert_track(vid, vid, f"Artist {vid}", "", 200)
            store.tracks.set_track_enrichment(tid, genre, "2001")
            key = store.tracks.catalog_subject(tid)["identity_key"]
            track = dict(key=key, video_id=vid, title=vid, artist=f"Artist {vid}", genre=genre, year="2001")
            (fresh if i >= 6 else familiar).append(track)
            tracks.append(track)
        bundles[str(mid)] = dict(wheelhouse=familiar, fresh=fresh)
    store.modes.replace_modes(modes, retired_ids=[], now=1)
    store.rec_surface.put_proposals("mode_bundles", bundles)
    return tracks


def candidate(store, **values):
    result = RadioCandidates(store, 100_000, {**radio_tuning.DEFAULTS, **values})
    result.rng.seed(3)
    return result


@pytest.mark.parametrize("bad", [None, [], {}, {**radio_tuning.DEFAULTS, "other": .5},
    {**radio_tuning.DEFAULTS, "energy": float("nan")}, {**radio_tuning.DEFAULTS, "energy": float("inf")},
    {**radio_tuning.DEFAULTS, "comfort": True}, {**radio_tuning.DEFAULTS, "theme": -.1}])
def test_controls_reject_invalid_input(bad):
    with pytest.raises(ValueError):
        radio_tuning.controls(bad)


def test_discovery_and_comfort_share_prepared_fresh_source(store, prepared):
    def sequence(comfort):
        c = candidate(store, comfort=comfort)
        history = [prepared[0]]
        for _ in range(5):
            history.append(c.next_track(history))
        return sum(bool(t.get("_radio_fresh")) for t in history[1:])
    assert sequence(0) >= 4
    assert sequence(1) == 0


def test_global_energy_overrides_inferred_mood_and_theme_stays_in_selected_region(store, prepared):
    chill = candidate(store, theme=0, energy=0)
    chill.now_posterior, chill.now_gain = {"2": 1}, 2
    for _ in range(5):
        assert chill.next_track([prepared[0]])["genre"] == "ambient"
    full = candidate(store, theme=0, energy=1)
    assert full.next_track([prepared[0]])["genre"] == "techno"
    themed = candidate(store, theme=1)
    themed.now_posterior, themed.now_gain = {"2": 1}, 2
    assert themed.next_track([prepared[0]], target=prepared[1])["genre"] == "ambient"


def test_enrichment_updates_visible_metadata_and_rejects_known_energy_mismatch(store, prepared):
    c = candidate(store, energy=0)
    tid = store.tracks.upsert_track("missing", "Missing", "Artist", "", 200)
    track = {"key": store.tracks.catalog_subject(tid)["identity_key"], "video_id": "missing"}
    c.retain([track])
    store.tracks.set_track_enrichment(tid, "hardcore", "2020")
    assert c.refresh_metadata([track])
    assert track["year"] == "2020"
    assert not c.fits(track)
    assert not c.refresh_metadata([track])


def test_cold_start_keeps_library_and_discovery_bounded(store):
    for i in range(110):
        store.tracks.upsert_track(f"v{i}", f"Song {i}", f"Artist {i}", "", 200)
    store.rec_surface.put_proposals("fresh_pool", [dict(key=f"fresh{i}", video_id=f"f{i}") for i in range(110)])
    c = candidate(store, comfort=0)
    assert len(c.pools["local"]) == CLUSTER_POOL_LIMIT
    assert any(not t.get("_radio_fresh") for t in c.pools["local"])


@pytest.mark.parametrize("base", [-2., 0., 2.])
def test_now_bonus_is_positive_bounded_and_neutral_at_zero_gain(base):
    assert radio_tuning.now_bonus(base, 1, 0) == base
    assert radio_tuning.now_bonus(base, .5, 2) > base
    assert radio_tuning.now_bonus(base, 1, 2) == pytest.approx(base + .12)


@pytest.mark.parametrize("evidence", ["organic", "thin", "old", "radio", "stale_dimensions", "gain_zero"])
def test_now_uses_persisted_confidence_gated_evidence_without_sampling_on_picks(store, prepared, monkeypatch, evidence):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    keys = [t["key"] for t in prepared[:3]]
    vectors = np.array([[1., 0.]] * 3, dtype=np.float32)
    if evidence == "stale_dimensions":
        vectors = np.array([[1., 0., 0.]] * 3, dtype=np.float32)
    reads = []
    def load(*args, **kwargs):
        reads.append(1)
        return keys, vectors, {k: i for i, k in enumerate(keys)}
    monkeypatch.setattr(embed, "load_content_vectors", load)
    if evidence == "gain_zero":
        rec_params.set_param(store, "now_gain", 0)
    store.settings.set_setting("radio_session_playlist_ids", '["PLRADIO"]')
    for i, key in enumerate(keys[:2] if evidence == "thin" else keys):
        at = 100_000 - i * 60 - (7 * 3600 if evidence == "old" else 0)
        store.history.record_play_event(iid, key, prepared[i]["video_id"], at,
            playlist_ytm_id="PLRADIO" if evidence == "radio" else None)
    c = candidate(store)
    assert c.now_posterior == ({"1": 1.} if evidence == "organic" else {})
    before = len(reads)
    for _ in range(5):
        c.next_track([prepared[0]])
    assert len(reads) == before
    if evidence == "gain_zero":
        assert not reads


def directions():
    tracks = [dict(key=str(i), video_id=str(i), artist=str(i)) for i in range(1000)]
    def pick(history, excluded=(), **kwargs):
        used = set(excluded) | {t["video_id"] for t in history}
        return next((t for t in tracks if t["video_id"] not in used), None)
    pool = SimpleNamespace(next_track=pick, direction_anchor=lambda h, a, **kw: pick(h + a, **kw),
                           cluster_for=lambda t: "local", modes={})
    return RadioDirections(pool, tracks[0]), [tracks[0]]


def test_decay_replaces_one_unused_stream_per_song_and_polls_are_stable():
    d, history = directions()
    d.update(history, [], 0)
    assert sorted(d.decay.values()) == [1, 2, 3]
    for transition in range(1, 7):
        previous = [b["id"] for b in d.slots]
        history.append(d.slots[d.active]["tracks"][0])
        d.update(history, [], transition)
        assert sum(d.slots[i]["id"] != previous[i] for i in range(4) if i != d.active) == 1
        assert sorted(d.decay.values()) == list(range(transition + 1, transition + 4))
        stable = [b["id"] for b in d.slots]
        assert not d.update(history, [], transition)
        assert [b["id"] for b in d.slots] == stable


def test_switch_preserves_other_deadlines_and_force_staggers_within_three_songs():
    d, history = directions()
    d.update(history, [], 0)
    saved = dict(d.decay)
    d.activate(d.slots[1]["id"])
    d.update(history, [], 0)
    assert 1 not in d.decay
    assert all(d.decay[slot] == saved[slot] for slot in (2, 3))
    assert d.decay[0] == 1
    d.update(history, [], 0, force=True)
    assert sorted(d.decay.values()) == [1, 2, 3]
