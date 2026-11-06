"""Legacy conflict preparation retains ordered evidence and independent choices."""
from contextlib import contextmanager
import json
from tests.enrichment_support import choose_field
from pathlib import Path
import time

import pytest

from tests.core.test_metadata_owner_cutover import predecessor
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import metadata_conflicts as preparation
from yt_playlist.core.store import Store


def seed(store, candidates=None, *, resolved=1, selected='130', field='bpm', snapshot=True,
         video_id='video', title='Song'):
    entry = store.tracks.upsert_track(video_id, title, 'Artist', None, 200)
    recording = store.conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (entry,)).fetchone()[0]
    if candidates is None:
        candidates = [{'provider': 'a', 'value': 120}, {'provider': 'b', 'value': 130},
                      {'provider': 'b', 'value': 130}, {'provider': 'blank', 'value': ''},
                      {'provider': 'null', 'value': None}, {'provider': '', 'value': 5},
                      ['malformed'], {'provider': 'invalid', 'value': 'not a number'}]
    raw = candidates if isinstance(candidates, str) else json.dumps(candidates, indent=2, ensure_ascii=False)
    with store.conn:
        identity = store.conn.execute('INSERT INTO enrichment_conflict '
            '(track_id,field,candidates,resolved,resolved_value,updated_at,recording_id,input_video_id,field_scope) '
            "VALUES (?,?,?,?,?,10,?,?,'recording')", (entry, field, raw, resolved, selected, recording, video_id)).lastrowid
    original = (dict(store.conn.execute('SELECT * FROM enrichment_conflict WHERE id=?', (identity,)).fetchone())
                if snapshot else None)
    return identity, original


@pytest.fixture
def phase(record_property):
    """Attribute the real operations without replacing their I/O or durability."""
    @contextmanager
    def measured(name):
        started = time.perf_counter()
        try:
            yield
        finally:
            record_property('metadata_conflict_' + name + '_seconds', time.perf_counter() - started)
    return measured


def finish(conn, size=2):
    for _ in range(100):
        result = preparation.JOB.step(conn, batch_size=size)
        if result.status == 'prepared':
            return result
    pytest.fail('Metadata conflict preparation did not finish')


def head(conn, identity):
    return conn.execute(f'SELECT g.* FROM {preparation.HEAD} g JOIN {preparation.CURRENT} h '
                        'USING(conflict_id,generation) WHERE conflict_id=?', (identity,)).fetchone()


def test_ordered_candidates_null_blank_and_resolution_keep_separate_identity(predecessor, phase):
    store = predecessor
    conn = store.conn
    with phase('source_insertion'):
        identity, original = seed(store)
    history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    with phase('job_installation'):
        preparation.JOB.install(conn)
    with phase('first_checkpoint'):
        first = preparation.JOB.step(conn, batch_size=2)
    assert first.processed == 2 and head(conn, identity) is None
    with phase('regular_migration'):
        regular.migrate(conn)
    with phase('completion'):
        finish(conn)
    captured = head(conn, identity)
    assert captured['candidates_count'] == 8 and captured['body_kind'] == 'list'
    assert captured['field_scope'] == 'recording' and captured['status'] == 'user_resolved'
    assert preparation.read_original(captured) == original
    candidates = conn.execute(f'SELECT position,provider,disposition,value_kind,number_value,text_value '
        f'FROM {preparation.CANDIDATES} WHERE conflict_id=? AND generation=? ORDER BY position',
        (identity, captured['generation'])).fetchall()
    assert list(map(tuple, candidates)) == [
        (0, 'a', 'offered', 'value', 120, None), (1, 'b', 'offered', 'value', 130, None),
        (2, 'b', 'offered', 'value', 130, None), (3, 'blank', 'offered', 'blank', None, ''),
        (4, 'null', 'offered', 'null', None, None), (5, None, 'invalid_candidate', None, None, None),
        (6, None, 'invalid_candidate', None, None, None), (7, 'invalid', 'offered', 'unresolved', None, None)]
    choice = conn.execute(f'SELECT * FROM {preparation.RESOLUTIONS} WHERE conflict_id=?', (identity,)).fetchone()
    assert choice['raw_value'] == '130' and choice['number_value'] == 130 and choice['decision_reason'] is None
    assert not any('candidate' in name or 'observation' in name for name in choice.keys())
    assert dict(conn.execute('SELECT * FROM enrichment_conflict WHERE id=?', (identity,)).fetchone()) == original
    assert list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
    assert not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='metadata_conflicts'").fetchone()
    choose_field(store, original['track_id'], 'bpm', 140)
    assert conn.execute('SELECT bpm FROM catalog_tracks WHERE id=?', (original['track_id'],)).fetchone()[0] == 140


