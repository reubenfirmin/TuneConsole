"""Key enforcement, stable feedback IDs, and atomic parent-table replacement."""
from contextlib import closing
import json
import sqlite3

import pytest

from yt_playlist.core import catalog, mood
from yt_playlist.core.migrations import Migration, MigrationError, migrate, registry
from yt_playlist.core.migrations import v066_explicit_keys as explicit_keys


UPGRADE = Migration(66, 'explicit_logical_keys', explicit_keys.apply, rebuild_tables=True)


def upgrade(conn, migration=UPGRADE):
    migrate(conn, registry=(*registry.MIGRATIONS[:65], migration))


@pytest.fixture
def predecessor(tmp_path, _pristine_installed_schema):
    with closing(sqlite3.connect(tmp_path / 'keys.db')) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        _pristine_installed_schema(65).backup(conn)
        conn.executemany('INSERT INTO recordings(id,identity_key,title,artist) VALUES (?,?,?,?)',
                         [(17, 'first|artist', 'First', 'Artist'), (18, 'second|artist', 'Second', 'Artist')])
        conn.execute('INSERT INTO rec_mood(rowid,created_at,direction,keys) VALUES (22,100,1,?)',
                     (json.dumps(['first|artist', 'unknown|artist', 'second|artist']),))
        conn.execute('INSERT INTO rec_mood(rowid,created_at,direction,keys) VALUES (23,100,-2,?)',
                     (json.dumps(['second|artist']),))
        conn.commit()
        yield conn


def test_rebuild_preserves_cascade_children_payloads_rowids_and_extensions(predecessor):
    conn = predecessor
    conn.execute("INSERT INTO settings(rowid,key,value) VALUES (1000,'personal','retain')")
    conn.execute("INSERT INTO rec_serving_policies VALUES ('version','{}',0)")
    conn.execute("INSERT INTO rec_policy_bases VALUES ('base','{}',2,0,0)")
    conn.execute("INSERT INTO rec_policy_variants VALUES ('version','base',0)")
    conn.execute("INSERT INTO rec_vectors VALUES ('first|artist',X'000000FF')")
    conn.execute("INSERT INTO rec_proposals VALUES ('fresh_pool','null',0)")
    conn.execute("INSERT INTO rec_proposal_cache_builds(cache_id,surface,byte_size,chunk_count,nonempty,created_at,expires_at) "
                 "VALUES ('cache','fresh_pool',5,1,1,0,100)")
    conn.execute("INSERT INTO rec_proposal_cache_chunks VALUES ('cache',0,X'6361636865')")
    conn.execute("UPDATE rec_proposal_cache_builds SET state=1 WHERE cache_id='cache'")
    conn.execute("INSERT INTO rec_proposal_cache_heads VALUES ('fresh_pool','cache',0)")
    conn.execute('CREATE TABLE settings_audit (key TEXT)')
    conn.execute('CREATE TRIGGER settings_extension AFTER UPDATE ON settings BEGIN '
                 'INSERT INTO settings_audit VALUES (NEW.key); END')
    conn.execute('CREATE VIEW settings_extension_view AS SELECT key,value FROM settings')
    conn.commit()
    retained = {table: list(map(tuple, conn.execute(f'SELECT rowid,* FROM {table}'))) for table in (
        'settings', 'rec_serving_policies', 'rec_policy_bases', 'rec_policy_variants',
        'rec_vectors', 'rec_proposals', 'rec_proposal_cache_heads')}
    upgrade(conn)
    for table, expected in retained.items():
        assert list(map(tuple, conn.execute(f'SELECT rowid,* FROM {table}'))) == expected, table
    assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    conn.execute("UPDATE settings SET value='new' WHERE key='personal'")
    assert [tuple(row) for row in conn.execute('SELECT * FROM settings_audit')] == [('personal',)]
    assert conn.execute("SELECT value FROM settings_extension_view WHERE key='personal'").fetchone()[0] == 'new'


