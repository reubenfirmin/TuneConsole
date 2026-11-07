"""Temporary suppression expiry restores original rankings without refitting."""
import logging
from types import SimpleNamespace

import numpy as np
import pytest

from tests.candidate_timing_support import candidate_costs, candidate_prefix_costs
from tests.conftest import seed_catalog_track
from tests.rec.test_model_refresh import refresh_costs
from tests.prepared_fixture_support import prepared_template
from tests.rec.test_candidate_refresh import candidate, no_fit
from yt_playlist.core.store import Store
from yt_playlist.rec import candidate_refresh, embedding_training as embed, mode_surfaces
from yt_playlist.rec.rec_worker import RecWorker


@pytest.fixture(scope='module')
def expiry_rankers():
    from yt_playlist.rec import measurement, rec_params, surfaces, taste_modes, vibe_pools

    # The subject is suppression expiry in Wheelhouse. Keep its real eligibility,
    # policy capture and typed publication; other lanes/presets have independent
    # ranking owners and need not be republished for every expiry transition.
    with pytest.MonkeyPatch.context() as patch:
        rank = surfaces.for_you
        patch.setattr(surfaces, 'for_you',
            lambda store, now, **kw: rank(store, now, limit=1))
        # Fitted scores are a lower-layer input. Their derivation from playlist
        # taste, decayed weights and transient mood is owned by test_recommend,
        # test_playlist_taste and test_mood. The real ranker reads SQL candidates,
        # applies suppression and excludes removed/unavailable recordings.
        patch.setattr(surfaces, 'playlist_taste', lambda *a, **kw: object())
        patch.setattr(surfaces, '_score_candidates',
            lambda store, pt, keys, vectors, index, now, **kw:
                {key: float(i) for i, key in enumerate(keys)})
        for name in ('explore_for_you', 'comfort_listening', 'cold_candidates'):
            patch.setattr(surfaces, name, lambda *a, **kw: [])
        # Expiry applies to the complete Wheelhouse pool independently of mode
        # attribution. Mode buckets, Radio membership and temporal lanes have
        # their own publication checks in test_ranking_playback_publication.
        patch.setattr(taste_modes, 'live_modes', lambda *a, **kw: [])
        patch.setattr(vibe_pools, 'prepare', lambda *a, **kw: None)
        # Retiring unrelated old policies is not part of feedback expiry. The
        # retention and policy-storage modules own that lower-layer operation.
        patch.setattr(measurement, 'maintain_storage', lambda *a, **kw: None)
        # Model/policy fingerprinting is independently covered by
        # test_measurement_isolation and test_typed_policy_storage. Keep real
        # suppression reads, policy records and publication here.
        patch.setattr(measurement, 'serving_policy', lambda store, now, **kw:
            ('expiry-inputs', {'schema': 4, 'algorithm': 'expiry-fixture',
                              'suppressed': sorted(store.rec_model.suppressed_keys('for_you', now))}))
        # These checks never change result-shaping knobs. Their registry and
        # persisted overrides are covered by test_rec_params/candidate_refresh.
        defaults = {spec.name: int(spec.default) if spec.integer else spec.default
                    for spec in rec_params.PARAMS}
        patch.setattr(rec_params, 'get_param', lambda store, name: defaults[name])
        calculate = mode_surfaces._calculate_bundles

        def wheelhouse(reader, now, policies):
            payload, _ = calculate(reader, now, policies)
            # Do not stage four unrelated empty surface pools on every clock
            # transition. The selected pool, durable policies and deadline
            # settings still pass through the actual typed publisher.
            return {'all': {'wheelhouse': payload['all']['wheelhouse']},
                    '_meta': payload['_meta']}, None

        patch.setattr(mode_surfaces, '_calculate_bundles', wheelhouse)
        yield


@pytest.fixture(scope='module')
def candidate_library_template(_pristine_current_schema, expiry_rankers):
    """Expiry needs ranked subjects and fitted vectors, not mode discovery.

    Keep three genres, overlapping playlists and real saved vectors/ranks. The
    mode-discovery threshold and full model/pool comparison remain covered by
    test_model_refresh and test_ranking_playback_publication.
    """
    def build(store):
        account = store.accounts.upsert_account('Listener', 'fixture', None, True)
        tids = [seed_catalog_track(store, f'expiry-{i}', f'Song {i}', f'Artist {i}',
                                  video_id=f'v{i}', genre=('Techno', 'Jazz', 'Rock')[i % 3])
                for i in range(3)]
        for i, members in enumerate((tids[:2], tids[1:])):
            playlist = store.playlists.upsert_playlist(account, f'p{i}', f'Playlist {i}', len(members), '', 1)
            store.playlists.set_playlist_tracks(playlist, members)
        store.history.import_play_events(account, [(f'expiry-{i}', f'v{i}', 1000 + i) for i in range(3)])
        # A completed fit is an input to expiry. Training itself is owned by
        # test_model_refresh; retain nonempty vectors so no-refit assertions
        # cannot pass merely because this fixture has no model.
        store.rec_model.replace_rec_vectors([
            (f'expiry-{i}', np.array([1., (i + 1) / 3], np.float32).tobytes())
            for i in range(3)])
        refresh(store)
        rows = store.recommendations.page('home', 'all', 'wheelhouse')
        assert rows
        return rows[0]['key']

    with prepared_template(_pristine_current_schema, build) as template:
        yield template


