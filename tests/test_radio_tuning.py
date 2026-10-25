"""Shared controls, bounded NOW evidence and staggered radio alternatives."""
from types import SimpleNamespace

import numpy as np
import pytest

from yt_playlist.rec import embedding_vectors as embed, radio_tuning, rec_params
from yt_playlist.rec.radio_candidates import RadioCandidates, CLUSTER_POOL_LIMIT
from yt_playlist.rec.radio_directions import RadioDirections
from tests.musical_support import prepare_profiles
from yt_playlist.core import catalog


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
            with store.conn:
                catalog.choose_field(store.conn, tid, 'energy', .02 if mid == 1 else .98)
            key = store.tracks.catalog_subject(tid)["identity_key"]
            track = dict(key=key, video_id=vid, title=vid, artist=f"Artist {vid}", genre=genre, year="2001")
            (fresh if i >= 6 else familiar).append(track)
            tracks.append(track)
        bundles[str(mid)] = dict(wheelhouse=familiar, fresh=fresh)
    store.modes.replace_modes(modes, retired_ids=[], now=1)
    store.rec_proposals.put_proposals("mode_bundles", bundles)
    prepare_profiles(store)
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


def test_middle_uses_style_and_energy_while_themed_pins_the_playing_genre(store, prepared):
    chill = candidate(store, theme=.5, energy=0)
    chill.now_posterior, chill.now_gain = {"2": 1}, 2
    for _ in range(5):
        assert chill.next_track([prepared[0]], target=prepared[-1])["genre"] == "ambient"
    full = candidate(store, theme=.5, energy=1)
    assert full.next_track([prepared[0]], target=prepared[-1])["genre"] == "techno"
    themed = candidate(store, theme=1)
    themed.now_posterior, themed.now_gain = {"2": 1}, 2
    assert themed.next_track([prepared[0]], target=prepared[-1])["genre"] == "ambient"


def test_enrichment_updates_visible_metadata_without_rejecting_a_weak_genre_prior(store, prepared):
    c = candidate(store, energy=0)
    tid = store.tracks.upsert_track("missing", "Missing", "Artist", "", 200)
    track = {"key": store.tracks.catalog_subject(tid)["identity_key"], "video_id": "missing"}
    c.retain([track])
    store.tracks.set_track_enrichment(tid, "hardcore", "2020")
    prepare_profiles(store)
    assert c.refresh_metadata([track])
    assert track["year"] == "2020"
    assert c.fits(track)
    assert not c.refresh_metadata([track])

    # An explicit recording correction can reject a future track. This update
    # changes only its prepared profile, with unchanged visible metadata.
    with store.conn:
        catalog.choose_field(store.conn, tid, 'energy', .95)
    prepare_profiles(store)
    assert c.refresh_metadata([track])
    assert not c.fits(track)
    assert not c.refresh_metadata([track])


def test_cold_start_keeps_library_and_discovery_bounded(store):
    for i in range(110):
        store.tracks.upsert_track(f"v{i}", f"Song {i}", f"Artist {i}", "", 200)
    store.rec_proposals.put_proposals("fresh_pool", [dict(key=f"fresh{i}", video_id=f"f{i}") for i in range(110)])
    c = candidate(store, comfort=0)
    assert len(c.pools["local"]) == CLUSTER_POOL_LIMIT
    assert any(not t.get("_radio_fresh") for t in c.pools["local"])


