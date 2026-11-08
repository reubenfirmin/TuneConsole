"""Real old metadata writers continue while typed staging is interrupted."""
from dataclasses import replace
from tests.enrichment_support import choose_field
import json

import pytest

from yt_playlist.core import catalog, genre_identity
from yt_playlist.core.migrations import regular
from yt_playlist.core.migrations.runner import MigrationError
from yt_playlist.core.preparation import metadata_evidence as preparation

from tests.core.test_metadata_owner_cutover import predecessor, seed
from tests.independent_schema_support import independent_regular_ddl


def finish(conn):
    for _ in range(200):
        result = preparation.JOB.step(conn, batch_size=128)
        if result.status == 'prepared':
            return result
    pytest.fail('Finite metadata fixture did not finish')


def test_durable_metadata_copy_preserves_choices_and_reconciles_composite_keys(predecessor):
    store = predecessor
    entry, _, _ = seed(store)
    job = preparation.JOB
    before = list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    job.install(store.conn)
    job.step(store.conn, batch_size=2)
    # These edits occur behind the already-copied cursor. SQLite's writer
    # journals durable logical keys, including composite keys without row IDs.
    choose_field(store, entry, 'genre', 'Blues')
    with store.conn:
        catalog.choose_field(store.conn, entry, 'bpm', '', 'user')
    finish(store.conn)
    staged = {row['field']:dict(row) for row in store.conn.execute(
        f"SELECT * FROM {preparation.TABLE} WHERE owner='entry_field_choices'")}
    assert staged['genre']['text_value'] == 'Blues'
    assert staged['bpm']['value_kind'] == 'blank'
    assert staged['bpm']['number_value'] is None
    assert staged['genre']['field_scope'] == 'legacy_item'
    assert staged['genre']['recording_id'] is None
    for owner, keys in preparation.KEYS.items():
        originals = {tuple(row[k] for k in keys):dict(row) for row in store.conn.execute('SELECT * FROM '+owner)}
        copies = {tuple(json.loads(row['source_key'])):json.loads(row['source_snapshot'])
                  for row in store.conn.execute(f'SELECT * FROM {preparation.TABLE} WHERE owner=?', (owner,))}
        assert copies == originals
    assert list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == before
    assert not store.conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='metadata_observations'").fetchone()
    assert store.tracks.get_track_enrichment(entry)[0] == 'Blues'
    # Delete and recreate an existing composite key after preparation.
    with store.conn:
        store.conn.execute("DELETE FROM entry_field_choices WHERE entry_id=? AND field='genre'", (entry,))
        store.conn.execute("INSERT INTO entry_field_choices VALUES (?,'genre','null','user')", (entry,))
    assert job.progress(store.conn).prepared_revision is None
    finish(store.conn)
    row = store.conn.execute(f"SELECT value_kind,text_value FROM {preparation.TABLE} WHERE owner='entry_field_choices' AND field='genre'").fetchone()
    assert tuple(row) == ('null', None)
    snapshots = [json.loads(row[0]) for row in store.conn.execute(f"SELECT source_snapshot FROM {preparation.ORIGINALS} "
        "WHERE owner='entry_field_choices' AND json_extract(source_key,'$[1]')='genre'")]
    assert {'Blues', None} <= {json.loads(snapshot['value_json']) for snapshot in snapshots}


def test_metadata_failure_is_durable_and_does_not_block_live_writers(predecessor, independent_regular_ddl):
    store = predecessor
    entry, _, _ = seed(store)
    job = preparation.JOB
    job.install(store.conn)
    with store.conn:
        store.conn.execute("UPDATE entry_field_choices SET value_json='\"bad-number\"' WHERE field='bpm'")
    with pytest.raises(MigrationError, match='active metadata choice'):
        finish(store.conn)
    failed = job.progress(store.conn)
    assert failed.status == 'failed' and failed.processed > 0
    choose_field(store, entry, 'bpm', 123)
    assert job.progress(store.conn).status == 'failed'
    with pytest.raises(MigrationError, match='retry explicitly'):
        job.step(store.conn)
    regular.migrate(store.conn)
    job.step(store.conn, retry=True)
    assert finish(store.conn).status == 'prepared'
    assert store.conn.execute(f"SELECT number_value FROM {preparation.TABLE} WHERE owner='entry_field_choices' AND field='bpm'").fetchone()[0] == 123


def test_metadata_interruption_rolls_back_only_current_batch_and_dependency_invalidates(predecessor):
    store = predecessor
    seed(store)
    job = preparation.JOB
    job.install(store.conn)
    committed = job.step(store.conn, batch_size=1)
    original = job.copy
    def interrupted(conn, owner, key, value):
        original(conn, owner, key, value)
        raise KeyboardInterrupt
    interrupted_job = replace(job, copy=interrupted)
    with pytest.raises(KeyboardInterrupt):
        interrupted_job.step(store.conn, batch_size=1)
    assert job.progress(store.conn) == committed
    complete = finish(store.conn)
    with store.conn:
        genre_identity.add_custom(store.conn, 'New prepared label')
    assert job.progress(store.conn).prepared_revision is None
    assert finish(store.conn).input_revision > complete.input_revision


