"""Serving presence must stay bounded, and prepared writes retain outer ownership."""
import json

import pytest

from yt_playlist.core import (choice_learning_state, home_offers, home_outputs,
    listening_outputs, model_comparison_state, recommendation_requests,
    recommendation_work, tv_recommendation_state)


@pytest.mark.parametrize('surface,value,module,reader,empty_present', [
    ('tv_pool', {'at': 10}, tv_recommendation_state, 'read', False),
    ('model_live_comparison', {'phase': 'preparing'}, model_comparison_state, 'read', False),
    ('choice_bandit', {'rows': []}, choice_learning_state, 'read', False),
    ('home_genre_request', {'epoch': 1, 'held_by_vibe': False}, recommendation_requests, 'read', False),
    ('album_menu_refresh', {'epoch': 1, 'exclude': []}, recommendation_requests, 'read', False),
    ('home_shown', {'epoch': 1, 'signatures': []}, home_offers, 'read', False),
    ('tv_preparation', {'failed': False}, recommendation_work, 'read', False),
    ('listening_edition', {'text': 'Ready'}, listening_outputs, 'encoded', True),
    ('home_prepared', {'tracks': []}, home_outputs, 'encoded', False),
])
def test_native_presence_does_not_read_display_content(store, monkeypatch, surface,
                                                      value, module, reader, empty_present):
    store.rec_proposals.put_proposals(surface, value, 10)
    assert store.rec_proposals.get_proposals(surface) == value

    def forbidden(*args, **kwargs):
        pytest.fail('Presence read a display document')

    with monkeypatch.context() as patch:
        patch.setattr(module, reader, forbidden)
        assert store.rec_proposals.has_proposals(surface)
    store.rec_proposals.put_proposals(surface, {}, 11)
    with monkeypatch.context() as patch:
        patch.setattr(module, reader, forbidden)
        assert bool(store.rec_proposals.has_proposals(surface)) == empty_present


@pytest.mark.parametrize('surface,original,replacement', [
    ('home_genre_request', {'epoch': 1, 'include_genres': ['unknown']}, {'epoch': 2, 'held_by_vibe': False}),
    ('tv_pool', {'at': 10, 'items': []}, {'at': 11, 'items': []}),
    ('home_prepared', {'tracks': [{'title': 'Original'}]}, {'tracks': [{'title': 'Replacement'}]}),
])
def test_encoded_publication_keeps_caller_transaction_and_rollback(store, surface, original, replacement):
    store.rec_proposals.put_proposals(surface, original, 10)
    with pytest.raises(RuntimeError, match='interrupt publication'):
        with store._lock, store.conn.atomic():
            store.rec_proposals.publish_prepared_home(json.dumps(replacement), 11, surface=surface)
            assert store.conn.in_transaction
            assert store.rec_proposals.get_proposals(surface) == replacement
            raise RuntimeError('interrupt publication')
    assert store.rec_proposals.get_proposals(surface) == original
    assert not store.conn.in_transaction