@pytest.mark.parametrize("cache", ["fresh", "expired", "different_model"])
def test_retuning_reuses_now_without_loading_library_vectors(store, prepared, monkeypatch, cache):
    previous = candidate(store)
    previous.now_posterior, previous.now_gain = {"1": 1., "retired": .5}, 2.
    if cache == "different_model":
        previous.encoding_model = "old-model"
    now = 100_000 + (radio_tuning.NOW_REFRESH_SECONDS if cache == "expired" else 1)

    def unexpected_load(*args, **kwargs):
        pytest.fail("Retuning must use cached NOW evidence, without a full library snapshot or matrix")

    monkeypatch.setattr(store.rec_model, "content_generation", unexpected_load)
    monkeypatch.setattr(embed, "load_content_vectors", unexpected_load)
    updated = RadioCandidates(store, now, {**radio_tuning.DEFAULTS, "comfort": 0}, now_source=previous)
    if cache == "fresh":
        assert updated.now_posterior == {"1": 1.}
        assert updated.now_gain == 2.
        assert updated.now_sampled_at == previous.now_sampled_at
        assert updated.now_posterior is not previous.now_posterior
    else:
        assert updated.now_posterior == {}
        assert updated.now_gain == 0
        assert now - updated.now_sampled_at >= radio_tuning.NOW_REFRESH_SECONDS
    assert updated.tuning["comfort"] == 0
    assert updated.next_track([prepared[0]])


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


def test_persisted_now_evidence_changes_a_future_choice_and_keeps_existing_directions(store, prepared, monkeypatch):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    neutral = candidate(store, theme=0)
    before = neutral.next_track([])
    preferred = [t for t in prepared if t["genre"] != before["genre"]][:3]
    keys = [t["key"] for t in preferred]
    vector = [1., 0.] if preferred[0]["genre"] == "ambient" else [0., 1.]
    monkeypatch.setattr(embed, "load_content_vectors", lambda *_args, **_kwargs: (
        keys, np.array([vector] * len(keys), dtype=np.float32), {k: i for i, k in enumerate(keys)}))
    for i, track in enumerate(preferred):
        store.history.record_play_event(iid, track["key"], track["video_id"], 100_000 - i * 60)
    influenced = candidate(store, theme=0)
    assert influenced.next_track([])["genre"] == preferred[0]["genre"]
    assert influenced.next_track([])["genre"] != before["genre"]

    plans = RadioDirections(influenced, prepared[0])
    history = [prepared[0]]
    plans.update(history, [], 0)
    selected = plans.slots[2]
    plans.activate(selected["id"])
    plans.update(history, [], 0)
    saved = plans.public([])
    # Refreshing evidence only affects future planning, never a selected or visible plan.
    rec_params.set_param(store, "now_gain", 0)
    influenced.refresh_now(100_001)
    assert not plans.update(history, [], 0)
    assert plans.public([]) == saved
    assert plans.active == 2
    assert plans.slots[2]["id"] == selected["id"]


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
    assert list(d.decay.values()) == [5, 4, 3]
    assert [(b["decay_remaining"], b["decay_total"]) for b in d.public([]) if not b["active"]] == [
        (5, 5), (4, 4), (3, 3)]
    for transition in range(1, 10):
        previous = [b["id"] for b in d.slots]
        history.append(d.slots[d.active]["tracks"][0])
        d.update(history, [], transition)
        assert sum(d.slots[i]["id"] != previous[i] for i in range(4) if i != d.active) == int(transition >= 3)
        if transition < 3:
            assert [(b["decay_remaining"], b["decay_total"]) for b in d.public([]) if not b["active"]] == [
                (5 - transition, 5), (4 - transition, 4), (3 - transition, 3)]
        else:
            assert sorted(d.decay.values()) == list(range(transition + 1, transition + 4))
        stable = [b["id"] for b in d.slots]
        assert not d.update(history, [], transition)
        assert [b["id"] for b in d.slots] == stable


def test_switch_preserves_other_deadlines_and_force_staggers_five_four_three():
    d, history = directions()
    d.update(history, [], 0)
    saved = dict(d.decay)
    d.activate(d.slots[1]["id"])
    d.update(history, [], 0)
    assert 1 not in d.decay
    assert all(d.decay[slot] == saved[slot] for slot in (2, 3))
    assert d.decay[0] == saved[1]
    d.update(history, [], 7, force=True)
    assert list(d.decay.values()) == [12, 11, 10]
    assert [b["decay_remaining"] for b in d.public([]) if not b["active"]] == [5, 4, 3]
