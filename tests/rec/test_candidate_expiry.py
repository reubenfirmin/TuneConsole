"""Temporary suppression expiry restores original rankings without refitting."""
import logging
from types import SimpleNamespace

import pytest

from tests.rec.test_candidate_refresh import candidate, library, no_fit, refresh
from yt_playlist.core.store import Store
from yt_playlist.rec import candidate_refresh, embedding_training as embed
from yt_playlist.rec.rec_worker import RecWorker


def snooze(store, key, until=3000.1, now=2100):
    store.rec_model.record_feedback('for_you', key, 'not_now', until=until, now=now)
    refresh(store, now)
    assert candidate(store, key) is None


def test_expiry_restores_candidate_without_source_edit_or_fitting(store, monkeypatch):
    key = library(store)
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
def test_old_deadline_respects_renewal_and_permanent_blocks(store, monkeypatch, permanent):
    key = library(store)
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
def test_expiry_preserves_unavailable_and_removed_sources(store, monkeypatch, removed):
    key = library(store)
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


def test_expiry_survives_restart(tmp_path, monkeypatch):
    path = tmp_path / 'expiry.db'
    store = Store(path)
    store.init_schema()
    key = library(store)
    snooze(store, key)
    store.conn.close()
    monkeypatch.setattr(embed, 'build_vectors_from_baskets', no_fit)
    store = Store(path)
    try:
        store.init_schema()
        assert candidate_refresh.due(store, 3100)
        refresh(store, 3100)
        assert candidate(store, key)
    finally:
        store.conn.close()


def test_idle_worker_notices_expiry(store, monkeypatch):
    key = library(store)
    snooze(store, key)
    worker = RecWorker(SimpleNamespace(store=store, now_fn=lambda: 3100,
        logger=logging.getLogger(__name__)), enabled=False)
    ticks = iter((False, True))
    monkeypatch.setattr(worker._stop, 'wait', lambda timeout: next(ticks))
    monkeypatch.setattr(worker.projections, 'poll', lambda: None)
    # This check isolates suppression expiry from independent content preparation.
    monkeypatch.setattr(worker.preparation, 'due', lambda now: False)
    triggers = []
    monkeypatch.setattr(worker, '_schedule', lambda **kwargs: triggers.append(kwargs))
    worker._maintenance_loop()
    assert triggers == [{'full': False, 'bundles': True}]