@pytest.mark.parametrize('raw,kind', [('broken [', 'malformed'), ('null', 'not_list'), ('{}', 'not_list'), ('[NaN]', 'malformed')])
def test_unparseable_candidate_body_is_retained_without_invented_evidence(predecessor, raw, kind):
    identity, original = seed(predecessor, raw, resolved=2, selected=None)
    conn = predecessor.conn
    preparation.JOB.install(conn)
    finish(conn)
    captured = head(conn, identity)
    assert captured['body_kind'] == kind and captured['candidates_count'] == 0
    assert captured['status'] == 'provider_converged'
    assert preparation.read_original(captured) == original
    assert not conn.execute(f'SELECT 1 FROM {preparation.CANDIDATES}').fetchone()
    assert not conn.execute(f'SELECT 1 FROM {preparation.RESOLUTIONS}').fetchone()


@pytest.mark.parametrize('selected,kind,reason', [(None, None, 'unrecorded_legacy_resolution'), ('', 'blank', None),
                                                ('not a number', 'unresolved', 'unsupported_legacy_value')])
def test_saved_choice_is_not_inferred_from_candidates(predecessor, selected, kind, reason):
    identity, _ = seed(predecessor, selected=selected)
    conn = predecessor.conn
    preparation.JOB.install(conn)
    finish(conn)
    choice = conn.execute(f'SELECT * FROM {preparation.RESOLUTIONS} WHERE conflict_id=?', (identity,)).fetchone()
    assert (choice['raw_value'], choice['value_kind'], choice['decision_reason']) == (selected, kind, reason)


def test_large_list_commits_only_requested_candidates_and_fences_unrelated_edits(predecessor):
    conn = predecessor.conn
    identity, original = seed(predecessor, [{'provider': 'a', 'value': i + 1} for i in range(600)])
    preparation.JOB.install(conn)
    assert preparation.JOB.step(conn, batch_size=128).processed == 128
    assert conn.execute(f'SELECT count(*) FROM {preparation.CANDIDATES}').fetchone()[0] == 128
    with conn:
        conn.execute('UPDATE enrichment_conflict SET updated_at=11 WHERE id=?', (identity,))
    before = preparation.JOB.progress(conn).processed
    for _ in range(10):
        progress = preparation.JOB.step(conn, batch_size=128)
        assert 0 <= progress.processed-before <= 128
        before = progress.processed
        if progress.status == 'prepared':
            break
    else:
        pytest.fail('Long candidate list did not finish')
    captured = head(conn, identity)
    assert captured['generation'] == 1
    assert conn.execute(f'SELECT count(*) FROM {preparation.CANDIDATES} WHERE generation=1').fetchone()[0] == 600
    previous = conn.execute(f'SELECT * FROM {preparation.HEAD} WHERE generation=0').fetchone()
    assert preparation.read_original(previous) == original


def test_source_replacement_key_rename_delete_and_dependency_change_keep_archives(predecessor):
    conn = predecessor.conn
    identity, original = seed(predecessor, [{'provider': 'a', 'value': 'Unadmitted'}], field='genre')
    preparation.JOB.install(conn)
    finish(conn)
    captured = head(conn, identity)
    with conn:
        conn.execute("INSERT INTO genre_catalog(key,name,source) VALUES ('admitted','Unadmitted','custom')")
    assert preparation.JOB.progress(conn).prepared_revision is None
    finish(conn)
    assert conn.execute(f'SELECT genre_key FROM {preparation.CANDIDATES} WHERE generation=?',
                        (head(conn, identity)['generation'],)).fetchone()[0] == 'admitted'
    with conn:
        conn.execute("UPDATE enrichment_conflict SET id=0,candidates='[]' WHERE id=?", (identity,))
    finish(conn)
    assert head(conn, identity) is None and head(conn, 0)['candidates_count'] == 0
    with conn:
        conn.execute('DELETE FROM enrichment_conflict WHERE id=0')
    finish(conn)
    assert head(conn, 0) is None
    assert preparation.read_original(captured) == original and Path(captured['filename']).is_file()


def test_file_io_does_not_hold_transaction_and_rejects_a_stale_source(predecessor, monkeypatch):
    conn = predecessor.conn
    identity, _ = seed(predecessor)
    preparation.JOB.install(conn)
    publish = preparation._publish_encoded
    second = Store(predecessor._db_path)
    calls = []
    try:
        def edit(root, encoded, digest):
            assert not conn.in_transaction
            if not calls:
                with second.conn:
                    second.conn.execute("UPDATE enrichment_conflict SET candidates='[]' WHERE id=?", (identity,))
                calls.append(True)
            return publish(root, encoded, digest)
        monkeypatch.setattr(preparation, '_publish_encoded', edit)
        result = preparation.JOB.step(conn, batch_size=2)
        assert result.input_revision == 1 and not conn.execute(f'SELECT 1 FROM {preparation.HEAD}').fetchone()
        finish(conn)
        assert head(conn, identity)['candidates_count'] == 0
    finally:
        second.conn.close()


