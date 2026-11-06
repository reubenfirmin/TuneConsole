"""A refresh cannot replace an eligible complete board with a thinner result."""
from copy import deepcopy
from tests.derived_rating_support import seed_dislike

import pytest

from tests.rec.test_home_prepared import prepared, service, store
from tests.recommendation_support import publish_bundles
from yt_playlist.rec import home_prepared


def thin_candidates(store):
    thin = deepcopy(store._fixture_bundle_payload)
    # This publication replaces Home candidates. Reusing the earlier Vibes
    # rankings would carry policy stamps from before the feedback change.
    thin.pop('_vibes', None)
    for identity, bucket in thin.items():
        if not identity.startswith('_'):
            for lane in bucket:
                if lane != 'comfort':
                    bucket[lane] = []
    publish_bundles(store, thin, 1002)


def test_partial_candidate_refresh_preserves_the_eligible_complete_board(store, prepared):
    before = home_prepared.cards(store, prepared, 1001, service(store), record=False)
    assert len(before.protos) == 4
    old_videos = {track['video_id'] for card in before.protos for track in card['tracks']}
    # The library and feedback do not change. Only the replacement candidate
    # publication is incomplete, as can happen during staged preparation.
    original = deepcopy(store._fixture_bundle_payload)
    # Recovery needs four playable lanes, not a second preparation of every
    # Vibes preset. Vibes publication/selection has its own integration checks.
    original.pop('_vibes', None)
    thin_candidates(store)
    assert home_prepared.prepare(store, 1003) is None
    assert store.rec_proposals.get_proposals(home_prepared.SURFACE) == prepared
    selected = home_prepared.load(store, 1004)
    after = home_prepared.cards(store, selected, 1004, service(store), record=False)
    assert len(after.protos) == 4
    assert old_videos == {track['video_id'] for card in after.protos for track in card['tracks']}
    # An ordinary complete refresh can still replace it and advance its inputs.
    publish_bundles(store, original, 1005)
    replacement = home_prepared.prepare(store, 1006)
    assert replacement and replacement['generation'] != prepared['generation']
    assert len(home_prepared.cards(store, replacement, 1006, service(store), record=False).protos) == 4


@pytest.mark.parametrize('reason', ['dislike', 'unavailable'])
def test_partial_refresh_does_not_retain_an_ineligible_old_board(store, prepared, reason):
    keys = {track['key'] for menu in prepared['menus'] for card in menu['protos']
            if card['lane'] != 'comfort' for track in card['tracks']}
    assert keys
    if reason == 'dislike':
        for key in keys:
            seed_dislike(store, key, until=None, now=1001)
    else:
        with store.conn:
            store.conn.executemany('UPDATE youtube_items SET available=0 WHERE recording_id='
                '(SELECT id FROM recordings WHERE identity_key=?)', [(key,) for key in keys])
    assert len(home_prepared.cards(store, prepared, 1001, service(store), record=False).protos) < 4
    thin_candidates(store)
    replacement = home_prepared.prepare(store, 1003)
    assert replacement and replacement['generation'] != prepared['generation']
    current = home_prepared.cards(store, replacement, 1004, service(store), record=False)
    assert len(current.protos) == 1
    assert keys.isdisjoint(track['key'] for card in current.protos for track in card['tracks'])


def test_first_partial_menu_is_published_without_an_existing_complete_board(store, prepared):
    store.rec_proposals.put_proposals(home_prepared.SURFACE, None, 1001)
    thin_candidates(store)
    result = home_prepared.prepare(store, 1003)
    assert result
    assert len(home_prepared.cards(store, result, 1004, service(store), record=False).protos) == 1
