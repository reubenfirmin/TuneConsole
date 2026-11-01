"""Retirement must preserve current inputs and reject stale delivery selection."""
import sqlite3

import numpy as np
import pytest

from yt_playlist.core import artist_credits, artists, credit_retention, mode_retention
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.core.preparation import runtime_history

DAY = 86400


def mode(identity, label='House'):
    return dict(mode_id=identity, label=label, families=[['House', 2]],
                centroid=np.array([.25, .75], dtype=np.float32), size=2, rep_keys=['one', 'two'], space='basis')


def deliver(store, entry, key, name, now):
    with atomic_write(store):
        artists.observe(store.conn, entry, name, [{'name': name, 'id': name}],
                        delivery_key=key, now=now)
        row = store.conn.execute('SELECT observation_id FROM artist_credit_replays '
                                 'WHERE entry_id=? AND source=? AND delivery_key=?', (entry, 'youtube', key)).fetchone()
        return row[0]


def finish(store, domain, now):
    for _ in range(100):
        if not runtime_history.step(store, domain, now, activate=True):
            return
    pytest.fail('Bounded history scan did not finish')


def test_expired_delivery_retains_receipt_without_reapplying_old_credit(store):
    entry = store.tracks.upsert_track('v', 'Song', 'Initial', None, 200)
    first = deliver(store, entry, 'first', 'Before', 1)
    second = deliver(store, entry, 'second', 'After', 2)
    selected = list(map(tuple, store.conn.execute('SELECT * FROM entry_artists WHERE entry_id=?', (entry,))))
    finish(store, 'credits', 31 * DAY)
    assert store.conn.execute('SELECT 1 FROM artist_credit_observations WHERE id=?', (first,)).fetchone() is None
    assert store.conn.execute('SELECT observation_id FROM entry_artist_selections WHERE entry_id=?', (entry,)).fetchone()[0] == second
    assert deliver(store, entry, 'first', 'Before', 60 * DAY) == first
    assert list(map(tuple, store.conn.execute('SELECT * FROM entry_artists WHERE entry_id=?', (entry,)))) == selected
    assert store.conn.execute('SELECT observation_id FROM artist_credit_capture_heads '
                              "WHERE entry_id=? AND source='youtube'", (entry,)).fetchone()[0] == second
    with pytest.raises(sqlite3.IntegrityError, match='cannot change'):
        deliver(store, entry, 'first', 'Different', 61 * DAY)
    with pytest.raises(ValueError, match='capture expired'):
        with atomic_write(store):
            artist_credits.select(store.conn, entry, first)
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_current_heads_recent_and_unknown_time_evidence_survive(store):
    entry = store.tracks.upsert_track('v', 'Song', 'Initial', None, 200)
    first = deliver(store, entry, 'first', 'Before', 1)
    deliver(store, entry, 'second', 'After', 2)
    # A source head is protected even when another source supplies the selection.
    with atomic_write(store):
        artists.observe(store.conn, entry, 'Other', [{'name': 'Other', 'id': 'other'}], source='other', now=3)
    finish(store, 'credits', 29 * DAY)
    assert store.conn.execute('SELECT 1 FROM artist_credit_observations WHERE id=?', (first,)).fetchone()
    finish(store, 'credits', 31 * DAY)
    assert store.conn.execute('SELECT count(*) FROM artist_credit_capture_heads WHERE entry_id=?', (entry,)).fetchone()[0] == 2
    assert store.conn.execute('SELECT count(*) FROM artist_credit_observations WHERE delivery_key=?', ('second',)).fetchone()[0] == 1


def test_preparation_rollback_restart_and_unknown_owner_guard(store, monkeypatch):
    entry = store.tracks.upsert_track('v', 'Song', 'Initial', None, 200)
    first = deliver(store, entry, 'first', 'Before', 1)
    deliver(store, entry, 'second', 'After', 2)
    monkeypatch.setattr(credit_retention, 'BATCH', 1)
    with pytest.raises(RuntimeError, match='interrupted'):
        with atomic_write(store):
            credit_retention.step(store.conn, 31 * DAY, activate=True)
            raise RuntimeError('interrupted')
    assert runtime_history.status(store.conn, 'credits')['enabled'] == 0
    assert store.conn.execute('SELECT 1 FROM artist_credit_observations WHERE id=?', (first,)).fetchone()
    with store.conn:
        store.conn.execute('CREATE TABLE new_owner(id INTEGER PRIMARY KEY,observation_id INTEGER REFERENCES artist_credit_observations(id))')
    with pytest.raises(ValueError, match='new retention audit'):
        runtime_history.step(store, 'credits', 31 * DAY, activate=True)
    with store.conn:
        store.conn.execute('DROP TABLE new_owner')
    finish(store, 'credits', 31 * DAY)
    assert not store.conn.execute('SELECT 1 FROM artist_credit_observations WHERE id=?', (first,)).fetchone()
    assert runtime_history.status(store.conn, 'credits')['completed'] == 1