def test_interruption_and_failure_preserve_committed_candidates_and_need_explicit_retry(predecessor, monkeypatch, phase):
    conn = predecessor.conn
    with phase('source_insertion'):
        identity, _ = seed(predecessor, snapshot=False)
    with phase('job_installation'):
        preparation.JOB.install(conn)
    with phase('first_checkpoint'):
        checkpoint = preparation.JOB.step(conn, batch_size=2)
    copy = preparation._candidate
    def interrupted(*args):
        copy(*args)
        raise KeyboardInterrupt('controlled candidate interruption')
    monkeypatch.setattr(preparation, '_candidate', interrupted)
    with phase('interruption'), pytest.raises(KeyboardInterrupt):
        preparation.JOB.step(conn, batch_size=2)
    assert preparation.JOB.progress(conn) == checkpoint
    assert conn.execute(f'SELECT count(*) FROM {preparation.CANDIDATES}').fetchone()[0] == 2
    def failed(*args):
        copy(*args)
        raise OSError('controlled candidate failure')
    monkeypatch.setattr(preparation, '_candidate', failed)
    with phase('failure'), pytest.raises(OSError, match='controlled candidate failure'):
        preparation.JOB.step(conn, batch_size=2)
    assert preparation.JOB.progress(conn).status == 'failed'
    assert conn.execute(f'SELECT count(*) FROM {preparation.CANDIDATES}').fetchone()[0] == 2
    with phase('regular_migration'):
        regular.migrate(conn)
    with pytest.raises(MigrationError, match='retry explicitly'):
        preparation.JOB.step(conn)
    monkeypatch.setattr(preparation, '_candidate', copy)
    with phase('reopened_retry'):
        reopened = Store(predecessor._db_path)
        try:
            preparation.JOB.step(reopened.conn, batch_size=2, retry=True)
            finish(reopened.conn)
            assert head(reopened.conn, identity)['candidates_count'] == 8
        finally:
            reopened.conn.close()


def test_oversized_source_fails_before_file_io_and_retains_source(predecessor, monkeypatch, phase):
    with phase('source_insertion'):
        raw = '[]' + ' ' * preparation.MAX_BYTES
        # The later exact-byte assertion is the required readback; avoid an
        # additional full SELECT of the >8 MiB row whose result was discarded.
        identity, _ = seed(predecessor, raw, snapshot=False)
    conn = predecessor.conn
    with phase('job_installation'):
        preparation.JOB.install(conn)
    monkeypatch.setattr(preparation, '_publish_encoded', lambda *args: pytest.fail('Oversized source reached file I/O'))
    with phase('rejection'), pytest.raises(MigrationError, match='byte budget'):
        preparation.JOB.step(conn)
    with phase('failure_progress'):
        assert preparation.JOB.progress(conn).status == 'failed'
    with phase('source_verification'):
        assert conn.execute('SELECT candidates FROM enrichment_conflict WHERE id=?', (identity,)).fetchone()[0] == raw
    with phase('regular_migration'):
        regular.migrate(conn)


def test_missing_recorded_association_stays_entry_local(predecessor):
    conn = predecessor.conn
    identity, original = seed(predecessor)
    with conn:
        conn.execute("UPDATE enrichment_conflict SET recording_id=NULL,input_video_id=NULL,field_scope='legacy_item' WHERE id=?",
                     (identity,))
    preparation.JOB.install(conn)
    finish(conn)
    captured = head(conn, identity)
    assert captured['entry_id'] == original['track_id']
    assert captured['recording_id'] is None and captured['input_video_id'] is None
    assert captured['field_scope'] == 'legacy_item'
    assert conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (original['track_id'],)).fetchone()[0] is not None


def test_interruption_after_candidate_and_current_head_writes_rolls_back_both(predecessor):
    conn = predecessor.conn
    identity, _ = seed(predecessor)
    preparation.JOB.install(conn)
    checkpoint = preparation.JOB.step(conn, batch_size=2)
    def interrupt():
        assert head(conn, identity) is not None
        raise KeyboardInterrupt('controlled final head interruption')
    with pytest.raises(KeyboardInterrupt):
        preparation.JOB.step(conn, batch_size=512, clock=interrupt)
    assert preparation.JOB.progress(conn) == checkpoint and head(conn, identity) is None
    assert conn.execute(f'SELECT count(*) FROM {preparation.CANDIDATES}').fetchone()[0] == 2
    finish(conn)
    assert head(conn, identity)['candidates_count'] == 8


def test_invalid_resolution_status_is_a_durable_failure(predecessor, phase):
    conn = predecessor.conn
    with phase('source_insertion'):
        identity, original = seed(predecessor, resolved=3)
    with phase('job_installation'):
        preparation.JOB.install(conn)
    with phase('rejection'), pytest.raises(MigrationError, match='Unsupported legacy conflict status'):
        preparation.JOB.step(conn)
    with phase('failure_progress'):
        assert preparation.JOB.progress(conn).status == 'failed'
    assert head(conn, identity) is None
    assert dict(conn.execute('SELECT * FROM enrichment_conflict WHERE id=?', (identity,)).fetchone()) == original
    with phase('regular_migration'):
        regular.migrate(conn)
