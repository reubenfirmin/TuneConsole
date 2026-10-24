"""Temporary fully Themed backfill must outlive finite prepared Home lists (#286)."""
import json

import numpy as np
import pytest

from yt_playlist.rec import embedding_vectors as embed, radio_catalog, radio_tuning
from yt_playlist.rec.radio_candidates import CLUSTER_POOL_LIMIT, RadioCandidates
from yt_playlist.rec.radio_directions import DEPTH, RadioDirections
from yt_playlist.rec.radio_genres import genre


def catalog_track(store, video, label="Downtempo", *, artist=None, duration=200, available=None):
    artist = artist or f"Artist {video}"
    tid = store.tracks.upsert_track(video, video, artist, "", duration, available=available)
    store.tracks.set_track_enrichment(tid, label, "2001")
    key = store.tracks.catalog_subject(tid)["identity_key"]
    return dict(key=key, video_id=video, title=video, artist=artist, genre=label), tid


@pytest.fixture
def seed(store):
    track, _ = catalog_track(store, "seed")
    store.modes.replace_modes([dict(mode_id=1, label="Electronic", families=[["electro-synth", 1]],
        centroid=np.array([1., 0.], dtype=np.float32), size=1, rep_keys=[track["key"]])], [], 1)
    store.rec_proposals.put_proposals("mode_bundles", {"1": {"wheelhouse": [track]}})
    return track


def candidates(store, **tuning):
    result = RadioCandidates(store, 100_000, {**radio_tuning.DEFAULTS, "theme": 1, **tuning})
    result.rng.seed(286)
    return result


def test_prepared_pool_reuses_the_accounts_verified_playable_upload(store, seed):
    owner = store.identities.upsert_identity('Main', 'credential', None, True)
    actual = {**seed, 'video_id': 'playable', 'duration': 200}
    store.tracks.upsert_track('playable', seed['title'], seed['artist'], '', 200)
    store.tracks.remember_playback_alternate(owner, {**seed, 'duration': 200}, actual,
        evidence={'playlist': 'PL', 'set_video_id': 'owned'}, now=1)
    c = candidates(store)
    assert [t['video_id'] for t in c._load('1')] == ['playable']
    assert c.seed_offer(1)[0]['video_id'] == 'playable'


def test_themed_fills_all_four_streams_with_unprepared_catalog_tracks(store, seed, monkeypatch):
    for i in range(30):
        catalog_track(store, f"catalog-{i}")
    c = candidates(store)

    def forbidden(*args, **kwargs):
        pytest.fail("Themed backfill must not load or rerank the full library model")

    monkeypatch.setattr(embed, "load_content_vectors", forbidden)
    monkeypatch.setattr(store.rec_model, "content_generation", forbidden)
    plans = RadioDirections(c, seed)
    assert plans.update([seed], [], 0)
    future = [t for branch in plans.slots for t in branch["tracks"]]
    assert len(future) == 4 * DEPTH
    assert len({t["video_id"] for t in future}) == len(future)
    assert all(genre(t) == genre(seed) and t["video_id"] != seed["video_id"] for t in future)
    assert not plans.update([seed], [], 0)


def test_themed_keeps_refilling_beyond_one_catalog_batch(store, seed, monkeypatch):
    # An entire rejected page must not hide the matching catalog entries after it.
    for i in range(CLUSTER_POOL_LIMIT):
        catalog_track(store, f"muted-{i}", artist="Muted Artist")
    expected = {catalog_track(store, f"eligible-{i}")[0]["video_id"]
                for i in range(CLUSTER_POOL_LIMIT + 7)}
    monkeypatch.setattr(store.rec_model, "muted_artists", lambda: {"Muted Artist"})
    c = candidates(store)
    batches, read = [], c._read_vectors

    def bounded(keys):
        batches.append(len(keys))
        assert len(keys) <= CLUSTER_POOL_LIMIT
        return read(keys)

    monkeypatch.setattr(c, "_read_vectors", bounded)
    history = [seed]
    for _ in range(len(expected)):
        pick = c.next_track(history)
        assert pick is not None
        assert pick["video_id"] in expected
        assert pick["key"] not in {t["key"] for t in history}
        history.append(pick)
    assert {t["video_id"] for t in history[1:]} == expected
    assert c.next_track(history) is None
    assert max(batches) == CLUSTER_POOL_LIMIT