def test_installed_legacy_captures_retire_but_current_unknown_selection_survives(store):
    entry = store.tracks.upsert_track('v', 'Song', 'Initial', None, 200)
    credit = store.conn.execute('SELECT credit_id FROM artist_credit_observations LIMIT 1').fetchone()[0]
    with atomic_write(store):
        old = store.conn.execute('INSERT INTO artist_credit_observations(entry_id,credit_id,source,processing_count,capture_kind) '
                                 "VALUES (?,?,?,1,'legacy_capture')", (entry, credit, 'legacy_literal')).lastrowid
    assert not runtime_history.step(store, 'credits', 1)
    assert store.conn.execute('SELECT 1 FROM artist_credit_observations WHERE id=?', (old,)).fetchone()
    finish(store, 'credits', 1)
    assert not store.conn.execute('SELECT 1 FROM artist_credit_observations WHERE id=?', (old,)).fetchone()
    assert store.conn.execute('SELECT count(*) FROM entry_artist_selections').fetchone()[0] == 1


def test_replay_protects_immutable_credit_members_after_observation_expiry(store):
    entry = store.tracks.upsert_track('v', 'Song', 'Initial', None, 200)
    first = deliver(store, entry, 'first', 'Before', 1)
    deliver(store, entry, 'second', 'After', 2)
    finish(store, 'credits', 31 * DAY)
    credit = store.conn.execute('SELECT credit_id FROM artist_credit_replays WHERE observation_id=?', (first,)).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        with atomic_write(store):
            store.conn.execute('DELETE FROM artist_credit_members WHERE credit_id=?', (credit,))


def test_expired_metadata_delivery_replay_does_not_need_the_old_delivery_graph(store):
    from yt_playlist.core import metadata_deliveries
    entry = store.tracks.upsert_track('v', 'Song', 'Initial', None, 200)
    recording = store.conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (entry,)).fetchone()[0]
    with atomic_write(store):
        delivery = metadata_deliveries.record_many(store.conn, [metadata_deliveries.Delivery(
            'youtube', 'revision', 'recording', 'v', 1, input_entry_id=entry,
            input_recording_id=recording, input_video_id='v')])[0]
        artists.observe(store.conn, entry, 'Before', [{'name': 'Before', 'id': 'Before'}], now=1,
                        metadata_delivery_id=delivery)
    original = store.conn.execute('SELECT observation_id FROM artist_credit_replays WHERE delivery_key=?',
                                  (f'metadata-delivery:{delivery}',)).fetchone()[0]
    deliver(store, entry, 'newer', 'After', 2)
    finish(store, 'credits', 31 * DAY)
    with atomic_write(store):
        store.conn.execute('DELETE FROM metadata_deliveries WHERE id=?', (delivery,))
        old = store.conn.execute('SELECT credit_id FROM artist_credit_replays WHERE observation_id=?', (original,)).fetchone()[0]
        assert artist_credits.capture(store.conn, entry, old, 'youtube', 32 * DAY,
                                      metadata_delivery_id=delivery) == (original, False)
    # A caller-controlled key with the same spelling is not a verified binding.
    fake = deliver(store, entry, 'metadata-delivery:999999', 'Fake', 1)
    deliver(store, entry, 'latest', 'After', 2)
    finish(store, 'credits', 31 * DAY)
    credit = store.conn.execute('SELECT credit_id FROM artist_credit_replays WHERE observation_id=?', (fake,)).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match='no verified metadata'):
        with atomic_write(store):
            artist_credits.capture(store.conn, entry, credit, 'youtube', 32 * DAY, metadata_delivery_id=999999)


def test_expired_replay_keeps_artist_redirect_semantics(store):
    entry = store.tracks.upsert_track('v', 'Song', 'First', None, 200)
    with atomic_write(store):
        artists.observe(store.conn, entry, 'First', [{'name': 'First'}], delivery_key='unresolved', now=1)
    earlier = store.conn.execute('SELECT observation_id FROM artist_credit_replays WHERE delivery_key=?', ('unresolved',)).fetchone()[0]
    store.tracks.upsert_track('known', 'Other song', 'First', None, 200, credits=[{'name': 'First', 'id': 'first'}])
    with atomic_write(store):
        artists.observe(store.conn, entry, 'First', [{'name': 'First', 'id': 'first'}], delivery_key='resolved', now=2)
    finish(store, 'credits', 31 * DAY)
    selected = store.conn.execute('SELECT observation_id FROM entry_artist_selections WHERE entry_id=?', (entry,)).fetchone()[0]
    with atomic_write(store):
        artists.observe(store.conn, entry, 'First', [{'name': 'First'}], delivery_key='unresolved', now=32 * DAY)
    assert not store.conn.execute('SELECT 1 FROM artist_credit_observations WHERE id=?', (earlier,)).fetchone()
    assert store.conn.execute('SELECT observation_id FROM entry_artist_selections WHERE entry_id=?', (entry,)).fetchone()[0] == selected