@pytest.fixture
def store(candidate_library_template):
    # Every case consumes already ranked inputs. Clone those once, including
    # their genuine guards/files, instead of initializing and replacing an
    # empty consumer before restoring the same prepared inputs again.
    with candidate_library_template.open(':memory:') as consumer:
        yield consumer


def refresh(store, now=2000):
    # Feedback and expiry change ranking eligibility, not fitted inputs. These
    # cases own real reranking/publication; model rebuilding has separate owners.
    assert mode_surfaces.prepare_bundles(store, now) is not None


def snooze(store, key, until=3000.1, now=2100):
    store.rec_model.record_feedback('for_you', key, 'not_now', until=until, now=now)
    refresh(store, now)
    assert candidate(store, key) is None


def test_expiry_restores_candidate_without_source_edit_or_fitting(store, monkeypatch, candidate_library_template):
    key = candidate_library_template.result
    snooze(store, key)
    vectors = store.rec_model.get_rec_vectors()
    monkeypatch.setattr(embed, 'build_vectors_from_baskets', no_fit)
    assert not candidate_refresh.due(store, 3000)
    assert candidate_refresh.due(store, 3000.1)
    refresh(store, 3000.1)
    assert candidate(store, key)
    assert store.rec_model.get_rec_vectors() == vectors
    assert not candidate_refresh.due(store, 3000.1)


@pytest.mark.parametrize('permanent', [False, True])
def test_old_deadline_respects_renewal_and_permanent_blocks(
        store, monkeypatch, permanent, candidate_library_template):
    key = candidate_library_template.result
    snooze(store, key)
    store.rec_model.record_feedback('for_you', key, 'dismiss' if permanent else 'not_now',
                                   until=None if permanent else 4000, now=2500)
    refresh(store, 2500)
    monkeypatch.setattr(embed, 'build_vectors_from_baskets', no_fit)
    refresh(store, 3100)
    assert candidate(store, key) is None
    refresh(store, 4000)
    assert bool(candidate(store, key)) == (not permanent)


@pytest.mark.parametrize('removed', [False, True])
def test_expiry_preserves_unavailable_and_removed_sources(
        store, monkeypatch, removed, candidate_library_template):
    key = candidate_library_template.result
    snooze(store, key)
    with store.conn:
        if removed:
            store.conn.execute('DELETE FROM library_memberships WHERE entry_id IN '
                '(SELECT id FROM catalog_entries WHERE recording_id='
                '(SELECT id FROM recordings WHERE identity_key=?))', (key,))
        else:
            store.conn.execute('UPDATE youtube_items SET available=0 WHERE recording_id='
                '(SELECT id FROM recordings WHERE identity_key=?)', (key,))
    refresh(store, 2500)
    monkeypatch.setattr(embed, 'build_vectors_from_baskets', no_fit)
    refresh(store, 3100)
    assert candidate(store, key) is None


def test_expiry_survives_restart(tmp_path, monkeypatch, candidate_library_template):
    path = tmp_path / 'expiry.db'
    with candidate_library_template.open(path) as store:
        key = candidate_library_template.result
        snooze(store, key)
    monkeypatch.setattr(embed, 'build_vectors_from_baskets', no_fit)
    store = Store(path)
    try:
        store.init_schema()
        assert candidate_refresh.due(store, 3100)
        refresh(store, 3100)
        assert candidate(store, key)
    finally:
        store.conn.close()


def test_idle_worker_notices_expiry(store, monkeypatch, candidate_library_template):
    key = candidate_library_template.result
    snooze(store, key)
    worker = RecWorker(SimpleNamespace(store=store, now_fn=lambda: 3100,
        logger=logging.getLogger(__name__)), enabled=False)
    ticks = iter((False, True))
    monkeypatch.setattr(worker._stop, 'wait', lambda timeout: next(ticks))
    monkeypatch.setattr(worker.projections, 'poll', lambda: None)
    # This check isolates suppression expiry from independent content preparation.
    monkeypatch.setattr(worker.preparation, 'due', lambda now: False)
    monkeypatch.setattr(worker.preparation, 'full_due', lambda now: False)
    monkeypatch.setattr(worker, '_poll_choices', lambda: None)
    monkeypatch.setattr(worker, '_poll_evidence_storage', lambda: None)
    monkeypatch.setattr(worker, '_poll_retention', lambda: None)
    monkeypatch.setattr(worker, '_poll_tv', lambda: None)
    triggers = []
    monkeypatch.setattr(worker, '_schedule', lambda **kwargs: triggers.append(kwargs))
    worker._maintenance_loop()
    assert triggers == [{'full': False, 'bundles': True}]
