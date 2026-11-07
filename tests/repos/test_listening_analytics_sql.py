"""SQL criteria stay bounded and display publication never performs file I/O."""
import pytest

from tests.core.test_listening_outputs_migration import bundle
from yt_playlist.core import recommendation_outputs as files


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
    publish = files.publish

    def write(root, raw):
        assert not store.conn.in_transaction
        assert not store._lock.held_by_current_thread
        written.append(raw)
        return publish(root, raw)

    monkeypatch.setattr(files, 'publish', write)
    with store.read_snapshot() as snapshot:
        # Binding captures the FAO method, so construct this reader after patching.
        generation = snapshot.store.listening_analytics.prepare_generation(
            *bundle(), 123, input_revision=snapshot._changes)
        assert len(written) == 3

        def forbidden(*args, **kwargs):
            pytest.fail('The guarded SQL publication must not call a file owner')

        monkeypatch.setattr(files, 'publish', forbidden)
        monkeypatch.setattr(files, 'read', forbidden)
        assert snapshot.publish(lambda: store.listening_analytics.publish_generation(generation, 'UTC'))
    assert store.conn.execute('SELECT count(*) FROM listening_output_files').fetchone()[0] == 3
    assert store.settings.get_setting('listening_timezone') == 'UTC'
