"""Current-listening policy is maintained without a visible/polling Radio page."""
import json
import time
from dataclasses import replace

import pytest

from tests.group_support import publish, recordings
from tests.radio_session_support import controlled_radio_checkpoints, poc, play
from tests.store_fixture_support import bounded_consumer_schema, initialize_consumer, reuse_reporting_reader
from tests.recommendation_support import publish_bundles, seed_publication_subjects
from yt_playlist.core.store import Store
from yt_playlist.rec import embedding_vectors as embed, measurement, radio_tuning, rec_params
from yt_playlist.rec.radio_candidates import RadioCandidates
from yt_playlist.web import radio_session


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These controller tests construct no HTTP application or sync service."""


@pytest.fixture(scope='module')
def now_schema():
    # NOW consumes two published track groups. Taxonomy and startup have their own
    # owners. Keep actual candidate SQL and connection-local revision guards.
    with bounded_consumer_schema((('ambient', 'Ambient'), ('techno', 'Techno'))) as schema:
        yield schema



def prepare_now_candidates(store):
    corpus = recordings([('Ambient', 6, .1), ('Techno', 6, .9)])
    tracks = [{'key': f'track-{index}|artist {index}', 'video_id': f'track-{index}',
               'title': f'Track {index}', 'artist': f'Artist {index}', 'genre': track.genre}
              for index, track in enumerate(corpus)]
    bundles = {'_radio': {'space': embed.content_space_id(store), 'members': {}}}
    seed_publication_subjects(store, tracks)
    head = publish(store, [replace(track, key=row['key']) for track, row in zip(corpus, tracks)])
    membership = store.taste_groups.leaf_members(head['generation'])
    assert len(set(membership.values())) == 2
    for track in tracks:
        version = membership[track['key']]
        bundle = bundles.setdefault(version, {'wheelhouse': [], 'fresh': []})
        lane = 'wheelhouse' if len(bundle['wheelhouse']) < 3 else 'fresh'
        bundle[lane].append(track)
        bundles['_radio']['members'].setdefault(version, []).append(track['key'])
    # Policy hashing is a lower-layer input to these NOW refresh cases. The
    # real NOW policy (including the concurrent gain change) remains live.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(measurement, 'serving_policy', lambda *args, **kwargs:
                      ('now-fixture', {'schema': 4, 'algorithm': 'now-fixture'}))
        publish_bundles(store, bundles)
    return tracks


@pytest.fixture
def store(now_schema, tmp_path, monkeypatch):
    source = Store(tmp_path / 'now.db')
    try:
        initialize_consumer(source, now_schema)
        source.conn.execute('PRAGMA synchronous=NORMAL')
        source.conn.execute('PRAGMA wal_autocheckpoint=0')
        source._fixture_radio_pool = []
        with reuse_reporting_reader(source, monkeypatch):
            yield source
    finally:
        source.conn.close()


@pytest.fixture
def prepared(store, monkeypatch):
    tracks = prepare_now_candidates(store)
    entries = store.tracks.track_ids_for_videos([track['video_id'] for track in tracks])
    # Playlist confirmation consumes these already observed uploads. Preserve
    # actual membership/publication SQL without repeating metadata ingestion.
    monkeypatch.setattr(store.tracks, 'upsert_track', lambda video, *args, **kwargs: entries[video])
    return tracks


@pytest.fixture
def active(controlled_radio_checkpoints, poc, prepared, monkeypatch):
    controller, remote, _, workers, _ = poc
    store = controller.ctx.store
    # Ordinary reads are lower-layer inputs; the refresh worker retains its
    # real isolated read_snapshot and revision fencing. Reader lifetime has
    # separate coverage in core/test_reporting_reads.
    clock = [100_000.]
    controller.ctx.now_fn = lambda: clock[0]
    monkeypatch.setattr(radio_session, "RadioCandidates", RadioCandidates)
    neutral = RadioCandidates(store, clock[0], {**radio_tuning.DEFAULTS, "theme": 0})
    neutral.rng.seed(3)
    neutral_genre = neutral.next_track([])["genre"]
    preferred = [t for t in prepared if t["genre"] != neutral_genre][:3]
    reads = []

    def vectors(*args, **kwargs):
        reads.append(1)
        pytest.fail("Radio must not load the complete content matrix")

    monkeypatch.setattr(embed, "load_content_vectors", vectors)
    iid = store.accounts.get_master_account().id
    store.history.import_play_events(iid, [(track['key'], track['video_id'], clock[0] - i * 60)
                                         for i, track in enumerate(preferred)])
    assert controller.start()["ok"]
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
        clock[0] += 24 * 3600
    else:
        rec_params.set_param(store, "now_gain", 0)
        rec_params.set_param(store, "session_alpha", 0)
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
    cached_genre = cached.next_track([])["genre"]
    fresh = RadioCandidates(store, clock[0], cached.tuning)
    fresh.rng.seed(3)
    assert cached_genre == fresh.next_track([])["genre"]


def test_refresh_preserves_plans_and_rejects_concurrently_changed_policy(active, monkeypatch):
    controller, remote, workers, clock, reads = active
    session, store = controller.session, controller.ctx.store
    candidates = session.candidates
    before = json.dumps(session.directions.public([]), sort_keys=True)
    saved_tracks, saved_remote = list(session.tracks), list(remote.ids)
    clock[0] += radio_tuning.NOW_REFRESH_SECONDS + 1
    original = candidates.sample_now
    today_gain = rec_params.get_param(store, "session_alpha")

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
    assert candidates.now_gain == today_gain and candidates.now_posterior
    assert candidates.now_sampled_at == clock[0]
    assert json.dumps(session.directions.public([]), sort_keys=True) == before
    assert session.tracks == saved_tracks and remote.ids == saved_remote


def test_new_session_crosses_groups_without_any_fixed_modes(store, prepared):
    assert not store.modes.list_modes()
    c = RadioCandidates(store, 100_000, {**radio_tuning.DEFAULTS, 'theme': 0})
    try:
        assert set(c.pools) == set(store.taste_groups.leaf_members(
            store.taste_groups.head_generation()).values())
        seeds = c.seed_offer(2)
        assert len({c.cluster_for(track) for track in seeds}) == 2
        anchor = seeds[0]
        crossed = c.next_track([anchor])
        assert crossed and c.cluster_for(crossed) != c.cluster_for(anchor)
        alternate = c.next_track([anchor], excluded={crossed['video_id']})
        assert alternate and alternate['video_id'] not in {crossed['video_id'], anchor['video_id']}
        c.configure({**c.tuning, 'theme': 1})
        themed = c.next_track([anchor])
        assert themed and c.cluster_for(themed) == c.cluster_for(anchor)
    finally:
        c.close()


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