def test_every_nullable_logical_key_rejects_null_after_rebuild(predecessor):
    conn = predecessor
    upgrade(conn)
    for name, (key, _, _) in explicit_keys.TABLES.items():
        if name == 'stale_dismissed':
            continue
        with pytest.raises(sqlite3.IntegrityError, match=f'NOT NULL constraint failed: {name}.{key}'):
            conn.execute(f'INSERT INTO "{name}"("{key}") VALUES (NULL)')
    with pytest.raises(sqlite3.IntegrityError, match='UNIQUE constraint failed: settings.key'):
        conn.execute("INSERT INTO settings VALUES ('catalog_identity_version','duplicate')")


def test_mood_ids_order_membership_unknowns_and_restart(predecessor):
    conn = predecessor
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    upgrade(conn)
    assert [tuple(row) for row in conn.execute('SELECT * FROM mood_events ORDER BY id')] == [(22,100,1),(23,100,-2)]
    assert mood.recent(conn, 200) == [(100,-2,['second|artist']),
                                    (100,1,['first|artist','unknown|artist','second|artist'])]
    assert [tuple(row) for row in conn.execute('SELECT * FROM mood_event_unresolved_seeds')] == [
        (22, 'unknown|artist', 1, 'unknown_recording')]
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='rec_mood'").fetchone()
    assert not conn.execute('SELECT 1 FROM recordings WHERE id NOT IN (17,18)').fetchone()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute('INSERT INTO mood_event_recordings VALUES (NULL,17,0)')
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute('INSERT INTO mood_event_recordings VALUES (22,NULL,0)')
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute('INSERT INTO mood_event_recordings VALUES (22,17,99)')
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute('DELETE FROM recordings WHERE id=17')
    conn.rollback()
    with closing(sqlite3.connect(path)) as reopened, reopened:
        assert mood.recent(reopened, 200)[0] == (100,-2,['second|artist'])
        reopened.execute('PRAGMA foreign_keys=ON')
        reopened.execute('DELETE FROM mood_events')
        assert reopened.execute('SELECT count(*) FROM mood_event_recordings').fetchone()[0] == 0
        assert reopened.execute('SELECT count(*) FROM mood_event_unresolved_seeds').fetchone()[0] == 0
        assert mood.record(reopened, ['first|artist'], 1, 100) > 23


def test_merge_deduplicates_memberships_without_combining_events(predecessor):
    conn = predecessor
    upgrade(conn)
    mood.reassociate(conn, 'first|artist', 18, previous_recording_id=17)
    assert [tuple(row) for row in conn.execute('SELECT id FROM mood_events ORDER BY id')] == [(22,), (23,)]
    assert mood.recent(conn, 200) == [(100,-2,['second|artist']), (100,1,['second|artist','unknown|artist'])]
    assert [tuple(row) for row in conn.execute('SELECT * FROM mood_event_recordings ORDER BY mood_event_id')] == [
        (22,18,0), (23,18,0)]


def test_retention_prunes_both_seed_relations_with_deterministic_ties(predecessor):
    conn = predecessor
    upgrade(conn)
    newer = mood.record(conn, ['first|artist', 'unresolved-new'], 2, 100, cap=2)
    assert [row[0] for row in conn.execute('SELECT id FROM mood_events ORDER BY id')] == [23, newer]
    assert not conn.execute('SELECT 1 FROM mood_event_recordings WHERE mood_event_id=22').fetchone()
    assert not conn.execute('SELECT 1 FROM mood_event_unresolved_seeds WHERE mood_event_id=22').fetchone()
    latest = mood.record(conn, [], -1, 200, cap=2, prune_before=101)
    assert [row[0] for row in conn.execute('SELECT id FROM mood_events')] == [latest]
    assert not conn.execute('SELECT 1 FROM mood_event_recordings').fetchone()
    assert not conn.execute('SELECT 1 FROM mood_event_unresolved_seeds').fetchone()
    assert mood.recent(conn, 2) == [(200, -1, [])]


