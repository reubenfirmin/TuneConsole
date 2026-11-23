import hashlib
import json

import pytest

from yt_playlist.core import content_work, prepared_inputs
from yt_playlist.core.catalog_query import ENRICHMENT_TRACKS_SQL, PLAYABLE_LIBRARY_SQL
from yt_playlist.rec.progressive import Preparation


def empty_capture(key, revision):
    payload = {'library': None, 'discovered': None}
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    return key, revision, raw, hashlib.sha256(raw.encode()).hexdigest(), True


def test_library_preparation_tracks_the_last_collection_or_listening_reason(store):
    account = store.accounts.upsert_account('Main', None, True)
    playlist = store.playlists.upsert_playlist(account, 'playlist', 'Playlist', 1, '', 1)
    anchor = store.tracks.upsert_track('anchor', 'Anchor', 'Artist', duration_s=200, library=False)
    primary = store.tracks.upsert_track('primary', 'Song', 'Artist', duration_s=200,
                                        available=False, library=False)
    standby = store.tracks.upsert_track('standby', 'Other upload', 'Artist', duration_s=210, library=False)
    subject = store.tracks.catalog_subject(primary)
    key, recording = subject['identity_key'], subject['recording_id']
    store.tracks.associate_item('standby', recording, source='test', evidence={'verified': True}, now=100)
    repository = store.prepared_content.repository

    def library():
        pending = dict(repository.pending())
        row, = store.prepared_content.capture([(key, pending.get(key, 1))])
        return json.loads(row[2])['library']

    def publish(entries):
        store.collection.cache_album_tracklist('album', 'Album', 'Artist', None, [],
                                                entries=[{'entry_id': entry} for entry in entries])

    publish([anchor, primary, primary])
    assert library() is None
    store.playlists.set_playlist_tracks(playlist, [anchor])
    assert store.playlists.get_playlist_track_ids(playlist) == [anchor]
    assert library() is not None
    assert [row[0] for row in store.conn.execute(
        f'SELECT video_id FROM ({PLAYABLE_LIBRARY_SQL}) WHERE recording_id=?', (recording,))] == ['standby']
    store.collection.add_saved_album({'browse': 'album', 'title': 'Album', 'artist': 'Artist'}, account_id=account)
    assert store.history.record_play_event(account, key, 'standby', 1700000000)
    assert [tuple(row) for row in store.conn.execute(
        f'SELECT source,in_library FROM ({ENRICHMENT_TRACKS_SQL}) WHERE recording_id=?', (recording,))] == [('library', 1)]
    store.playlists.set_playlist_tracks(playlist, [])
    assert library() is not None
    publish([anchor, standby])
    assert library() is not None
    store.collection.remove_saved_album('album', account_id=account)
    assert library() is not None
    revision = dict(repository.pending())[key]
    with store.conn:
        store.conn.execute('DELETE FROM play_events WHERE recording_id=?', (recording,))
    assert dict(repository.pending())[key] > revision
    assert library() is None
    assert not store.conn.execute('SELECT 1 FROM track_sources WHERE library_origin IS NOT NULL').fetchone()


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
    assert store.conn.execute('SELECT input_revision,built_at FROM prepared_recording_inputs '
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
    assert not store.conn.execute("SELECT 1 FROM prepared_recording_inputs WHERE raw_key='missing'").fetchone()
    assert store.conn.execute('SELECT digest FROM prepared_recording_inputs p '
        "JOIN recordings r ON r.id=p.recording_id WHERE r.identity_key='missing'").fetchone()[0] == row[3]


def test_acknowledgement_preserves_a_later_arrival(store):
    repository = store.prepared_content.repository
    with store.conn:
        store.conn.execute("INSERT INTO recordings(identity_key) VALUES ('first')")
        content_work.enqueue(store.conn, ('second',))
    first, second = repository.pending()
    with store.conn:
        content_work.enqueue(store.conn, ('first',))
    newest = repository.pending()[0]
    assert newest[0] == first[0] and newest[1] > first[1]
    repository.acknowledge([first, second])
    assert repository.pending() == [newest]
    repository.acknowledge([newest])
    with store.conn:
        content_work.enqueue(store.conn, ('first',))
    repository.acknowledge([newest])
    assert len(repository.pending()) == 1
    assert not store.conn.in_transaction


def test_recording_rename_and_delete_retract_the_previous_identity_in_order(store):
    repository = store.prepared_content.repository
    with store.conn:
        store.conn.execute("INSERT INTO recordings(identity_key) VALUES ('before'),('unrelated')")
    old, unrelated = repository.pending()
    with store.conn:
        store.conn.execute("UPDATE recordings SET identity_key='after' WHERE identity_key='before'")
    retraction, unchanged, current = repository.pending()
    assert retraction[0] == old[0] and retraction[1] > old[1]
    assert unchanged == unrelated
    assert current[0] == 'after'
    assert store.conn.execute(f'SELECT key FROM settings WHERE {content_work.PENDING}').fetchone()[0] == (
        content_work.PREFIX + 'before')
    with store.conn:
        store.conn.execute("DELETE FROM recordings WHERE identity_key='after'")
    repository.acknowledge([old, current])
    assert repository.pending()[:2] == [retraction, unchanged]
    new_retraction = repository.pending()[2]
    assert new_retraction[0] == current[0] and new_retraction[1] > current[1]
    with store.conn:
        store.conn.execute("INSERT INTO recordings(identity_key) VALUES ('after')")
    assert repository.pending()[2][0] == 'after'
    assert not store.conn.execute('SELECT 1 FROM settings WHERE key=?', (content_work.PREFIX + 'after',)).fetchone()


def test_late_acknowledgement_failure_keeps_inputs_and_notification_for_retry(store, monkeypatch):
    repository = store.prepared_content.repository
    with store.conn:
        content_work.enqueue(store.conn, ('missing',))
    pending = repository.pending()
    before = repository.counters()
    def fail(pending):
        raise RuntimeError('late failure')
    with monkeypatch.context() as patch:
        patch.setattr(repository, 'acknowledge', fail)
        with pytest.raises(RuntimeError, match='late failure'):
            store.prepared_content.publish_capture([empty_capture('missing', 1)], 100)
    assert repository.pending() == pending
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
