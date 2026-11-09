"""A refresh cannot replace a worker's complete board with a thinner result."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.derived_rating_support import seed_dislike
from tests.home_fixture_support import publish_cards, service
from tests.home_prepared_support import seed
from tests.recommendation_support import publish_bundles
from tests.store_fixture_support import (
    bounded_consumer_schema, native_sql_calls, uncontended_file_consumer_store as store,
    without_query_timing,
)
from yt_playlist.rec import home_cache, home_cards, home_prepared, measurement


pytestmark = pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Retention owns private SQL/publications without a web lifecycle."""


@pytest.fixture(scope='module')
def _pristine_current_schema():
    genres = ('Jazz', 'Techno', 'Classical', 'Ambient', 'Trance', 'Indie Rock')
    with bounded_consumer_schema(tuple((genre.casefold(), genre) for genre in genres),
                                 snapshot_guards=False) as schema:
        yield schema


@pytest.fixture
def candidates(store, monkeypatch):
    # Retention owns the decision between completed producer results, not their
    # ranking. test_stored_home retains real typed assembly, hydration and
    # eligibility checks. Keep real subjects, typed heads and Home publication.
    # Policy fingerprint calculation is another lower producer; policy rows,
    # candidate stamps and their publication still use the real implementation.
    monkeypatch.setattr(measurement, 'serving_policy',
                        lambda *args, **kwargs: ('completed-policy', {'schema': 4, 'fixture': 'retention'}))
    # Keep the original six modes, four lanes and four distinct songs per
    # lane. Only the lower ranker's completed four-card selection is fixed.
    bundles = seed(store, tracks_per_lane=4, publish=False)
    protos = []
    for mid, lane in enumerate(('wheelhouse', 'explore', 'comfort', 'fresh')):
        proto = home_cards._proto(store, lane, f'{mid} {lane}',
                                  deepcopy(bundles[str(mid)][lane]), 1000)
        proto['mode_id'] = mid
        proto['recipe'] = {'lane': lane, 'mode_id': mid}
        protos.append(proto)
    result = home_cards.CardsResult(protos, [{'proto': proto, 'pool': None} for proto in protos],
                                   None, None, [], [], 0, None)
    completed = publish_cards(store, result, 1000)
    completed['generation'] = 'completed-worker-generation'
    home_cache.put(store, home_prepared.SURFACE, completed, 1000)
    state = SimpleNamespace(bundles=bundles, lanes=set(bundles['all']), completed=completed)
    publish_bundles(store, bundles, 1000)

    def cards(self, request):
        protos = [deepcopy(proto) for proto in completed['menus'][0]['protos']
                  if proto['lane'] in state.lanes]
        return home_cards.CardsResult(protos, [{'proto': proto, 'pool': None} for proto in protos],
                                      None, None, [], [], request.epoch, None)

    monkeypatch.setattr(home_cards.HomeCards, 'cards', cards)
    return state


@pytest.fixture
def prepared(candidates):
    return candidates.completed


def thin_candidates(store, candidates):
    thin = deepcopy(candidates.bundles)
    for identity, bucket in thin.items():
        if not identity.startswith('_'):
            for lane in bucket:
                if lane != 'comfort':
                    bucket[lane] = []
    publish_bundles(store, thin, 1002)
    candidates.lanes = {'comfort'}


def test_partial_candidate_refresh_preserves_the_complete_worker_board(store, candidates, prepared):
    before = home_prepared.cards(store, prepared, 1001, service(store), record=False)
    assert len(before.protos) == 4
    old_videos = {track['video_id'] for card in before.protos for track in card['tracks']}
    # The library and feedback do not change. Only the replacement candidate
    # publication is incomplete, as can happen during staged preparation.
    original = deepcopy(candidates.bundles)
    thin_candidates(store, candidates)
    assert home_prepared.prepare_mixes(store, 1003) is None
    assert store.rec_proposals.get_proposals(home_prepared.SURFACE) == prepared
    selected = home_prepared.load(store, 1004)
    after = home_prepared.cards(store, selected, 1004, service(store), record=False)
    assert len(after.protos) == 4
    assert old_videos == {track['video_id'] for card in after.protos for track in card['tracks']}
    # An ordinary complete refresh can still replace it and advance its inputs.
    publish_bundles(store, original, 1005)
    candidates.lanes = set(original['all'])
    replacement = home_prepared.prepare_mixes(store, 1006)
    assert replacement and replacement['generation'] != prepared['generation']
    assert len(home_prepared.cards(store, replacement, 1006, service(store), record=False).protos) == 4


def test_feedback_and_availability_do_not_discard_a_complete_worker_board(store, candidates, prepared):
    # Freeze the displayed choice; an unrecorded draw has new exposure metadata
    # and may select another worker menu after its mode evidence changes.
    before = home_prepared.cards(store, prepared, 1000, service(store))
    keys = {track['key'] for menu in prepared['menus'] for card in menu['protos']
            if card['lane'] != 'comfort' for track in card['tracks']}
    assert keys
    for key in keys:
        seed_dislike(store, key, until=None, now=1001)
    with store.conn:
        store.conn.executemany('UPDATE youtube_items SET available=0 WHERE recording_id='
            '(SELECT id FROM recordings WHERE identity_key=?)', [(key,) for key in keys])
    assert home_prepared.cards(store, prepared, 1001, service(store), record=False).protos == before.protos
    thin_candidates(store, candidates)
    replacement = home_prepared.prepare_mixes(store, 1003)
    assert replacement is None
    current = home_prepared.cards(store, prepared, 1004, service(store), record=False)
    assert current.protos == before.protos
    assert store.rec_proposals.get_proposals(home_prepared.SURFACE) == prepared


def test_first_partial_menu_is_published_without_an_existing_complete_board(store, candidates, prepared):
    store.rec_proposals.put_proposals(home_prepared.SURFACE, None, 1001)
    thin_candidates(store, candidates)
    result = home_prepared.prepare_mixes(store, 1003)
    assert result
    assert len(home_prepared.cards(store, result, 1004, service(store), record=False).protos) == 1
