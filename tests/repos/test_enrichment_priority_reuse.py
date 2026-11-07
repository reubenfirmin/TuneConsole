"""Queue priority reuse must preserve changing account inputs and current job gates."""
from contextlib import contextmanager
from tests.enrichment_support import complete_entries
from contextlib import closing
import sqlite3

import pytest

from tests.core.test_predecessor_writes import installed
from tests.test_enrich_queue import _play
from yt_playlist.core.activity import WorkClosing, cancellable_work
from yt_playlist.core.store import Store


@pytest.fixture
def queue(file_backed_store, monkeypatch):
    store = file_backed_store
    store.settings.set_setting('enrich_caught_up_at', '100')
    store.enrichment_state.reconcile_provider_configuration({
        'deezer': {'fingerprint': 'original', 'status': 'available'}})
    first = store.tracks.upsert_track('first', 'First', 'A', None, 200, created_at=1)
    second = store.tracks.upsert_track('second', 'Second', 'B', None, 200, created_at=1)
    anchor = store.tracks.upsert_track('anchor', 'Anchor', 'C', 'Album', 200,
                                     album_browse_id='AL', created_at=1)
    _play(store, 'anchor|c', 2)
    complete_entries(store, [anchor], 10)
    calculations = []
    original = store.read_snapshot

    @contextmanager
    def traced_reader():
        with original() as snapshot:
            def trace(sql):
                if sql.lstrip().startswith('WITH plays AS'):
                    calculations.append(sql)
            snapshot.store.conn.set_trace_callback(trace)
            yield snapshot

    monkeypatch.setattr(store, 'read_snapshot', traced_reader)
    monkeypatch.setattr(store.enrichment_queue, 'read_snapshot', traced_reader)
    return store, first, second, anchor, calculations


def names(store, **kwargs):
    return [row['title'] for row in store.enrichment_queue.next_enrich_batch(
        40, providers=['deezer'], now=1000, **kwargs)]


@pytest.mark.parametrize('change', ['plays', 'playlist', 'album', 'artist', 'arrival', 'boundary', 'removed'])
def test_reused_priority_changes_with_its_real_inputs(queue, change):
    store, first, second, anchor, calculations = queue
    if change == 'boundary':
        with store.conn:
            store.conn.execute('UPDATE recordings SET created_at=50 WHERE id=(SELECT recording_id FROM catalog_entries WHERE id=?)',
                               (second,))
        _play(store, 'first|a', 1)
    assert names(store) == ['First', 'Second']
    assert names(store) == ['First', 'Second']
    assert len(calculations) == 1

    expected = ['Second', 'First']
    if change == 'plays':
        _play(store, 'second|b', 3)
    elif change == 'playlist':
        account = store.accounts.upsert_account('main', 'cred', None, True)
        playlist = store.playlists.upsert_playlist(account, 'PL', 'Mix', 2, 'h', 1)
        store.playlists.set_playlist_tracks(playlist, [anchor, second])
    elif change == 'album':
        store.tracks.upsert_track('second', 'Second', 'B', 'Album', 200,
                                 album_browse_id='AL', created_at=1)
    elif change == 'artist':
        store.tracks.set_track_artist(second, 'C')
    elif change == 'arrival':
        store.discovery.upsert_discovered_track('arrival|d', 'arrival', 'Arrival', 'D', None,
                                               None, None, None, 'radio:s', 300)
        expected = ['Arrival', 'First', 'Second']
    elif change == 'boundary':
        store.settings.set_setting('enrich_caught_up_at', '10')
    elif change == 'removed':
        with store.conn:
            store.conn.execute('DELETE FROM library_memberships WHERE entry_id=?', (first,))
        expected = ['Second']
    assert names(store) == expected
    assert names(store) == expected
    assert len(calculations) == 2