def test_mood_aliases_preserve_ambiguity_and_later_resolve_without_fake_parents(predecessor):
    conn = predecessor
    conn.executemany('INSERT INTO recording_aliases VALUES (?,?,?)',
                     [('ambiguous',17,'fixture'), ('ambiguous',18,'fixture'), ('unique',17,'fixture')])
    conn.commit()
    upgrade(conn)
    event_id = mood.record(conn, ['unique','first|artist','ambiguous','later'], 1, 200)
    assert mood.recent(conn, 1) == [(200,1,['first|artist','ambiguous','later'])]
    assert [tuple(row) for row in conn.execute('SELECT legacy_key,reason FROM mood_event_unresolved_seeds '
                                              'WHERE mood_event_id=? ORDER BY position', (event_id,))] == [
        ('ambiguous','ambiguous_alias'), ('later','unknown_recording')]
    mood.reassociate(conn, 'ambiguous', 17)
    assert conn.execute('SELECT reason FROM mood_event_unresolved_seeds '
                        "WHERE mood_event_id=? AND legacy_key='ambiguous'", (event_id,)).fetchone()[0] == 'ambiguous_alias'
    conn.execute("INSERT INTO recordings(id,identity_key) VALUES (19,'later')")
    mood.resolve_pending(conn, 'later')
    assert conn.execute('SELECT recording_id FROM mood_event_recordings '
                        'WHERE mood_event_id=? AND recording_id=19', (event_id,)).fetchone()
    conn.execute("UPDATE recordings SET identity_key='later-renamed' WHERE id=19")
    assert mood.recent(conn, 1) == [(200,1,['first|artist','ambiguous','later-renamed'])]


def test_positions_cannot_overlap_resolved_and_unresolved_seeds(predecessor):
    conn = predecessor
    upgrade(conn)
    with pytest.raises(sqlite3.IntegrityError, match='Mood seed position is already occupied'):
        conn.execute("INSERT INTO mood_event_unresolved_seeds VALUES (22,'collision',0,'unknown_recording')")
    with pytest.raises(sqlite3.IntegrityError, match='Mood seed position is already occupied'):
        conn.execute('UPDATE mood_event_recordings SET position=1 WHERE mood_event_id=22 AND recording_id=17')


def test_catalog_admission_resolves_pending_seeds_and_ambiguity_preserves_known_owner(predecessor):
    conn = predecessor
    upgrade(conn)
    event = mood.record(conn, ['future|artist'], 1, 200)
    first = catalog.upsert(conn, 'future-one', 'Future', 'Artist', preferred_key='future|artist')
    original = conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (first,)).fetchone()[0]
    assert conn.execute('SELECT recording_id FROM mood_event_recordings WHERE mood_event_id=?',
                        (event,)).fetchone()[0] == original
    assert not conn.execute('SELECT 1 FROM mood_event_unresolved_seeds WHERE mood_event_id=?', (event,)).fetchone()

    catalog.upsert(conn, 'future-two', 'Future', 'Artist', preferred_key='future|artist')
    key = conn.execute('SELECT identity_key FROM recordings WHERE id=?', (original,)).fetchone()[0]
    assert key != 'future|artist'
    assert mood.recent(conn, 1) == [(200, 1, [key])]
    ambiguous = mood.record(conn, ['future|artist'], 1, 201)
    assert conn.execute('SELECT reason FROM mood_event_unresolved_seeds WHERE mood_event_id=?',
                        (ambiguous,)).fetchone()[0] == 'ambiguous_alias'


