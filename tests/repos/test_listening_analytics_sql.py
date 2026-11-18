"""SQL criteria stay bounded and display publication never performs file I/O."""
import hashlib
import json
import pytest

from yt_playlist.core import recommendation_outputs as files


def bundle(label='one'):
    raw = json.dumps({'schema': 2, 'columns': ['day', 'track'],
        'rows': [['2026-10-29', label]], 'catalog': {label: {'title': label}}})
    key = hashlib.sha256(raw.encode()).hexdigest()[:24]
    manifest = json.dumps({'schema': 2, 'built_at': 123.5, 'timezone': 'UTC',
        'generation': hashlib.sha256(key.encode()).hexdigest()[:24],
        'chunks': [{'key': key, 'from': '2026-10-29', 'until': '2026-10-29', 'bytes': len(raw.encode())}]})
    return {key: raw}, manifest, json.dumps({'available': True, 'id': label, 'stories': []})


def test_feedback_mapping_groups_bans_and_dislikes_at_the_first_observation(store):
    with store.conn:
        store.conn.executemany('INSERT INTO rec_feedback(surface,item_key,scope,kind,created_at) '
            "VALUES ('listening',?,?,?,?)", [
            ('song', 'ban', 'ban', 2), ('song', 'dislike', 'dislike', 3), ('song', 'like', 'like', 4),
            ('song', 'skip', 'skip', 1), ('future', 'dislike', 'dislike', 101)])
    with store.read_snapshot() as snapshot:
        rows = [row for batch in snapshot.store.listening_analytics.feedback(100) for row in batch]
    assert sorted(rows, key=lambda row: row['kind']) == [
        {'item_key': 'song', 'kind': 'dislike', 'at': 2},
        {'item_key': 'song', 'kind': 'like', 'at': 4}]


def test_impression_counter_only_advances_after_the_supplied_cutoff(store):
    repository = store.listening_analytics.repository
    repository.record_impressions(['story'], 100, last_shown_before=0)
    repository.record_impressions(['story'], 200, last_shown_before=100)
    row = store.conn.execute("SELECT views,last_shown FROM rec_impressions WHERE item_key='story'").fetchone()
    assert tuple(row) == (1, 100)
    repository.record_impressions(['story'], 300, last_shown_before=101)
    row = store.conn.execute("SELECT views,last_shown FROM rec_impressions WHERE item_key='story'").fetchone()
    assert tuple(row) == (2, 300)


def test_native_generation_performs_file_work_before_guarded_sql(store, monkeypatch):
    written = []
    publish = store.listening_analytics.files.publish_reserved

    def write(identity, raw, **kwargs):
        assert not store.conn.in_transaction
        assert not store._lock.held_by_current_thread
        written.append(raw)
        return publish(identity, raw, **kwargs)

    monkeypatch.setattr(store.listening_analytics.files, 'publish_reserved', write)
    with store.read_snapshot() as snapshot:
        # Binding captures the FAO method, so construct this reader after patching.
        generation = store.listening_analytics.prepare_generation(
            *bundle(), 123, input_revision=snapshot._changes)
        assert len(written) == 3

        def forbidden(*args, **kwargs):
            pytest.fail('The guarded SQL publication must not call a file owner')

        monkeypatch.setattr(store.listening_analytics.files, 'publish_reserved', forbidden)
        monkeypatch.setattr(files, 'read', forbidden)
        assert snapshot.publish(lambda: store.listening_analytics.publish_generation(generation, 'UTC'))
    assert store.conn.execute('SELECT count(*) FROM listening_output_files').fetchone()[0] == 3
    assert store.settings.get_setting('listening_timezone') == 'UTC'
