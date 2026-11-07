"""Interrupted corrections finish their independently committed owners on retry."""
import pytest

from tests.core.test_discovery_reassociation import discover, move, subject
from tests.store_fixture_support import initialize_consumer
from yt_playlist.core.store import Store


@pytest.mark.parametrize(('partial', 'owner', 'method'), [
    (True, 'tracks.repository', 'assign_recording'),
    (True, 'discovery', 'finish_track_reassociation'),
    (True, 'rec_model', 'reassociate_rating_feedback'),
    (True, 'recording_keys', 'invalidate_catalog'),
    (False, 'tracks.repository', 'assign_recording'),
    (False, 'discovery', 'finish_track_reassociation'),
    (False, 'recording_keys', 'finish_reassociation'),
    (False, 'tracks.repository', 'redirect_recording'),
    (False, 'rec_model', 'reassociate_rating_feedback'),
    (False, 'recording_keys', 'invalidate_catalog'),
])
@pytest.mark.parametrize('after_commit', [False, True])
def test_retry_after_reopen_completes_discovery_without_replaying_counts(
        tmp_path, _pristine_current_schema, monkeypatch, partial, owner, method, after_commit):
    path = tmp_path / 'state.db'
    store = Store(path)
    try:
        initialize_consumer(store, _pristine_current_schema)
        a = store.tracks.upsert_track('a', 'a', 'Artist', None, 200)
        b = store.tracks.upsert_track('b', 'b', 'Artist', None, 200)
        target = store.tracks.upsert_track('target', 'target', 'Artist', None, 200)
        if partial:
            move(store, 'b', a)
        discover(store, 'a', 'radio:a', 1)
        if partial:
            discover(store, 'b', 'genre:b', 2)
        previous = subject(store, a)['recording_id']
        destination = subject(store, target)['recording_id']
        old_key = subject(store, a)['identity_key']
        new_key = subject(store, target)['identity_key']
        with store.conn:
            store.conn.executemany('INSERT INTO rec_impressions VALUES (?,?,?,?)',
                [('home', old_key, 3, 1), ('home', new_key, 5, 2)])
            store.conn.execute('INSERT INTO rec_feedback(surface,item_key,kind,reason,created_at,until,'
                'target_type,recording_id) VALUES (?,?,?,?,?,?,?,?)',
                ('home', old_key, 'dismiss', 'reviewed', 1, 50, 'recording', previous))
        store.discovery.mark_offered('track', [subject(store, a)['identity_key']], 3)
        receipts = store.conn.execute('SELECT COUNT(*) FROM item_recording_associations').fetchone()[0]
        collaborator = store
        for name in owner.split('.'):
            collaborator = getattr(collaborator, name)
        original = getattr(collaborator, method)

        def interrupt(*args, **kwargs):
            assert not store.conn.in_transaction
            if after_commit:
                original(*args, **kwargs)
            assert not store.conn.in_transaction
            raise RuntimeError('interrupted correction')

        with monkeypatch.context() as patch:
            patch.setattr(collaborator, method, interrupt)
            with pytest.raises(RuntimeError, match='interrupted correction'):
                move(store, 'a', target, 4)
        store.conn.close()
        store = Store(path)
        store.init_schema()
        move(store, 'a', target, 5)
        move(store, 'a', target, 6)
        assert subject(store, a)['recording_id'] == destination
        memberships = {row['recording_id']: row['entry_id'] for row in store.conn.execute(
            'SELECT * FROM discovery_memberships')}
        assert memberships == ({previous: b, destination: a} if partial else {destination: a})
        sources = sorted((row['recording_id'], row['input_video_id']) for row in store.conn.execute(
            'SELECT * FROM discovery_sources'))
        assert sources == (sorted([(previous, 'b'), (destination, 'a')]) if partial else [(destination, 'a')])
        assert store.conn.execute('SELECT offered_count FROM discovery_memberships WHERE recording_id=?',
                                  (destination,)).fetchone()[0] == (0 if partial else 1)
        assert store.conn.execute('SELECT COUNT(*) FROM item_recording_associations').fetchone()[0] == receipts + 1
        views = dict(store.conn.execute("SELECT item_key,views FROM rec_impressions WHERE surface='home'"))
        assert views == ({old_key: 3, new_key: 5} if partial else {new_key: 8})
        feedback = [tuple(row) for row in store.conn.execute(
            "SELECT item_key,kind,reason,created_at,until FROM rec_feedback WHERE surface='home'")]
        assert feedback == [(old_key if partial else new_key, 'dismiss', 'reviewed', 1, 50)]
        assert not store.conn.execute('SELECT * FROM recording_association_work').fetchall()
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        store.conn.close()