def test_catalog_retirement_resolves_all_transferred_aliases_and_keeps_event_ids(predecessor):
    conn = predecessor
    upgrade(conn)
    migrate(conn, registry=registry.MIGRATIONS)
    old_entry = catalog.upsert(conn, 'old-upload', 'Old', 'Artist')
    new_entry = catalog.upsert(conn, 'new-upload', 'New', 'Artist')
    old, target = [conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (entry,)).fetchone()[0]
                   for entry in (old_entry, new_entry)]
    catalog.add_alias(conn, 'formerly-ambiguous', old, 'fixture')
    catalog.add_alias(conn, 'formerly-ambiguous', target, 'fixture')
    first = mood.record(conn, ['old|artist', 'formerly-ambiguous', 'new|artist'], 1, 200)
    second = mood.record(conn, ['old|artist'], -1, 200)
    catalog.associate_item(conn, 'old-upload', target, source='fixture', evidence={'verified': True}, now=201)
    assert [row[0] for row in conn.execute('SELECT id FROM mood_events WHERE id IN (?,?) ORDER BY id',
                                          (first, second))] == [first, second]
    assert mood.recent(conn, 2) == [(200, -1, ['new|artist']), (200, 1, ['new|artist'])]
    assert not conn.execute('SELECT 1 FROM mood_event_unresolved_seeds WHERE mood_event_id=?', (first,)).fetchone()
    assert [tuple(row) for row in conn.execute('SELECT * FROM mood_event_recordings WHERE mood_event_id IN (?,?) '
                                              'ORDER BY mood_event_id', (first, second))] == [
        (first, target, 0), (second, target, 0)]


def test_partial_upload_reassociation_leaves_recording_mood_with_its_owner(predecessor):
    conn = predecessor
    upgrade(conn)
    migrate(conn, registry=registry.MIGRATIONS)
    entries = [catalog.upsert(conn, video, title, 'Artist') for video, title in (
        ('retained-upload', 'Retained'), ('moved-upload', 'Moved'), ('target-upload', 'Target'))]
    old, _, target = [conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (entry,)).fetchone()[0]
                      for entry in entries]
    catalog.associate_item(conn, 'moved-upload', old, source='fixture', evidence={'verified': True}, now=200)
    event = mood.record(conn, ['retained|artist'], 1, 201)
    catalog.associate_item(conn, 'moved-upload', target, source='fixture', evidence={'corrected': True}, now=202)
    assert conn.execute('SELECT recording_id FROM mood_event_recordings WHERE mood_event_id=?',
                        (event,)).fetchone()[0] == old
    assert mood.recent(conn, 1) == [(201, 1, ['retained|artist'])]


def test_v66_remains_a_validated_predecessor_for_the_next_schema(predecessor, monkeypatch):
    from yt_playlist.core.migrations import current
    from yt_playlist.core.migrations.runner import validate_version

    conn = predecessor
    upgrade(conn)
    monkeypatch.setattr(current, 'VERSION', 67)
    validate_version(conn, 66)
    conn.execute('DROP INDEX ix_mood_event_order')
    with pytest.raises(MigrationError, match='ix_mood_event_order'):
        validate_version(conn, 66)


def test_stamped_v65_missing_lookup_is_rejected_before_key_replacement(predecessor):
    conn = predecessor
    conn.execute('DROP INDEX ix_group_library_inputs')
    conn.commit()
    before = list(conn.iterdump())
    with pytest.raises(MigrationError, match='ix_group_library_inputs'):
        migrate(conn, registry=registry.MIGRATIONS)
    assert list(conn.iterdump()) == before
    assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1


def test_new_event_ids_do_not_reuse_retained_legacy_receipt_ids(predecessor):
    conn = predecessor
    conn.execute("INSERT INTO identity_state_archive VALUES ('rec_mood','900','original receipt','legacy identity conversion')")
    conn.execute('DELETE FROM rec_mood')
    conn.commit()
    upgrade(conn)
    assert mood.record(conn, [], 1, 200) == 901
    assert tuple(conn.execute("SELECT source_id,payload FROM identity_state_archive WHERE kind='rec_mood'").fetchone()) == (
        '900', 'original receipt')


