import hashlib
import json

import pytest

from yt_playlist.core import prepared_inputs
from yt_playlist.rec.progressive import Preparation


def empty_capture(key, revision):
    payload = {'library': None, 'discovered': None}
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    return key, revision, raw, hashlib.sha256(raw.encode()).hexdigest(), True


def test_retries_do_not_count_the_same_accepted_digest_twice(store):
    repository = store.prepared_content.repository
    row = empty_capture('missing', 1)
    fields = prepared_inputs.flatten(json.loads(row[2]))
    values = [('missing', fields, row[3], True)]
    before = repository.counters()
    assert repository.publish_capture(values, 100) == 1
    assert not store.conn.in_transaction
    assert repository.publish_capture(values, 101) == 0
    assert repository.counters() == (before[0] + 1, before[1])
    assert store.conn.execute('SELECT input_revision,built_at FROM unresolved_preparation_inputs '
                              "WHERE raw_key='missing'").fetchone()[:] == (before[0] + 1, 100)


def test_same_empty_digest_moves_to_an_admitted_recording(store):
    row = empty_capture('missing', 1)
    fields = prepared_inputs.flatten(json.loads(row[2]))
    repository = store.prepared_content.repository
    values = [('missing', fields, row[3], True)]
    repository.publish_capture(values, 100)
    with store.conn:
        store.conn.execute("INSERT INTO recordings(identity_key) VALUES ('missing')")
    assert repository.publish_capture(values, 101) == 1
    assert not store.conn.execute("SELECT 1 FROM unresolved_preparation_inputs WHERE raw_key='missing'").fetchone()
    assert store.conn.execute('SELECT digest FROM prepared_recording_inputs p '
        "JOIN recordings r ON r.id=p.recording_id WHERE r.identity_key='missing'").fetchone()[0] == row[3]


def test_acknowledgement_preserves_a_later_arrival(store):
    repository = store.prepared_content.repository
    with store.conn:
        store.conn.executemany('INSERT INTO rec_content_pending(identity_key,revision) VALUES (?,?)',
                               [('first', 2), ('second', 3)])
    repository.acknowledge([('first', 1), ('second', 3)])
    assert repository.pending() == [('first', 2)]
    assert not store.conn.in_transaction


def test_late_acknowledgement_failure_keeps_inputs_and_notification_for_retry(store, monkeypatch):
    repository = store.prepared_content.repository
    with store.conn:
        store.conn.execute("INSERT INTO rec_content_pending(identity_key,revision) VALUES ('missing',1)")
    before = repository.counters()
    def fail(pending):
        raise RuntimeError('late failure')
    with monkeypatch.context() as patch:
        patch.setattr(repository, 'acknowledge', fail)
        with pytest.raises(RuntimeError, match='late failure'):
            store.prepared_content.publish_capture([empty_capture('missing', 1)], 100)
    assert repository.pending() == [('missing', 1)]
    assert repository.counters() == (before[0] + 1, before[1])
    assert store.rec_refresh.revision() is not None
    rows = store.prepared_content.capture(repository.pending())
    assert rows[0][-1] is False
    store.prepared_content.publish_capture(rows, 101)
    assert not repository.pending()
    assert repository.counters() == (before[0] + 1, before[1])


def test_ordered_snapshot_publication_has_no_outer_transaction_and_keeps_prior_commits(store):
    def publish():
        assert not store.conn.in_transaction
        store.settings.publish_values({'ordered-first': 'accepted'})
        assert not store.conn.in_transaction
        raise RuntimeError('later owner failed')
    with store.read_snapshot() as snapshot:
        snapshot.store.settings.get_setting('ordered-source')
        with pytest.raises(RuntimeError, match='later owner failed'):
            snapshot.publish_ordered(publish)
    assert store.settings.get_setting('ordered-first') == 'accepted'
    assert not store.conn.in_transaction


def test_ordered_snapshot_rejects_changed_captured_inputs_before_any_write(store):
    called = []
    with store.read_snapshot() as snapshot:
        snapshot.store.settings.get_setting('ordered-source')
        store.settings.set_setting('ordered-source', 'changed')
        assert not snapshot.publish_ordered(lambda: called.append(True))
    assert not called


def test_ordered_snapshot_cannot_borrow_another_transaction(store):
    called = []
    with store.read_snapshot() as snapshot, store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        with pytest.raises(RuntimeError, match='cannot join'):
            snapshot.publish_ordered(lambda: called.append(True))
    assert not called


def test_recording_capture_enters_each_live_owner_without_an_outer_transaction(store, monkeypatch):
    store.tracks.upsert_track('ordered-video', 'Ordered Song', 'Artist', '', 200)
    called = set()
    owners = (
        (store.musical_profiles, 'publish_recordings'),
        (store.vibe_predictions, 'publish_prepared'),
        (store.rec_model, 'publish_training_generation'),
        (store.prepared_content.repository, 'publish_capture'),
        (store.prepared_content.repository, 'acknowledge'),
        (store.rec_refresh.repository, 'enqueue'),
        (store.rec_refresh.repository, 'publish_notification'),
        (store.settings, 'publish_values'),
    )
    for owner, method in owners:
        original = getattr(owner, method)
        def checked(*args, _original=original, _method=method, **kwargs):
            assert not store.conn.in_transaction, f'{_method} borrowed another repository transaction'
            called.add(_method)
            result = _original(*args, **kwargs)
            assert not store.conn.in_transaction
            return result
        monkeypatch.setattr(owner, method, checked)
    assert Preparation(store).capture(1000)
    assert {'publish_recordings', 'publish_prepared', 'publish_capture', 'acknowledge'} <= called
    assert not store.prepared_content.has_pending()