def test_retired_modes_keep_identity_fks_and_active_vectors_exactly(store):
    store.modes.replace_modes([mode(1), mode(900)], [], 1)
    store.modes.replace_modes([], [900], 2)
    before = store.modes.list_modes()[0]
    store.modes.log_impressions(1, [('held', 900)], 1)
    store.modes.log_pick(1, 900, 2)
    finish(store, 'modes', 40 * DAY)
    after = store.modes.list_modes()[0]
    assert before.keys() == after.keys()
    assert before['centroid'].tobytes() == after['centroid'].tobytes()
    assert {k: v for k, v in before.items() if k != 'centroid'} == {k: v for k, v in after.items() if k != 'centroid'}
    history = {m['mode_id']: m for m in store.modes.list_modes(active_only=False)}[900]
    assert history['label'] == 'House' and history['centroid'] is None and history['rep_keys'] is None
    assert store.modes.impression_counts() == {900: 1} and store.modes.pick_counts() == {900: 1}
    assert store.modes.next_mode_id() == 901
    queries = []
    store.conn.set_trace_callback(queries.append)
    assert len(store.modes.list_summaries()) == 2
    store.conn.set_trace_callback(None)
    assert not any('centroid' in sql.lower() for sql in queries)
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_mode_reactivation_requires_new_payload_and_allocation_never_reuses_deleted_maximum(store):
    store.modes.replace_modes([mode(1), mode(900)], [], 1)
    store.modes.replace_modes([], [900], 2)
    finish(store, 'modes', 3)
    with pytest.raises(sqlite3.IntegrityError, match='requires a new payload'):
        with atomic_write(store):
            store.conn.execute('UPDATE taste_modes SET active=1 WHERE mode_id=900')
    for invalid in (None, [], [float('nan')], [[.25, .75]]):
        with pytest.raises(ValueError, match='nonempty finite vector'):
            store.modes.replace_modes([{**mode(900), 'centroid': invalid}], [], 4)
        assert [row['mode_id'] for row in store.modes.list_modes()] == [1]
    for invalid in (b'', b'\x00', np.array([np.nan], dtype=np.float32).tobytes()):
        with pytest.raises(ValueError, match='nonempty finite vector'):
            with atomic_write(store):
                store.modes.publish_generation([(900, 'Invalid', '[]', invalid, 2, '[]', 1, 4, 'basis')], [])
    store.modes.replace_modes([mode(900, 'Updated')], [], 4)
    assert not store.conn.execute('SELECT 1 FROM taste_mode_retired_payloads WHERE mode_id=900').fetchone()
    assert {m['mode_id'] for m in store.modes.list_modes()} == {1, 900}
    with atomic_write(store):
        store.conn.execute('DELETE FROM taste_modes WHERE mode_id=900')
    assert store.modes.next_mode_id() == 901
    with pytest.raises(sqlite3.IntegrityError, match='go backwards'):
        with atomic_write(store):
            store.conn.execute('UPDATE taste_mode_retention SET high_water=1')


def test_reader_keeps_retired_payload_until_release_and_stale_publication_is_rejected(store):
    store.modes.replace_modes([mode(1)], [], 1)
    store.modes.replace_modes([], [1], 2)
    with store.read_snapshot() as snapshot:
        old = snapshot.store.modes.list_modes(active_only=False)[0]['centroid'].tobytes()
        finish(store, 'modes', 3)
        assert snapshot.store.modes.list_modes(active_only=False)[0]['centroid'].tobytes() == old
        assert not snapshot.publish(lambda: pytest.fail('Stale model published after payload retirement'))
    assert store.modes.list_modes(active_only=False)[0]['centroid'] is None


def test_idle_maintenance_does_not_activate_bulk_work_and_retains_reactivated_modes(store, monkeypatch):
    store.modes.replace_modes([mode(1), mode(2)], [], 1)
    store.modes.replace_modes([], [1, 2], 2)
    assert not any(runtime_history.maintain(store, 3).values())
    assert store.conn.execute('SELECT count(*) FROM taste_mode_retired_payloads').fetchone()[0] == 0
    monkeypatch.setattr(mode_retention, 'BATCH', 1)
    assert runtime_history.step(store, 'modes', 3, activate=True)
    store.modes.replace_modes([mode(2)], [], 4)
    finish(store, 'modes', 5)
    assert [m['mode_id'] for m in store.modes.list_modes()] == [2]
    store.modes.replace_modes([], [2], 6)
    # Completed idle passes run daily rather than continuously rescanning heads.
    assert not any(runtime_history.maintain(store, 7).values())
    for _ in range(5):
        runtime_history.maintain(store, DAY + 7)
    assert store.modes.list_modes(active_only=False)[1]['centroid'] is None