@pytest.mark.parametrize('invalid', ['null_key', 'bad_seed', 'non_string_seed'])
def test_invalid_legacy_rows_are_diagnosed_and_preserved_before_cutover(predecessor, invalid):
    conn = predecessor
    if invalid == 'null_key':
        conn.executemany('INSERT INTO settings(key,value) VALUES (NULL,?)', [('first body',), ('second body',)])
        message = 'NULL logical key settings.key'
    elif invalid == 'bad_seed':
        conn.execute("UPDATE rec_mood SET keys='unparsed original' WHERE rowid=22")
        message = 'Invalid mood event 22'
    else:
        conn.execute("UPDATE rec_mood SET keys='[null]' WHERE rowid=22")
        message = 'Invalid mood event 22'
    conn.commit()
    before = list(conn.iterdump())
    with pytest.raises(MigrationError, match=message):
        upgrade(conn)
    assert list(conn.iterdump()) == before
    assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1


def test_interrupted_rebuild_rolls_back_schema_memberships_and_history(predecessor):
    conn = predecessor
    before = list(conn.iterdump())
    def interrupted(connection, context):
        explicit_keys.apply(connection, context)
        raise RuntimeError('interrupted after replacement')
    with pytest.raises(MigrationError, match='interrupted after replacement'):
        upgrade(conn, Migration(66, UPGRADE.name, interrupted, rebuild_tables=True))
    assert list(conn.iterdump()) == before
    assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    upgrade(conn)
    before = list(conn.iterdump())
    upgrade(conn)
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize('foreign_keys', [0,1])
def test_rebuild_runner_checks_references_and_restores_enforcement(predecessor, foreign_keys):
    conn = predecessor
    conn.execute(f'PRAGMA foreign_keys={foreign_keys}')
    before = list(conn.iterdump())
    def broken(connection, context):
        connection.execute('CREATE TABLE broken_reference (recording_id INTEGER REFERENCES recordings(id))')
        connection.execute('INSERT INTO broken_reference VALUES (999999)')
    with pytest.raises(MigrationError, match='Table replacement broke a foreign-key reference'):
        upgrade(conn, Migration(66, UPGRADE.name, broken, rebuild_tables=True))
    assert list(conn.iterdump()) == before
    assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == foreign_keys


def test_optional_installed_dismissals_survive_with_explicit_keys(predecessor):
    conn = predecessor
    conn.execute('CREATE TABLE stale_dismissed (ytm TEXT PRIMARY KEY,until REAL)')
    conn.execute("INSERT INTO stale_dismissed(rowid,ytm,until) VALUES (9,'old-playlist',NULL)")
    conn.commit()
    upgrade(conn)
    assert tuple(conn.execute('SELECT rowid,* FROM stale_dismissed').fetchone()) == (9,'old-playlist',None)
    with pytest.raises(sqlite3.IntegrityError, match='NOT NULL constraint failed: stale_dismissed.ytm'):
        conn.execute('INSERT INTO stale_dismissed VALUES (NULL,NULL)')


def test_unrecognized_installed_constraints_are_preserved_before_cutover(predecessor):
    conn = predecessor
    conn.execute('ALTER TABLE settings RENAME TO replaced_settings')
    conn.execute('CREATE TABLE settings (key TEXT PRIMARY KEY,value TEXT CHECK(value IS NOT NULL))')
    conn.execute('INSERT INTO settings SELECT * FROM replaced_settings')
    conn.execute('DROP TABLE replaced_settings')
    conn.commit()
    before = list(conn.iterdump())
    with pytest.raises(MigrationError, match='Unrecognized constraints in settings'):
        upgrade(conn)
    assert list(conn.iterdump()) == before
    assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1


def test_startup_rechecks_history_after_releasing_reservation(predecessor):
    conn = predecessor
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    class RacingConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            result = super().execute(sql, parameters)
            if sql == 'PRAGMA foreign_keys=OFF':
                # A separate startup completes the same version during the
                # reservation gap required to disable FK actions.
                upgrade(conn)
            return result
    with closing(sqlite3.connect(path, factory=RacingConnection)) as racing, racing:
        racing.execute('PRAGMA foreign_keys=ON')
        upgrade(racing)
        assert racing.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert racing.execute('SELECT count(*) FROM schema_migrations WHERE version=66').fetchone()[0] == 1
        assert not racing.execute('PRAGMA foreign_key_check').fetchall()
