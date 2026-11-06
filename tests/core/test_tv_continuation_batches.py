"""Real continuation batches retain checkpoints, references and publication on retry."""
from dataclasses import replace
import json
import sqlite3

import pytest

from tests.core.test_predecessor_writes import installed
from tests.core.test_tv_state_preparation import finish, head
from tests.test_tv_stream import video
from yt_playlist.core.preparation import tv_publication as publication, tv_state as preparation


pytestmark = pytest.mark.parametrize('installed', [79, 93, 94], indirect=True)


def staged_rows(conn):
    names = (*preparation.tables('tv_continuations'),
        *(preparation.relation(name) for name in preparation.RELATIONS['tv_continuations']))
    return {name: sorted(map(tuple, conn.execute('SELECT * FROM '+name))) for name in names}


@pytest.mark.parametrize('ending', ['trigger', 'interrupt'])
def test_partial_real_continuation_batch_rolls_back_rows_and_checkpoint_then_publishes(installed, ending):
    store, _ = installed
    value = {f'{number+1}:stream:request:{number}': {'playlist_id': number+101, 'stream': 'next',
        'items': [video(index) for index in range(10)]} for number in range(3)}
    raw = json.dumps(value, indent=2)
    with store.conn:
        store.conn.execute('INSERT INTO rec_proposals VALUES (?,?,?)', ('tv_continuations', raw, 30))
    job = preparation.JOBS['continuations']
    job.install(store.conn)
    checkpoint = job.step(store.conn, batch_size=1)
    assert checkpoint.status != 'prepared'
    before = staged_rows(store.conn)
    if ending == 'trigger':
        with store.conn:
            store.conn.execute('CREATE TRIGGER controlled_tv_batch BEFORE INSERT ON '+
                preparation.relation('tv_continuation_items')+
                " WHEN NEW.receipt_id=1 AND NEW.position=5 BEGIN SELECT RAISE(ABORT,'controlled TV batch'); END")
        try:
            with pytest.raises(sqlite3.IntegrityError, match='controlled TV batch'):
                job.step(store.conn, batch_size=128)
        finally:
            with store.conn:
                store.conn.execute('DROP TRIGGER controlled_tv_batch')
        failed = job.progress(store.conn)
        assert failed.status == 'failed' and 'controlled TV batch' in failed.error
        assert (failed.cursor, failed.processed, failed.input_revision, failed.prepared_revision) == (
            checkpoint.cursor, checkpoint.processed, checkpoint.input_revision, checkpoint.prepared_revision)
    else:
        def interrupt(conn, row, revision, selected):
            preparation.copy_batch(conn, row, revision, selected[:10])
            raise KeyboardInterrupt('controlled TV batch interruption')
        with pytest.raises(KeyboardInterrupt, match='controlled TV batch interruption'):
            replace(job, copy_batch=interrupt).step(store.conn, batch_size=128)
        assert job.progress(store.conn) == checkpoint
    assert staged_rows(store.conn) == before and not store.conn.in_transaction
    assert head(store.conn, 'tv_continuations') is None
    job.step(store.conn, batch_size=128, retry=True)
    progress = finish(job, store.conn)
    owner = publication.OWNERS['continuations']
    owner.publish(store.conn, revision=progress.prepared_revision)
    assert owner.selected(store.conn) == progress.prepared_revision
    assert store.rec_proposals._proposal_json(owner.source) == raw
    assert store.rec_proposals.get_proposals(owner.source) == value
    assert store.conn.execute('SELECT count(*) FROM '+preparation.relation('tv_continuation_items')+
        ' WHERE preparation_revision=?', (progress.prepared_revision,)).fetchone()[0] == 30
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_continuation_reference_reuse_does_not_survive_a_catalog_change(installed):
    store, _ = installed
    value = {f'{number+1}:stream:request:{number}': {'playlist_id': number+101, 'stream': 'next',
        'items': [video(index) for index in range(3)]} for number in range(2)}
    store.rec_proposals.put_proposals('tv_continuations', value, 30)
    job = preparation.JOBS['continuations']
    job.install(store.conn)
    first = finish(job, store.conn).prepared_revision
    owner = publication.OWNERS['continuations']
    owner.publish(store.conn, revision=first)
    table = preparation.relation('tv_continuation_items')
    assert [tuple(row) for row in store.conn.execute('SELECT recording_id,video_resolution FROM '+table+
        ' WHERE preparation_revision=? ORDER BY receipt_id,position', (first,))] == [(None, 'external_video')] * 6
    store.tracks.upsert_track(video(0)['video_id'], 'Arrived', 'Artist', None, 100)
    recording = store.conn.execute('SELECT recording_id FROM youtube_items WHERE video_id=?',
        (video(0)['video_id'],)).fetchone()[0]
    assert job.progress(store.conn).prepared_revision is None
    second = finish(job, store.conn).prepared_revision
    assert second != first
    owner.publish(store.conn, revision=second)
    assert [tuple(row) for row in store.conn.execute('SELECT recording_id,video_resolution FROM '+table+
        ' WHERE preparation_revision=? ORDER BY receipt_id,position', (second,))] == [
            (recording, None), (None, 'external_video'), (None, 'external_video')] * 2
    assert [tuple(row) for row in store.conn.execute('SELECT recording_id,video_resolution FROM '+table+
        ' WHERE preparation_revision=? ORDER BY receipt_id,position', (first,))] == [(None, 'external_video')] * 6
    assert store.rec_proposals.get_proposals(owner.source) == value
    assert not store.conn.in_transaction and not store.conn.execute('PRAGMA foreign_key_check').fetchone()