def test_cli_status_is_read_only_and_wrong_staging_definitions_are_not_adopted(predecessor, tmp_path, capsys):
    from yt_playlist.core.preparation.rows import main
    store = predecessor
    job = preparation.JOB
    before = list(store.conn.iterdump())
    assert main(job, [str(store._db_path), '--status']) == 0
    assert json.loads(capsys.readouterr().out) == {'status': 'uninitialized'}
    assert list(store.conn.iterdump()) == before
    job.install(store.conn)
    before = list(store.conn.iterdump())
    assert main(job, [str(store._db_path), '--status']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'copying'
    assert list(store.conn.iterdump()) == before
    missing = tmp_path/'never-create.db'
    with pytest.raises(SystemExit) as failed:
        main(job, [str(missing)])
    assert failed.value.code == 1 and not missing.exists()
    with store.conn:
        store.conn.execute('DROP TRIGGER prepare_metadata_entry_field_choices_update')
    damaged = list(store.conn.iterdump())
    with pytest.raises(MigrationError, match='Incompatible preparation object'):
        job.install(store.conn)
    assert list(store.conn.iterdump()) == damaged


def test_operational_fields_keep_recorded_subjects_and_all_observed_source_revisions(predecessor):
    store = predecessor
    entry, recording, _ = seed(store)
    preparation.JOB.install(store.conn)
    finish(store.conn)
    rows = store.conn.execute(f'SELECT o.* FROM {preparation.OPERATIONAL} o JOIN {preparation.TABLE} h '
        'ON h.owner=o.owner AND h.source_key=o.source_key AND h.source_digest=o.source_digest '
        'ORDER BY o.owner,o.position').fetchall()
    assert rows and all(row['recording_id'] == recording and row['entry_id'] is None for row in rows)
    assert any(row['field'] == 'year' and row['canonical_field'] == 'mb_year' for row in rows)
    assert any(row['field'] == 'bpm' and row['value_kind'] == 'value' and row['number_value'] == 120 for row in rows)
    assert all(row['input_video_id'] == 'video' for row in rows if row['owner'] == 'provider_attempts')
    original = store.conn.execute("SELECT * FROM provider_attempts ORDER BY id LIMIT 1").fetchone()
    key = json.dumps([original['id']], separators=(',', ':'))
    before = store.conn.execute(f'SELECT source_digest FROM {preparation.TABLE} WHERE owner=? AND source_key=?',
                                ('provider_attempts', key)).fetchone()[0]
    replacement = {'bpm': '', 'genre': None, 'isrc': ['exact', 'exact'], 'mb_year': '2001'}
    with store.conn:
        store.conn.execute('UPDATE provider_attempts SET fields_json=? WHERE id=?', (json.dumps(replacement), original['id']))
    finish(store.conn)
    after = store.conn.execute(f'SELECT source_digest FROM {preparation.TABLE} WHERE owner=? AND source_key=?',
                               ('provider_attempts', key)).fetchone()[0]
    assert before != after
    current = {row['field']: dict(row) for row in store.conn.execute(f'SELECT * FROM {preparation.OPERATIONAL} '
        'WHERE owner=? AND source_key=? AND source_digest=?', ('provider_attempts', key, after))}
    assert current['bpm']['value_kind'] == 'blank' and current['genre']['value_kind'] == 'null'
    assert current['isrc']['value_kind'] is None and json.loads(current['isrc']['raw_value']) == ['exact', 'exact']
    assert store.conn.execute(f'SELECT count(*) FROM {preparation.OPERATIONAL} '
        'WHERE owner=? AND source_key=? AND source_digest=?', ('provider_attempts', key, before)).fetchone()[0] > 0
    assert json.loads(store.conn.execute(f'SELECT source_snapshot FROM {preparation.ORIGINALS} '
        'WHERE owner=? AND source_key=? AND digest=?', ('provider_attempts', key, before)).fetchone()[0]) == dict(original)
    choose_field(store, entry, 'bpm', 124)
    assert store.conn.execute('SELECT bpm FROM catalog_tracks WHERE id=?', (entry,)).fetchone()[0] == 124


@pytest.mark.parametrize('raw', ['null', '[]', '{} ' + ' ' * preparation.MAX_OPERATIONAL_BYTES,
                                 '{"unclassified_provider_field":1}', ''])
def test_invalid_operational_input_fails_durably_without_rewriting_it(predecessor, raw):
    store = predecessor
    seed(store)
    with store.conn:
        store.conn.execute('UPDATE provider_attempts SET fields_json=?', (raw,))
    preparation.JOB.install(store.conn)
    with pytest.raises((MigrationError, ValueError)):
        finish(store.conn)
    assert preparation.JOB.progress(store.conn).status == 'failed'
    assert store.conn.execute('SELECT fields_json FROM provider_attempts').fetchone()[0] == raw


def test_large_requested_batch_is_capped_before_expanding_provider_fields(predecessor):
    store = predecessor
    _, recording, _ = seed(store)
    with store.conn:
        store.conn.executemany('INSERT INTO provider_attempts(recording_id,input_video_id,provider,at,status,fields_json) '
            "VALUES (?,'video','more',10,'ok','{}')", [(recording,)] * 40)
    preparation.JOB.install(store.conn)
    prior = 0
    for _ in range(100):
        result = preparation.JOB.step(store.conn, batch_size=512)
        assert 0 <= result.processed - prior <= 16
        prior = result.processed
        if result.status == 'prepared':
            break
    else:
        pytest.fail('Capped metadata work did not finish')