def test_completion_and_provider_retries_reuse_priority_but_change_live_admission(queue):
    store, first, second, anchor, calculations = queue
    assert names(store) == ['First', 'Second']
    store.settings.set_setting('unrelated-interactive-choice', 'saved')
    store.enrichment_state.record_enrichment_attempt('first|a', 'deezer', {}, retry=True, now=1000)
    assert names(store) == ['Second']
    assert len(calculations) == 1
    complete_entries(store, [second], 1001)
    assert names(store) == []
    assert len(calculations) == 1
    assert [row['title'] for row in store.enrichment_queue.next_enrich_batch(
        40, providers=['deezer'], now=1e12)] == ['First']
    assert len(calculations) == 1


def test_empty_gates_do_not_calculate_account_priority(queue):
    store, first, second, anchor, calculations = queue
    assert store.enrichment_queue.next_enrich_batch(40, providers=[]) == []
    assert store.enrichment_queue.next_enrich_batch(0) == []
    store.enrichment_state.record_enrichment_attempt('first|a', 'deezer', {}, retry=True, now=1000)
    store.enrichment_state.record_enrichment_attempt('second|b', 'deezer', {}, retry=True, now=1000)
    assert names(store) == []
    assert calculations == []
    complete_entries(store, [first, second], 1001)
    assert names(store) == []
    assert calculations == []


def test_old_snapshot_keeps_its_order_and_cache_hit_keeps_publication_guards(queue):
    store, first, second, anchor, calculations = queue
    assert names(store) == ['First', 'Second']
    with store.read_snapshot() as old:
        assert [r['title'] for r in old.store.enrichment_queue.next_enrich_batch(40)] == ['First', 'Second']
        assert len(calculations) == 1
        _play(store, 'second|b', 3)
        assert names(store) == ['Second', 'First']
        assert [r['title'] for r in old.store.enrichment_queue.next_enrich_batch(40)] == ['First', 'Second']
        assert not old.publish(lambda: pytest.fail('Published a stale priority calculation'))


def test_external_writer_and_restart_cannot_reuse_obsolete_order(queue):
    store, first, second, anchor, calculations = queue
    assert names(store) == ['First', 'Second']
    with closing(sqlite3.connect(store._db_path)) as external, external:
        external.execute('UPDATE recordings SET created_at=300 WHERE id=(SELECT recording_id FROM catalog_entries WHERE id=?)',
                         (second,))
    assert names(store) == ['Second', 'First']
    assert len(calculations) == 2
    reopened = Store(store._db_path)
    try:
        reopened.init_schema()
        assert names(reopened) == ['Second', 'First']
    finally:
        reopened.conn.close()


def test_cancelled_calculation_never_supplies_a_partial_order(queue, monkeypatch):
    from yt_playlist.repos import enrichment_queue

    store, first, second, anchor, calculations = queue
    original = enrichment_queue.check_cancellable_work
    stop = False

    def cancel_after_sql():
        nonlocal stop
        stop = True
        original()

    with monkeypatch.context() as patch:
        patch.setattr(enrichment_queue, 'check_cancellable_work', cancel_after_sql)
        with pytest.raises(WorkClosing), cancellable_work(lambda: stop):
            names(store)
    assert names(store) == ['First', 'Second']
    assert len(calculations) == 2
    assert names(store) == ['First', 'Second']
    assert len(calculations) == 2


@pytest.mark.parametrize('installed', [79, 94, 99, 107], indirect=True)
def test_priority_and_continuing_writes_use_the_selected_installed_relations(installed):
    store, _ = installed
    store.settings.set_setting('enrich_caught_up_at', '100')
    store.enrichment_state.reconcile_provider_configuration({
        'deezer': {'fingerprint': 'installed', 'status': 'available'}})
    first = store.tracks.upsert_track('first', 'First', 'A', None, 200, created_at=1)
    store.tracks.upsert_track('second', 'Second', 'B', None, 200, created_at=1)
    assert names(store) == ['First', 'Second']
    assert names(store) == ['First', 'Second']
    _play(store, 'second|b', 3)
    assert names(store) == ['Second', 'First']
    complete_entries(store, [first], 1001)
    assert names(store) == ['Second']
    store.discovery.upsert_discovered_track('arrival|d', 'arrival', 'Arrival', 'D', None,
                                           None, None, None, 'radio:s', 300)
    assert names(store) == ['Arrival', 'Second']