def test_themed_catalog_respects_exclusions_and_current_metadata(store, seed):
    rejected = {}
    for name in ("disliked", "dismissed", "muted", "reserved", "heard", "override", "duplicate"):
        rejected[name] = catalog_track(store, name)
    catalog_track(store, "unavailable", available=0)
    catalog_track(store, "too-long", duration=100_000)
    catalog_track(store, "unknown", label=None)
    catalog_track(store, "same-family", label="Synthwave")
    # The underlying recording is Downtempo, but the current upload override wins.
    with store.conn:
        store.conn.execute("INSERT INTO entry_field_choices VALUES (?,?,?,?)",
                           (rejected["override"][1], "genre", json.dumps("Indie Rock"), "test"))
    # Another upload of an already heard recording is not another eligible song.
    heard, tid = rejected["duplicate"]
    store.tracks.upsert_track("duplicate-upload", heard["title"], heard["artist"], "", 200)
    rid = store.tracks.catalog_subject(tid)["recording_id"]
    store.tracks.associate_youtube_item("duplicate-upload", rid, source="test", evidence={"verified": True}, now=1)
    heard["key"] = store.tracks.identity_key_for_video(heard["video_id"])
    assert store.tracks.identity_key_for_video("duplicate-upload") == heard["key"]
    good, good_id = catalog_track(store, "corrected", label="Indie Rock")
    with store.conn:
        store.conn.execute("INSERT INTO entry_field_choices VALUES (?,?,?,?)",
                           (good_id, "genre", json.dumps("Downtempo"), "test"))
    c = candidates(store)
    # Feedback can arrive after construction, before these tracks enter a menu.
    store.rec_model.record_dislike(rejected["disliked"][0]["key"], 200_000, 1)
    store.rec_model.record_feedback("for_you", rejected["dismissed"][0]["key"], "dismiss", now=1)
    store.rec_model.record_feedback("for_you", "artist:Artist muted", "mute", now=1)
    history = [heard, rejected["heard"][0], seed]
    excluded = {rejected["reserved"][0]["video_id"]}
    pick = c.next_track(history, excluded=excluded)
    assert pick["video_id"] == good["video_id"]
    assert genre(pick) == genre(seed)
    assert c.next_track(history + [pick], excluded=excluded) is None


@pytest.mark.parametrize("comfort", [0, 1])
def test_themed_catalog_keeps_library_and_discovery_distinct(store, seed, comfort):
    owned, _ = catalog_track(store, "owned")
    store.discovery.upsert_discovered_track("fresh-key", "fresh-video", "Fresh", "New Artist", "", "",
                                           "Downtempo", 2001, "artist:new", 1)
    pick = candidates(store, comfort=comfort).next_track([seed])
    assert pick["video_id"] == (owned["video_id"] if comfort else "fresh-video")
    assert bool(pick["_radio_fresh"]) == (comfort == 0)


@pytest.mark.parametrize("label", ["DRUM AND BASS", "Drum & Bass", "drum-and-bass"])
def test_themed_catalog_matches_genre_aliases_and_punctuation(store, seed, label):
    current = {**seed, "genre": "Drum & Bass"}
    expected, _ = catalog_track(store, "alias", label=label)
    catalog_track(store, "related", label="Jungle")
    pick = candidates(store).next_track([current], current=current)
    assert pick["video_id"] == expected["video_id"]


@pytest.mark.parametrize("theme", [0, .5, .99])
def test_catalog_workaround_is_limited_to_fully_themed(store, seed, monkeypatch, theme):
    catalog_track(store, "unprepared")

    def forbidden(*args, **kwargs):
        pytest.fail("Other theme settings must keep their existing candidate selection")

    monkeypatch.setattr(radio_catalog, "themed_pool", forbidden)
    c = candidates(store, theme=theme)
    assert c.next_track([seed]) is None
