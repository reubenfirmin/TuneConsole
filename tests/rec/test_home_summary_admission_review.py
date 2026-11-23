"""Ready mixes must leave independent Home summary preparation reachable."""
import logging
import time
from types import SimpleNamespace

import pytest

from tests.rec.test_home_prepared import cached_menu, cached_menu_schema, store
from yt_playlist.core import fao, served_facts
from yt_playlist.rec import album_choices, album_tuning, home_cache, home_prepared, measurement, surfaces
from yt_playlist.rec.worker_tasks import ServingTasks


@pytest.mark.parametrize('summary', ['discovery', 'revisit'])
def test_ready_mix_batch_does_not_strand_missing_home_summary(
        store, cached_menu, monkeypatch, summary):
    now = 1001
    assert home_prepared.has_prepared_batch(store, now)
    surface = (home_prepared.DISCOVERY_SURFACE if summary == 'discovery'
               else home_prepared.REVISIT_SURFACE)
    assert store.rec_proposals.get_proposals(surface) is None
    store.discovery.upsert_discovered_album('review-album', 'Artist', 'New album', '2026', None, 1)
    # This case owns scheduling and real summary publication. History ranking,
    # album selection and reporting remain controlled collaborators.
    for name in ('rediscover_playlists', 'rediscover_albums', 'rediscover_tracks'):
        monkeypatch.setattr(surfaces, name, lambda *args, **kwargs: [])
    monkeypatch.setattr(album_tuning, 'state', lambda *args: {'values': {}, 'genres': []})
    monkeypatch.setattr(album_choices, 'selection', lambda *args, **kwargs: (None, [], None))

    def forbidden_mix(*args):
        pytest.fail('A complete mix batch must not be rebuilt to prepare a summary')

    monkeypatch.setattr(home_prepared, 'prepare_mixes', forbidden_mix)

    def stage(name, target, *args, **kwargs):
        if target in (home_prepared.prepare, home_prepared.prepare_mixes,
                      home_prepared.prepare_discovery, home_prepared.prepare_revisit):
            return target(*args, **kwargs)
        return {'items': []} if name == 'Album choices' else False

    tasks = ServingTasks(SimpleNamespace(store=store, now_fn=lambda: now,
        client_provider=lambda: {}, logger=logging.getLogger(__name__)), stage)
    tasks.home()
    actual = store.rec_proposals.get_proposals(surface)
    if actual is None:
        # Establish that summary publication itself works on this same input;
        # the missing result must come from admission, not an unusable fixture.
        prepare = (home_prepared.prepare_discovery if summary == 'discovery'
                   else home_prepared.prepare_revisit)
        prepare(store, now)
        assert store.rec_proposals.get_proposals(surface) is not None
    assert home_cache.read(store, home_prepared.SURFACE) == cached_menu
    assert actual is not None, f'A ready mix batch stranded missing {summary} preparation'


def test_home_publishes_discovery_after_diagnostic_disappears_during_cleanup(store, monkeypatch):
    now = time.time() + served_facts.RETAIN_SECONDS + 10
    previous = {'discovery_albums': [{'browse_id': 'old-album', 'artist': 'Old artist'}],
                'discovery_artists': [], 'discovery_tracks': []}
    store.rec_proposals.put_proposals(home_prepared.DISCOVERY_SURFACE, previous, now - 1000)
    store.discovery.upsert_discovered_album('new-album', 'Artist', 'New album', '2026', None, now)
    files = store.rec_ledger.impressions.files
    files.publish_many([(1, 100, {}), (2, 100, {})])
    vanished = files.root / served_facts.relative(1)
    remaining = files.root / served_facts.relative(2)
    original = fao.read

    def remove_before_stat(path):
        if path == vanished:
            vanished.unlink()
        return original(path)

    monkeypatch.setattr(fao, 'read', remove_before_stat)
    monkeypatch.setattr(album_choices, 'selection', lambda *args, **kwargs: (None, [], None))

    def stage(name, target, *args, **kwargs):
        if target is measurement.maintain_storage:
            return target(*args, **kwargs)
        if target is home_prepared.prepare_discovery:
            assert home_prepared.load_discovery(store) == previous
            assert not vanished.exists() and not remaining.exists()
            return target(*args, **kwargs)
        return {'items': []} if name == 'Album choices' else False

    tasks = ServingTasks(SimpleNamespace(store=store, now_fn=lambda: now, client_provider=lambda: {}), stage)
    tasks.home()
    actual = home_prepared.load_discovery(store)
    assert [item['browse_id'] for item in actual['discovery_albums']] == ['new-album']


def test_new_candidate_heads_do_not_refill_a_ready_home_board(store, cached_menu, monkeypatch):
    from tests.recommendation_support import publish_bundles, seed_bundle_subjects

    monkeypatch.setattr(album_tuning, 'state', lambda *args: {'values': {}, 'genres': []})
    monkeypatch.setattr(album_choices, 'selection', lambda *args, **kwargs: (None, [], None))

    def stage(name, target, *args, **kwargs):
        if name == 'Home collections':
            pytest.fail('Candidate catch-up must reuse an already useful Home board')
        if name == 'Album choices':
            return {'items': []}
        if target is home_prepared.prepare_discovery:
            return {}
        if target is home_prepared.prepare_revisit:
            return True
        return False

    tasks = ServingTasks(SimpleNamespace(store=store, now_fn=lambda: 1001,
        client_provider=lambda: {}, logger=logging.getLogger(__name__)), stage)
    previous = home_prepared._proposal_generation(store)
    for index in range(3):
        fresh = [{'key': f'catchup {index}|artist', 'title': f'Catchup {index}',
                  'artist': 'Artist', 'video_id': f'catchup-{index}'}]
        seed_bundle_subjects(store, {'all': {}}, fresh=fresh)
        publish_bundles(store, {'all': {}}, now=1001, fresh=fresh)
        current = home_prepared._proposal_generation(store)
        assert current != previous
        assert tasks.home() is False
        assert home_cache.read(store, home_prepared.SURFACE) == cached_menu
        previous = current
