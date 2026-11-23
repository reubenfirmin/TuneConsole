"""Demand has explicit ownership and survives disposable output replacement."""
import json
import sqlite3

import pytest

from yt_playlist.core.migrations import current
from yt_playlist.rec import mix_tuning


def test_demand_publication_participates_in_an_enclosing_transaction(store):
    assert store.rec_proposals.get_proposals('home_genre_request') is None
    store.rec_proposals.put_proposals('home_genre_request', {'epoch': 1, 'include_genres': ['unknown']}, 10)
    store.conn.execute('BEGIN IMMEDIATE')
    store.rec_proposals.publish_prepared_home(json.dumps({'epoch': 2, 'avoid_genres': ['jazz']}), 11,
                                               surface='home_genre_request')
    store.conn.rollback()
    assert store.rec_proposals.get_proposals('home_genre_request') == {'epoch': 1, 'include_genres': ['unknown']}
    store.rec_proposals.put_proposals('home_genre_request', {}, 12)
    assert store.rec_proposals.get_proposals('home_genre_request') == {}
    current.validate(store.conn)


def test_home_request_rejects_invalid_demand_lists_and_retired_proposal_storage(store):
    request = {'epoch': 1, 'include_genres': ['jazz']}
    store.rec_proposals.put_proposals('home_genre_request', request, 10)
    with pytest.raises(sqlite3.IntegrityError), store.conn:
        store.conn.execute("INSERT INTO rec_proposals VALUES ('home_genre_request','{}',1)")
    for invalid in (
        {'epoch': 1, 'include_genres': {}},
        {'epoch': 1, 'avoid_genres': 42},
        {'epoch': None, 'include_genres': ['jazz']},
    ):
        with pytest.raises(ValueError):
            store.rec_proposals.put_proposals('home_genre_request', invalid, 11)
        assert store.rec_proposals.get_proposals('home_genre_request') == request


def test_mix_publication_guards_only_the_home_settings_it_uses(store):
    def read_controls(snapshot):
        snapshot.store.rec_proposals.get_proposals('home_genre_request')
        snapshot.store.rec_proposals.vibe_request()

    with store.read_snapshot(dependencies=mix_tuning.PUBLICATION_INPUTS) as snapshot:
        read_controls(snapshot)
        store.rec_proposals.put_proposals('home_shown', {'epoch': 3, 'signatures': ['offer']}, 10)
        store.rec_proposals.save_revisit_remaining(['rediscover'])
        assert snapshot.publish(lambda: None)
    with store.read_snapshot(dependencies=mix_tuning.PUBLICATION_INPUTS) as snapshot:
        read_controls(snapshot)
        store.rec_proposals.put_proposals('home_genre_request', {'epoch': 3, 'avoid_genres': ['jazz']}, 11)
        assert not snapshot.publish(lambda: pytest.fail('Published with a changed genre request'))
    with store.read_snapshot(dependencies=mix_tuning.PUBLICATION_INPUTS) as snapshot:
        read_controls(snapshot)
        store.rec_proposals.save_vibe_request({'id': 'new', 'vibe': 'calm', 'phase': 'pending'})
        assert not store.rec_proposals.finish_vibe_request('old', 'failed')
        assert store.rec_proposals.vibe_request()['phase'] == 'pending'
        assert not snapshot.publish(lambda: pytest.fail('Published with a changed Vibe request'))
