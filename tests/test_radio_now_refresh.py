"""Current-listening policy is maintained without a visible/polling Radio page."""
import json
import time

import numpy as np
import pytest

from tests.radio_session_support import poc, play
from tests.test_radio_tuning import prepared
from yt_playlist.rec import embedding_vectors as embed, radio_tuning, rec_params
from yt_playlist.rec.radio_candidates import RadioCandidates
from yt_playlist.web import radio_session


@pytest.fixture
def active(poc, prepared, monkeypatch):
    controller, remote, _, workers, _ = poc
    store = controller.ctx.store
    clock = [100_000.]
    controller.ctx.now_fn = lambda: clock[0]
    monkeypatch.setattr(radio_session, "RadioCandidates", RadioCandidates)
    neutral = RadioCandidates(store, clock[0], {**radio_tuning.DEFAULTS, "theme": 0})
    neutral.rng.seed(3)
    neutral_style = neutral.next_track([])["genre"]
    preferred = [t for t in prepared if t["genre"] != neutral_style][:3]
    keys = [t["key"] for t in preferred]
    vector = [1., 0.] if preferred[0]["genre"] == "ambient" else [0., 1.]
    reads = []

    def vectors(*args, **kwargs):
        reads.append(1)
        return keys, np.array([vector] * len(keys), dtype=np.float32), {k: i for i, k in enumerate(keys)}

    monkeypatch.setattr(embed, "load_content_vectors", vectors)
    iid = store.identities.get_master_identity().id
    for i, track in enumerate(preferred):
        store.history.record_play_event(iid, track["key"], track["video_id"], clock[0] - i * 60)
    assert controller.start(prepared[0]["video_id"])["ok"]
    play(controller, remote.ids[0])
    assert controller.session.candidates.now_posterior
    workers.clear()
    reads.clear()
    return controller, remote, workers, clock, reads


@pytest.mark.parametrize("invalidation", ["quiet_window", "disabled_gain"])
def test_hidden_radio_monitor_refreshes_invalid_now_evidence(active, monkeypatch, invalidation):
    controller, remote, workers, clock, reads = active
    store, session = controller.ctx.store, controller.session
    if invalidation == "quiet_window":
        clock[0] += (rec_params.get_param(store, "now_window_h") + 1) * 3600
    else:
        rec_params.set_param(store, "now_gain", 0)
        clock[0] += radio_tuning.NOW_REFRESH_SECONDS + 1
    ticks = [0]

    def wait(_timeout):
        ticks[0] += 1
        if ticks[0] == 2:
            # Let the maintenance job finish while the real monitor remains active.
            assert len(workers) == 1 and not reads
            workers[0][0][0]()
        if ticks[0] > 3:
            session.stop.set()
            return True
        controller.ctx.bridge.now_playing = {"video_id": remote.ids[-1], "playlist_ytm_id": "PLSESSION",
                                             "paused": False, "position": 30, "duration": 300}
        controller.ctx.bridge.now_playing_seen_at = time.monotonic()
        return False

    monkeypatch.setattr(session.wake, "wait", wait)
    controller._monitor(session)
    assert not session.error and remote.adds
    assert len(workers) == 1
    cached = session.candidates
    assert cached.now_sampled_at == clock[0] and cached.now_posterior == {}
    if invalidation == "disabled_gain":
        assert cached.now_gain == 0 and not reads
    cached.configure({**radio_tuning.DEFAULTS, "theme": 0})
    cached.rng.seed(3)
    cached_style = cached.next_track([])["genre"]
    fresh = RadioCandidates(store, clock[0], cached.tuning)
    fresh.rng.seed(3)
    assert cached_style == fresh.next_track([])["genre"]


def test_refresh_preserves_plans_and_rejects_concurrently_changed_policy(active, monkeypatch):
    controller, remote, workers, clock, reads = active
    session, store = controller.session, controller.ctx.store
    candidates = session.candidates
    before = json.dumps(session.directions.public([]), sort_keys=True)
    saved_tracks, saved_remote = list(session.tracks), list(remote.ids)
    clock[0] += radio_tuning.NOW_REFRESH_SECONDS + 1
    original = candidates.sample_now

    def policy_changed(now, *, store):
        sample = original(now, store=store)
        # The detached calculation must not expose partial state or block serving writes.
        assert candidates.now_sampled_at == 100_000
        rec_params.set_param(controller.ctx.store, "now_gain", 0)
        return sample

    monkeypatch.setattr(candidates, "sample_now", policy_changed)
    for _ in range(20):
        controller._refresh_now(session)
    assert len(workers) == 1 and not reads
    workers.pop()[0][0]()
    assert candidates.now_sampled_at == 100_000  # stale snapshot rejected
    assert not session.now_refreshing
    controller._refresh_now(session)
    assert not workers  # bounded retry after a conflicting write
    monkeypatch.setattr(candidates, "sample_now", original)
    clock[0] += radio_tuning.NOW_REFRESH_SECONDS
    controller._refresh_now(session)
    workers.pop()[0][0]()
    assert candidates.now_gain == 0 and candidates.now_posterior == {}
    assert candidates.now_sampled_at == clock[0]
    assert json.dumps(session.directions.public([]), sort_keys=True) == before
    assert session.tracks == saved_tracks and remote.ids == saved_remote


@pytest.mark.parametrize("change", ["stop", "replacement", "retune"])
def test_obsolete_session_sample_is_not_published(active, change):
    controller, _, workers, clock, _ = active
    session = controller.session
    candidates = session.candidates
    clock[0] += radio_tuning.NOW_REFRESH_SECONDS + 1
    controller._refresh_now(session)
    assert len(workers) == 1
    if change == "stop":
        controller.stop()
    elif change == "retune":
        session.candidates = RadioCandidates(controller.ctx.store, clock[0], now_source=candidates)
    else:
        controller.session = None
    workers.pop()[0][0]()
    assert candidates.now_sampled_at == 100_000
    assert not session.now_refreshing
