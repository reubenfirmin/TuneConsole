"""Paid copy and quota evidence survive relocation independently of display caches."""
import json
from pathlib import Path
import sqlite3
import shutil
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier

import pytest

from yt_playlist.core import (album_description_files as files,
                              album_aliases, audio_annotation_files, audio_annotations, choice_evidence_files,
                              policy_documents, sample_files)
from tests import file_custody_support as custody
from tests import paid_request_support as requests
from yt_playlist.core.migrations import migrate, registry
from yt_playlist.core.migrations import v070_policy_documents as policies
from yt_playlist.core.migrations import v071_album_descriptions as migration
from yt_playlist.core.migrations.runner import Context, MigrationError
from yt_playlist.core.store import Store
from yt_playlist.core.activity import WorkClosing
from yt_playlist.integrations.deepseek import DeepSeekIAO, DescriptionUnavailable
from yt_playlist.providers import deepseek
from yt_playlist.rec import album_descriptions


@pytest.fixture
def predecessor(tmp_path, _pristine_installed_schema):
    with closing(sqlite3.connect(tmp_path / 'paid.db')) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        _pristine_installed_schema(69).backup(conn)
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            policies.apply(conn, Context(4))
            policy_documents.install(conn, [('new-policy', '{"prompt_version":1}', 1)])
            conn.execute("INSERT INTO albums(id,title,metadata_rank) VALUES (1,'Canonical release',0)")
        yield conn


def seed(conn):
    good = json.dumps({'status': 'ready', 'description': 'Preserved paid prose. é', 'model': 'deepseek-flash',
                      'at': 86405, 'usage': {'prompt_tokens': 11, 'completion_tokens': 7, 'total_tokens': 18},
                      'album': {'artist': 'Artist', 'title': 'Same title'}, 'opaque': ['retained', 1]},
                     ensure_ascii=False, indent=2)
    unknown = json.dumps({'status': 'unknown', 'description': None, 'model': 'deepseek-flash', 'at': 86406,
                         'retry_at': 86400 * 8, 'usage': {'total_tokens': 9}, 'confidence': 'low'})
    usage = json.dumps({'day': 1, 'requests': 4, 'status': 'unknown', 'credential': 'credential-digest',
                        'lease_until': 0, 'token': None, 'send_authorized': True, 'retry_at': 0,
                        'additional_evidence': ['retain original bytes']})
    rows = [('album_description:unresolved-hash', good, 86405.5),
            ('album_description:unknown-hash', unknown, 86406.5),
            ('album_description_usage', usage, 86406.5),
            ('deepseek_credential:blocked-key', '{"status":"auth","retry_at":99999,"opaque":true}', 86400),
            ('deepseek_host', '{"status":"unavailable","retry_at":99000}', 86401),
            ('album_notes', '{"display-only":"untouched"}', 86402),
            ('albumXdescription:keep', '{"unrecognized":"do not classify"}', 86403)]
    with conn:
        conn.executemany('INSERT INTO rec_proposals VALUES (?,?,?)', rows)
    return {surface: (raw, built) for surface, raw, built in rows}


def apply(conn):
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        migration.apply(conn, Context(4))
    migration.validate(conn)


def test_every_original_paid_result_and_quota_fact_is_preserved_without_inventing_release_identity(predecessor):
    conn = predecessor
    original = seed(conn)
    apply(conn)
    requests = conn.execute('SELECT * FROM album_description_requests ORDER BY legacy_identity').fetchall()
    assert len(requests) == 2
    for row in requests:
        raw, built = original['album_description:' + row['legacy_identity']]
        assert row['album_id'] is None and row['policy_version'] is None and row['attempt_count'] is None
        assert row['resolution_status'] == 'legacy_unresolved'
        assert row['updated_at'] == built
        assert row['retry_at'] == json.loads(raw).get('retry_at', 0)
        assert files.read(row['outcome_filename'], row['outcome_digest']) == raw
    paid = conn.execute('SELECT d.*,r.legacy_identity FROM album_descriptions d '
                        'JOIN album_description_requests r ON r.id=d.request_id').fetchall()
    assert len(paid) == 1 and paid[0]['legacy_identity'] == 'unresolved-hash'
    assert files.read(paid[0]['filename'], paid[0]['content_digest']) == original['album_description:unresolved-hash'][0]
    legacy = conn.execute('SELECT * FROM legacy_album_description_usage').fetchone()
    assert (legacy['period'], legacy['request_count'], legacy['send_authorized']) == (1, 4, 1)
    assert files.read(legacy['filename'], legacy['content_digest']) == original['album_description_usage'][0]
    debits = conn.execute("SELECT request_id,legacy_usage_id,units FROM provider_usage WHERE usage_kind='requests'").fetchall()
    assert list(map(tuple, debits)) == [(None, legacy['id'], 4)]
    assert dict(conn.execute('SELECT usage_kind,sum(units) FROM provider_usage GROUP BY usage_kind')) == {
        'requests': 4, 'prompt_tokens': 11, 'completion_tokens': 7, 'total_tokens': 27}
    for row in conn.execute('SELECT * FROM album_description_access'):
        key = 'deepseek_host' if row['scope'] == 'host' else 'deepseek_credential:' + row['credential_key']
        assert files.read(row['evidence_filename'], row['evidence_digest']) == original[key][0]
    assert set(row[0] for row in conn.execute('SELECT surface FROM rec_proposals')) == {'album_notes', 'albumXdescription:keep'}
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('problem', ['invalid_units', 'invalid_time', 'malformed', 'nonfinite',
                                    'unknown_status', 'missing_model', 'missing_failure_credential'])
def test_invalid_originals_are_rejected_before_any_file_or_table_is_created(predecessor, problem):
    conn = predecessor
    seed(conn)
    surface = 'album_description_usage' if problem == 'missing_failure_credential' else 'album_description:unresolved-hash'
    raw = conn.execute('SELECT payload FROM rec_proposals WHERE surface=?', (surface,)).fetchone()[0]
    value = json.loads(raw)
    if problem == 'invalid_units':
        value['usage']['total_tokens'] = True
    elif problem == 'invalid_time':
        value['at'] = 'not a time'
    elif problem == 'nonfinite':
        value['opaque'] = float('inf')
    elif problem == 'unknown_status':
        value['status'] = 'unrecognized status'
    elif problem == 'missing_model':
        del value['model']
    elif problem == 'missing_failure_credential':
        value.update(status='auth', credential=None)
    with conn:
        conn.execute('UPDATE rec_proposals SET payload=? WHERE surface=?',
                     ('original malformed document' if problem == 'malformed' else json.dumps(value), surface))
    before = list(conn.iterdump())
    with pytest.raises(MigrationError, match='Invalid paid description evidence'), conn:
        conn.execute('BEGIN IMMEDIATE')
        migration.apply(conn, Context(4))
    assert list(conn.iterdump()) == before
    assert not files.directory(conn).exists()


def test_interruption_preserves_all_old_owners_and_restarts_with_verified_files(predecessor, monkeypatch):
    conn = predecessor
    original = seed(conn)
    before = list(conn.iterdump())
    publish = files.publish
    count = 0
    def interrupted(*args):
        nonlocal count
        count += 1
        result = publish(*args)
        if count == 2:
            raise KeyboardInterrupt('controlled interruption after durable publication')
        return result
    monkeypatch.setattr(files, 'publish', interrupted)
    with pytest.raises(KeyboardInterrupt), conn:
        conn.execute('BEGIN IMMEDIATE')
        migration.apply(conn, Context(4))
    assert list(conn.iterdump()) == before
    assert len(list(files.directory(conn).glob('*/*.json'))) == 2
    monkeypatch.setattr(files, 'publish', publish)
    apply(conn)
    for row in conn.execute('SELECT legacy_identity,outcome_filename,outcome_digest FROM album_description_requests'):
        assert files.read(row[1], row[2]) == original['album_description:' + row[0]][0]
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()


def request(conn, key, *, album=1, policy='new-policy', state='pending', token=None):
    return conn.execute('INSERT INTO album_description_requests('
        'album_id,request_key,policy_version,resolution_status,status,claim_token,lease_until,'
        'attempt_count,credential_key,created_at,updated_at) '
        "VALUES (?,?,?,'resolved',?,?,?,1,?,1,1)",
        (album, key, policy, state, token, 60 if token else 0, 'key' if token else None)).lastrowid


def test_new_requests_require_exact_owners_and_enforce_unique_admission_and_tokens(predecessor):
    conn = predecessor
    apply(conn)
    for album, policy in ((None, 'new-policy'), (99, 'new-policy'), (1, None), (1, 'missing')):
        with pytest.raises(sqlite3.IntegrityError), conn:
            request(conn, 'invalid', album=album, policy=policy)
    with conn:
        first = request(conn, 'first', state='claimed', token='exclusive-token')
    for key, token in (('first', 'different-token'), ('second', 'exclusive-token')):
        with pytest.raises(sqlite3.IntegrityError), conn:
            request(conn, key, state='claimed', token=token)
    for sql in ("UPDATE album_description_requests SET album_id=99 WHERE id=?",
                "UPDATE album_description_requests SET policy_version='missing' WHERE id=?",
                "UPDATE album_description_requests SET status='sent' WHERE id=?"):
        with pytest.raises(sqlite3.IntegrityError), conn:
            conn.execute(sql, (first,))
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()


def test_paid_success_and_debits_cannot_be_erased_or_replaced_by_cache_cleanup(predecessor):
    conn = predecessor
    seed(conn)
    apply(conn)
    with conn:
        conn.execute('DELETE FROM rec_proposals')
    assert conn.execute('SELECT count(*) FROM album_descriptions').fetchone()[0] == 1
    for sql in ('DELETE FROM album_descriptions', 'DELETE FROM album_description_requests',
                'DELETE FROM provider_usage', 'UPDATE provider_usage SET units=0',
                'UPDATE legacy_album_description_usage SET request_count=0',
                "UPDATE album_descriptions SET model_version='another model'",
                "UPDATE album_description_requests SET status='unknown' WHERE status='ready'",
                "UPDATE album_description_requests SET outcome_digest=NULL WHERE status='ready'"):
        with pytest.raises(sqlite3.IntegrityError), conn:
            conn.execute(sql)
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute('INSERT INTO provider_usage('
            'request_id,legacy_usage_id,reservation_key,provider,period,usage_kind,units,created_at) '
            "SELECT request_id,legacy_usage_id,reservation_key,provider,period,usage_kind,units,created_at FROM provider_usage")


def test_usage_idempotency_uses_the_request_attempt_not_an_arbitrary_reservation_key(predecessor):
    conn = predecessor
    seed(conn)
    apply(conn)
    with conn:
        owner = request(conn, 'actual-request')
        conn.execute('INSERT INTO provider_usage('
            'request_id,attempt_number,reservation_key,provider,period,usage_kind,units,created_at) '
            "VALUES (?,1,'first-debit','deepseek',1,'requests',1,86401)", (owner,))
    for attempt, key in ((1, 'another-key-same-attempt'), (None, 'unversioned-known-attempt')):
        with pytest.raises(sqlite3.IntegrityError), conn:
            conn.execute('INSERT INTO provider_usage('
                'request_id,attempt_number,reservation_key,provider,period,usage_kind,units,created_at) '
                "VALUES (?,?,?,'deepseek',1,'requests',1,86401)", (owner, attempt, key))
    # A separately counted retry is a separate attempt, never an overwrite.
    with conn:
        conn.execute('UPDATE album_description_requests SET attempt_count=2 WHERE id=?', (owner,))
        conn.execute('INSERT INTO provider_usage('
            'request_id,attempt_number,reservation_key,provider,period,usage_kind,units,created_at) '
            "VALUES (?,2,'retry-debit','deepseek',1,'requests',1,86402)", (owner,))
    assert conn.execute("SELECT sum(units) FROM provider_usage WHERE request_id=? AND usage_kind='requests'",
                         (owner,)).fetchone()[0] == 2


def test_migrated_global_auth_failure_keeps_the_strongest_access_state(predecessor):
    conn = predecessor
    seed(conn)
    with conn:
        conn.execute("UPDATE rec_proposals SET payload=? WHERE surface='album_description_usage'",
            (json.dumps({'day': 1, 'requests': 4, 'credential': 'blocked-key', 'status': 'auth',
                         'retry_at': 5, 'lease_until': 0, 'token': None, 'send_authorized': False}),))
        conn.execute("UPDATE rec_proposals SET payload=? WHERE surface='deepseek_credential:blocked-key'",
                     ('{"status":"rate_limit","retry_at":99999}',))
    apply(conn)
    row = conn.execute("SELECT status,retry_at,legacy_usage_id,evidence_filename FROM album_description_access "
                       "WHERE scope='credential' AND credential_key='blocked-key'").fetchone()
    assert tuple(row[:3]) == ('auth', 99999, 1)
    assert Path(row[3]).is_file()


def ready_record():
    return {'status': 'ready', 'description': 'A durable, completed paid description.', 'model': 'deepseek-flash',
            'usage': {'prompt_tokens': 20, 'completion_tokens': 10, 'total_tokens': 30}}


def test_real_request_admission_authorization_completion_and_restart_preserve_one_counted_result(predecessor):
    conn = predecessor
    seed(conn)
    apply(conn)
    before_backoff = list(conn.iterdump())
    assert requests.claim(conn, 1, 'new-policy', 'credential', 20, 86410) == {
        'status': 'unavailable', 'retry_at': 99000}
    assert requests.usage(conn, 86410) == 4
    assert list(conn.iterdump()) == before_backoff
    # Wait out the migrated host backoff within the same UTC usage day.
    now = 99001
    assert now // 86400 == 86410 // 86400
    claim = requests.claim(conn, 1, 'new-policy', 'credential', 20, now)
    assert claim['status'] == 'reserved' and claim['attempt'] == 1
    assert requests.usage(conn, now) == 5
    assert requests.claim(conn, 1, 'new-policy', 'replacement-key', 20, now + 1)['status'] == 'busy'
    assert not requests.authorize(conn, claim['request_id'], 'stale-token', 'credential', 20, now + 1)
    assert requests.authorize(conn, claim['request_id'], claim['token'], 'credential', 20, now + 1)
    assert not requests.authorize(conn, claim['request_id'], claim['token'], 'credential', 20, now + 2)
    assert requests.finish(conn, claim['request_id'], claim['token'], ready_record(), now + 2)
    before = set(files.directory(conn).glob('*/*.json'))
    assert not requests.finish(conn, claim['request_id'], claim['token'],
                               {'status': 'unavailable', 'retry_at': 99999}, now + 3)
    assert set(files.directory(conn).glob('*/*.json')) == before
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    with sqlite3.connect(path) as reopened:
        reopened.execute('PRAGMA foreign_keys=ON')
        assert requests.result(reopened, claim['request_id']) == ready_record()
        assert requests.claim(reopened, 1, 'new-policy', 'new-key', 20, now + 10) == {
            'status': 'cached', 'record': ready_record()}
        assert requests.usage(reopened, now + 10) == 5
        assert not reopened.execute('PRAGMA foreign_key_check').fetchall()


def test_unsent_takeover_fences_the_old_token_without_resetting_its_reservation(predecessor):
    conn = predecessor
    apply(conn)
    first = requests.claim(conn, 1, 'new-policy', 'credential', 20, 100)
    second = requests.claim(conn, 1, 'new-policy', 'credential', 20, 161)
    assert second['status'] == 'reserved' and second['request_id'] == first['request_id']
    assert second['token'] != first['token'] and second['attempt'] == 2
    assert requests.usage(conn, 161) == 2
    assert not requests.authorize(conn, first['request_id'], first['token'], 'credential', 20, 162)
    assert not requests.finish(conn, first['request_id'], first['token'], ready_record(), 162)
    assert requests.authorize(conn, second['request_id'], second['token'], 'credential', 20, 162)
    assert requests.finish(conn, second['request_id'], second['token'], ready_record(), 163)
    assert requests.result(conn, second['request_id']) == ready_record()
    assert not requests.finish(conn, first['request_id'], first['token'],
                               {'status': 'paused', 'retry_at': 200}, 164)


def test_sent_expiry_is_uncertain_and_never_readmitted_after_restart_or_day_rollover(predecessor):
    conn = predecessor
    apply(conn)
    claim = requests.claim(conn, 1, 'new-policy', 'credential', 20, 100)
    assert requests.authorize(conn, claim['request_id'], claim['token'], 'credential', 20, 101)
    assert requests.claim(conn, 1, 'new-policy', 'credential', 20, 162) == {'status': 'uncertain'}
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    with sqlite3.connect(path) as reopened:
        reopened.execute('PRAGMA foreign_keys=ON')
        assert requests.claim(reopened, 1, 'new-policy', 'replacement', 20, 172800) == {'status': 'uncertain'}
        assert reopened.execute('SELECT attempt_count FROM album_description_requests').fetchone()[0] == 1
        assert requests.usage(reopened, 100) == 1 and requests.usage(reopened, 172800) == 0
        # The original owner can supply an eventual successful response. No new
        # token was issued, so this cannot complete another worker's claim.
        assert requests.finish(reopened, claim['request_id'], claim['token'], ready_record(), 172801)
        assert requests.result(reopened, claim['request_id']) == ready_record()


def test_unavailable_after_send_stays_uncertain_instead_of_rebilling_the_same_request(predecessor):
    conn = predecessor
    apply(conn)
    claim = requests.claim(conn, 1, 'new-policy', 'credential', 20, 100)
    assert requests.authorize(conn, claim['request_id'], claim['token'], 'credential', 20, 101)
    assert requests.finish(conn, claim['request_id'], claim['token'],
                           {'status': 'unavailable', 'retry_at': 1000}, 102)
    assert requests.claim(conn, 1, 'new-policy', 'credential', 20, 1001) == {'status': 'uncertain'}
    assert requests.usage(conn, 1001) == 1


def test_midnight_invalidates_send_authorization_without_erasing_the_old_day_count(predecessor):
    conn = predecessor
    apply(conn)
    first = requests.claim(conn, 1, 'new-policy', 'credential', 1, 86399)
    assert not requests.authorize(conn, first['request_id'], first['token'], 'credential', 1, 86401)
    assert requests.finish(conn, first['request_id'], first['token'], {'status': 'paused'}, 86401)
    second = requests.claim(conn, 1, 'new-policy', 'credential', 1, 86402)
    assert second['status'] == 'reserved' and second['attempt'] == 2
    assert requests.usage(conn, 86399) == 1 and requests.usage(conn, 86402) == 1
    assert requests.authorize(conn, second['request_id'], second['token'], 'credential', 1, 86403)


def test_two_connections_cannot_admit_overlapping_paid_claims(predecessor):
    conn = predecessor
    apply(conn)
    with conn:
        conn.execute("INSERT INTO albums(id,title,metadata_rank) VALUES (2,'Another canonical release',0)")
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    barrier = Barrier(2)
    def admit(album):
        with sqlite3.connect(path, timeout=5) as worker:
            worker.execute('PRAGMA foreign_keys=ON')
            barrier.wait(timeout=3)
            return requests.claim(worker, album, 'new-policy', 'credential', 20, 100)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(admit, (1, 2)))
    assert sorted(row['status'] for row in outcomes) == ['busy', 'reserved']
    assert requests.usage(conn, 100) == 1


def test_completion_failure_after_file_publication_rolls_back_result_usage_and_status(predecessor):
    conn = predecessor
    apply(conn)
    claim = requests.claim(conn, 1, 'new-policy', 'credential', 20, 100)
    assert requests.authorize(conn, claim['request_id'], claim['token'], 'credential', 20, 101)
    with conn:
        conn.execute('CREATE TRIGGER reject_completed_request BEFORE UPDATE ON album_description_requests '
                     "WHEN NEW.status='ready' BEGIN SELECT RAISE(ABORT,'controlled completion failure'); END")
    before = list(conn.iterdump())
    with pytest.raises(sqlite3.IntegrityError, match='controlled completion failure'):
        requests.finish(conn, claim['request_id'], claim['token'], ready_record(), 102)
    assert list(conn.iterdump()) == before
    assert len(list(files.directory(conn).glob('*/*.json'))) == 1
    with conn:
        conn.execute('DROP TRIGGER reject_completed_request')
    assert requests.finish(conn, claim['request_id'], claim['token'], ready_record(), 103)
    assert requests.result(conn, claim['request_id']) == ready_record()
    assert len(list(files.directory(conn).glob('*/*.json'))) == 1


def backup(conn, root, database):
    with conn:
        conn.execute('BEGIN')
        manifest = files.export(conn, root)
        with sqlite3.connect(database) as target:
            conn.backup(target)
    restored = sqlite3.connect(database)
    restored.execute('PRAGMA foreign_keys=ON')
    return manifest, restored


def references(conn):
    return {(owner, key): (filename, digest, files.read(filename, digest))
            for owner, key, filename, digest in files._references(conn)}


def test_backup_restores_every_direct_owner_and_composite_access_key_after_original_files_are_lost(
        predecessor, tmp_path):
    conn = predecessor
    seed(conn)
    apply(conn)
    original = references(conn)
    assert {owner for owner, _key in original} == set(files.OWNERS)
    manifest, restored = backup(conn, tmp_path / 'backup', tmp_path / 'restored.db')
    try:
        original_usage = restored.execute('SELECT * FROM provider_usage ORDER BY id').fetchall()
        shutil.rmtree(files.directory(conn))
        files.restore(restored, manifest)
        actual = references(restored)
        assert set(actual) == set(original)
        for identity, (filename, digest, raw) in actual.items():
            assert (digest, raw) == original[identity][1:]
            assert Path(filename).is_relative_to(files.directory(restored))
        assert restored.execute('SELECT * FROM provider_usage ORDER BY id').fetchall() == original_usage
        assert not restored.execute('PRAGMA foreign_key_check').fetchall()
        # Repeating restore is safe, and a normal reopen needs only the restored root.
        files.restore(restored, manifest)
        with sqlite3.connect(tmp_path / 'restored.db') as reopened:
            assert references(reopened) == actual
    finally:
        restored.close()


def test_recovery_file_bundle_combines_policy_annotation_and_paid_owners_from_one_snapshot(predecessor, tmp_path):
    conn = predecessor
    seed(conn)
    apply(conn)
    version = 'audio-session-targets-1'
    with conn:
        conn.execute("INSERT INTO recordings(id,identity_key) VALUES (1,'recording')")
        conn.execute("INSERT INTO youtube_items(video_id,recording_id) VALUES ('video',1)")
        conn.execute("INSERT INTO audio_vibe_runs VALUES ('run','video','Title','Artist',1)")
        audio_annotations.save(conn, 'run', [], ['relax'], version, {'opaque': ['original prediction']}, 2)
        audio_annotations.save(conn, 'run', [], [], version, None, 3, state='withdrawn')
    original_paid = {key: value[1:] for key, value in references(conn).items()}
    original_annotations = {row[0]: audio_annotation_files.read(row[1]) for row in conn.execute(
        'SELECT id,model_output_filename FROM audio_annotations')}
    restored = sqlite3.connect(tmp_path / 'all-domains.db')
    try:
        with policy_documents.retained_reader(policy_documents.directory(conn)), conn:
            conn.execute('BEGIN')
            manifests = custody.export_files(conn, tmp_path / 'all-domains.db',
                                                       backup_root=tmp_path / 'backup')
            conn.backup(restored)
        assert set(manifests) == {'annotations', 'policies', 'descriptions', 'samples', 'choice_evidence'}
        # This historical fixture predates populated sample/choice owners; their
        # independent recovery cases exercise content after producer removal.
        assert list(sample_files._entries(manifests['samples'])) == []
        assert json.loads(choice_evidence_files._read(Path(manifests['choice_evidence']))) == {
            'format': 'choice-evidence-1', 'pages': []}
        for root in (files.directory(conn), audio_annotation_files.directory(conn), policy_documents.directory(conn)):
            shutil.rmtree(root)
        restored.execute('PRAGMA foreign_keys=ON')
        custody.restore_files(restored, manifests)
        assert {key: value[1:] for key, value in references(restored).items()} == original_paid
        assert policy_documents.configuration(restored, 'new-policy') == {'prompt_version': 1}
        assert {row[0]: audio_annotation_files.read(row[1]) for row in restored.execute(
            'SELECT id,model_output_filename FROM audio_annotations')} == original_annotations
        assert not restored.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        restored.close()


def test_interrupted_restore_publishes_no_database_paths_and_reuses_completed_files(
        predecessor, tmp_path, monkeypatch):
    conn = predecessor
    seed(conn)
    apply(conn)
    manifest, restored = backup(conn, tmp_path / 'backup', tmp_path / 'restored.db')
    try:
        before = list(restored.iterdump())
        original = references(conn)
        publish = files.publish
        count = 0
        def interrupted(*args, **kwargs):
            nonlocal count
            count += 1
            value = publish(*args, **kwargs)
            if count == 2:
                raise KeyboardInterrupt('controlled restore interruption')
            return value
        monkeypatch.setattr(files, 'publish', interrupted)
        with pytest.raises(KeyboardInterrupt, match='controlled restore interruption'):
            files.restore(restored, manifest)
        assert not restored.in_transaction
        assert list(restored.iterdump()) == before
        assert len(list(files.directory(restored).glob('*/*.json'))) == 2
        monkeypatch.setattr(files, 'publish', publish)
        files.restore(restored, manifest)
        assert {key: value[1:] for key, value in references(restored).items()} == {
            key: value[1:] for key, value in original.items()}
    finally:
        restored.close()


@pytest.mark.parametrize('problem', ['missing_owner', 'wrong_digest', 'path', 'duplicate', 'invalid_key', 'corrupt_file'])
def test_bad_backup_cannot_repoint_any_paid_owner(predecessor, tmp_path, problem):
    conn = predecessor
    seed(conn)
    apply(conn)
    manifest, restored = backup(conn, tmp_path / 'backup', tmp_path / 'restored.db')
    try:
        before = list(restored.iterdump())
        root = Path(manifest).parent.parent
        value = json.loads(files.read(manifest, Path(manifest).stem))
        digest = value['chunks'][0]
        chunk = json.loads(files.read(root / digest[:2] / (digest + '.json'), digest))
        entry = chunk['entries'][0]
        if problem == 'missing_owner':
            chunk['entries'].pop()
        elif problem == 'wrong_digest':
            entry['digest'] = 'a' * 64
            entry['relative_filename'] = 'aa/' + 'a' * 64 + '.json'
        elif problem == 'path':
            entry['relative_filename'] = '../outside.json'
        elif problem == 'duplicate':
            chunk['entries'].append(entry.copy())
        elif problem == 'invalid_key':
            entry['key'] = [[]]
        else:
            (root / entry['relative_filename']).write_bytes(b'corrupted retained evidence')
        chunk_digest = files.publish(root, json.dumps(chunk))[1]
        value['chunks'] = [chunk_digest]
        manifest = files.publish(root, json.dumps(value))[0]
        with pytest.raises(ValueError):
            files.restore(restored, manifest)
        assert not restored.in_transaction
        assert list(restored.iterdump()) == before
    finally:
        restored.close()


@pytest.fixture
def paid_store(file_backed_store):
    store = file_backed_store
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        store.conn.executemany('INSERT INTO albums(id,title,metadata_rank) VALUES (?,?,0)',
                               [(1, 'Same title'), (2, 'Same title')])
        store.conn.executemany('INSERT INTO album_provider_ids(provider,provider_id,album_id) '
                               "VALUES ('youtube',?,?)", [('release-one', 1), ('release-two', 2)])
    store._bind_repositories()
    return store


def test_paid_repository_uses_exact_album_identity_and_keeps_policy_and_admission_together(paid_store):
    store = paid_store
    repo = store.album_descriptions
    policy = {'model': 'controlled-model', 'prompt_version': 1}
    version = repo.policy_version(policy)
    assert repo.album_id({'browse_id': 'release-one'}) == 1
    assert repo.album_id({'album_id': 2}) == 2
    for ambiguous in ({'artist': 'Artist', 'title': 'Same title'}, {'album_id': True},
                      {'album_id': 99}, {'album_id': 2, 'browse_id': 'release-one'},
                      {'album_id': 1, 'browse_id': 'unknown-release'}):
        assert repo.album_id(ambiguous) is None
    assert repo.claim(1, policy, 'credential', 0, 100)['status'] == 'daily_limit'
    assert not store.conn.execute('SELECT 1 FROM recommendation_policies WHERE version=?', (version,)).fetchone()
    claim = repo.claim(1, policy, 'credential', 20, 100)
    assert claim['status'] == 'reserved'
    assert policy_documents.configuration(store.conn, version) == policy
    assert store.conn.execute('SELECT policy_version FROM album_description_requests').fetchone()[0] == version
    assert repo.requests_today(100) == 1
    assert repo.authorize(claim['request_id'], claim['token'], 'credential', 20, 101)
    assert repo.finish(claim['request_id'], claim['token'], ready_record(), 102)
    with store.conn:
        store.conn.execute('DELETE FROM rec_proposals')
    with store.read_view() as reader:
        assert reader.album_descriptions.cached(1, version) == ready_record()
        assert reader.album_descriptions.cached(2, version) is None
        assert reader.album_descriptions.cached(1, repo.policy_version({**policy, 'prompt_version': 2})) is None
        assert reader.album_descriptions.requests_today(102) == 1
    assert repo.claim(1, policy, 'replacement-key', 0, 103)['record'] == ready_record()


def test_paid_repository_cannot_leave_a_policy_row_when_admission_rolls_back(paid_store):
    store = paid_store
    policy = {'model': 'controlled-model', 'prompt_version': 2}
    with store.conn:
        store.conn.execute('CREATE TRIGGER reject_paid_admission BEFORE INSERT ON album_description_requests '
                           "BEGIN SELECT RAISE(ABORT,'controlled admission failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='controlled admission failure'):
        store.album_descriptions.claim(1, policy, 'credential', 20, 100)
    assert not store.conn.in_transaction
    assert not store.conn.execute('SELECT 1 FROM recommendation_policies').fetchone()
    assert not store.conn.execute('SELECT 1 FROM album_description_requests').fetchone()
    assert store.album_descriptions.requests_today(100) == 0
    assert policy_documents.collect_orphans(store.conn) == 1


def test_paid_repository_reads_legacy_evidence_without_resolving_its_owner(predecessor):
    conn = predecessor
    original = seed(conn)
    apply(conn)
    repo = requests.service(conn)
    assert repo.legacy_result('unresolved-hash') == json.loads(original['album_description:unresolved-hash'][0])
    assert repo.cached(1, 'new-policy') is None
    assert conn.execute('SELECT count(*) FROM album_description_requests WHERE album_id IS NOT NULL').fetchone()[0] == 0


def typed_transport(monkeypatch):
    from types import SimpleNamespace
    iao = DeepSeekIAO(pacer=SimpleNamespace(wait=lambda: None))
    monkeypatch.setattr(iao, 'api_key', lambda *args: 'controlled-credential')
    return iao


def test_typed_transport_authorizes_one_post_and_uses_only_the_paid_owner(paid_store, monkeypatch):
    store = paid_store
    iao = typed_transport(monkeypatch)
    policy = {'model': 'deepseek-flash', 'prompt_version': 1}
    posts = []
    def request(payload, key, **kwargs):
        assert not store.conn.in_transaction
        assert store.conn.execute('SELECT status FROM album_description_requests').fetchone()[0] == 'sent'
        posts.append(payload)
        return {'controlled': 'response'}
    monkeypatch.setattr(iao, '_request', request)
    def project(send):
        assert send({'messages': ['public album fields']}) == {'controlled': 'response'}
        with pytest.raises(RuntimeError, match='exactly one request'):
            send({'messages': ['second send must fail']})
        return ready_record()
    assert iao.complete(store, 1, project, 100, policy=policy) == ready_record()
    assert len(posts) == 1 and store.album_descriptions.requests_today(100) == 1
    assert not store.conn.execute("SELECT 1 FROM rec_proposals WHERE surface GLOB 'album_description*'").fetchone()
    assert iao.complete(store, 1, lambda send: pytest.fail('cached paid result was sent again'),
                        101, policy=policy) == ready_record()
    assert store.album_descriptions.cached(1, store.album_descriptions.policy_version(policy)) == ready_record()


def test_typed_transport_rechecks_settings_after_pacing_without_erasing_the_count(paid_store, monkeypatch):
    store = paid_store
    iao = typed_transport(monkeypatch)
    policy = {'model': 'deepseek-flash', 'prompt_version': 1}
    monkeypatch.setattr(iao.pacer, 'wait', lambda: store.settings.set_setting('deepseek_daily_limit', '0'))
    monkeypatch.setattr(iao, '_request', lambda *args, **kwargs: pytest.fail('disabled request was sent'))
    record = iao.complete(store, 1, lambda send: send({'public': 'album'}), 100, policy=policy)
    assert record['status'] == 'paused'
    assert store.album_descriptions.requests_today(100) == 1
    assert tuple(store.conn.execute('SELECT status,claim_token,authorized_at FROM album_description_requests').fetchone()) == (
        'paused', None, None)


@pytest.mark.parametrize('failure', ['timeout', 'cancelled'])
def test_typed_transport_uncertain_send_cannot_be_rebilled_after_lease_expiry(paid_store, monkeypatch, failure):
    store = paid_store
    iao = typed_transport(monkeypatch)
    policy = {'model': 'deepseek-flash', 'prompt_version': 1}
    def request(*args, **kwargs):
        if failure == 'cancelled':
            raise WorkClosing()
        raise DescriptionUnavailable('unavailable')
    monkeypatch.setattr(iao, '_request', request)
    project = lambda send: send({'public': 'album'})
    if failure == 'cancelled':
        with pytest.raises(WorkClosing):
            iao.complete(store, 1, project, 100, policy=policy)
    else:
        assert iao.complete(store, 1, project, 100, policy=policy)['status'] == 'unavailable'
    assert iao.complete(store, 1, lambda send: pytest.fail('uncertain attempt was sent again'),
                        1000, policy=policy)['status'] == 'uncertain'
    assert store.album_descriptions.requests_today(1000) == 1
    assert store.conn.execute('SELECT attempt_count FROM album_description_requests').fetchone()[0] == 1


def test_typed_album_copy_keeps_paid_success_after_policy_change_and_disable(paid_store, monkeypatch):
    store = paid_store
    iao = typed_transport(monkeypatch)
    monkeypatch.setattr(deepseek, '_iao', iao)
    sent = []
    def describe(album, send):
        sent.append(album)
        send({'public': deepseek.album_fields(album)})
        return {key: value for key, value in ready_record().items() if key != 'status'}
    monkeypatch.setattr(deepseek, 'describe', describe)
    monkeypatch.setattr(iao, '_request', lambda *args, **kwargs: {})
    album = {'browse_id': 'release-one', 'artist': 'Artist', 'title': 'Same title', 'year': '2001'}
    text, record = album_descriptions.get(store, album, 100)
    assert text['text'] == ready_record()['description'] and record['status'] == 'ready'
    assert len(sent) == 1
    with store.conn:
        store.conn.execute('DELETE FROM rec_proposals')
    store.settings.set_values({'deepseek_album_descriptions_enabled': '0', 'deepseek_daily_limit': '0'})
    monkeypatch.setattr(deepseek, 'PROMPT_VERSION', 2)
    assert album_descriptions.read(store, album) == text
    assert album_descriptions.get(store, album, 101)[0] == text
    assert len(sent) == 1 and store.album_descriptions.requests_today(101) == 1
    assert album_descriptions.read(store, {**album, 'browse_id': 'release-two'}) is None
    assert album_descriptions.get(store, {'artist': 'Artist', 'title': 'Same title'}, 102) == (
        None, {'status': 'unresolved_album'})


def test_installed_paid_owner_routes_default_settings_and_reader_without_legacy_paid_surfaces(paid_store, monkeypatch):
    store = paid_store
    iao = typed_transport(monkeypatch)
    monkeypatch.setattr(deepseek, '_iao', iao)
    original = type(store.rec_proposals).get_proposals
    def read(self, surface):
        if surface.startswith(('album_description:', 'album_description_usage', 'deepseek_')):
            pytest.fail('Installed paid owner read a retired legacy surface')
        return original(self, surface)
    monkeypatch.setattr(type(store.rec_proposals), 'get_proposals', read)
    claim = store.album_descriptions.claim(1, deepseek.description_policy(), 'controlled', 20, 100)
    assert claim['status'] == 'reserved'
    assert album_descriptions.settings(store, 100)['requests_today'] == 1
    assert deepseek.settings(store, 100)['requests_today'] == 1
    assert album_descriptions.read(store, {'browse_id': 'release-one'}) is None
    # Settings wake only the display worker; durable admission and usage survive.
    store.album_notes.wake(101)
    assert store.album_descriptions.requests_today(101) == 1
    assert tuple(store.conn.execute('SELECT claim_token,status FROM album_description_requests').fetchone()) == (
        claim['token'], 'claimed')
    with pytest.raises(ValueError, match='generation policy'):
        iao.complete(store, 'legacy-fingerprint', lambda send: pytest.fail('legacy admission sent'), 101)


def test_paid_owner_detection_survives_reopen_without_a_typed_caller_override(paid_store):
    store = paid_store
    path = store.conn.execute('PRAGMA database_list').fetchone()[2]
    reader = Store(path)
    try:
        reader._bind_repositories()
        assert reader._paid_descriptions
        assert album_descriptions.read(reader, {'browse_id': 'release-one'}) is None
    finally:
        reader.conn.close()


def test_typed_album_copy_preserves_legacy_unknown_retry_without_assigning_it(predecessor, monkeypatch):
    conn = predecessor
    album = {'browse_id': 'legacy-alias', 'artist': 'Artist', 'title': 'Same title'}
    original = {'status': 'unknown', 'description': None, 'confidence': 'low',
                'model': 'deepseek-flash', 'retry_at': 1000, 'at': 10}
    with conn:
        conn.execute('INSERT INTO rec_proposals VALUES (?,?,10)',
                     ('album_description:' + album_descriptions.identity(album), json.dumps(original)))
        conn.execute("INSERT INTO album_provider_ids(provider,provider_id,album_id) VALUES ('youtube','legacy-alias',1)")
    apply(conn)
    store = Store(conn.execute('PRAGMA database_list').fetchone()[2])
    store._bind_repositories()
    try:
        monkeypatch.setattr(deepseek, 'generate', lambda *args, **kwargs: pytest.fail('unexpired abstention was rebilled'))
        assert album_descriptions.get(store, album, 100, typed=True) == (None, original)
        assert not store.conn.execute('SELECT 1 FROM album_description_requests WHERE album_id IS NOT NULL').fetchone()
        assert store.album_descriptions.requests_today(100) == 0
    finally:
        store.conn.close()


@pytest.mark.parametrize('state', ['ready', 'sent'])
def test_verified_album_reconciliation_preserves_paid_owner_and_cannot_start_an_equivalent_request(paid_store, state):
    store = paid_store
    owner = store.album_descriptions
    policy = {'model': 'deepseek-flash', 'prompt_version': 1}
    version = owner.policy_version(policy)
    claim = owner.claim(2, policy, 'credential', 20, 100)
    assert owner.authorize(claim['request_id'], claim['token'], 'credential', 20, 101)
    if state == 'ready':
        assert owner.finish(claim['request_id'], claim['token'], ready_record(), 102)
    release = {'title': 'Same title', 'year': '2001', 'artists': [{'name': 'Artist'}], 'trackCount': 1,
               'tracks': [{'videoId': 'controlled-upload', 'title': 'Song', 'duration': '3:01', 'isExplicit': False}]}
    with store.conn:
        assert album_aliases.observe(store.conn, 'release-one', release, now=110)
        assert album_aliases.observe(store.conn, 'release-two', release, now=111)
    assert owner.album_id({'browse_id': 'release-two'}) == 1
    assert owner.album_id({'album_id': 2, 'browse_id': 'release-one'}) == 1
    assert store.conn.execute('SELECT album_id FROM album_description_requests').fetchone()[0] == 2
    again = owner.claim(1, policy, 'credential', 20, 162)
    assert again['status'] == ('cached' if state == 'ready' else 'uncertain')
    assert owner.requests_today(162) == 1
    if state == 'sent':
        assert owner.finish(claim['request_id'], claim['token'], ready_record(), 163)
    assert owner.successful_result(1) == ready_record()
    assert owner.cached(1, version) == ready_record()
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    plan = ' '.join(row[3] for row in store.conn.execute('EXPLAIN QUERY PLAN SELECT id '
        'FROM album_description_requests WHERE policy_version=? AND album_id IN '
        '(SELECT ? UNION ALL SELECT retired_id FROM album_alias_reconciliations WHERE album_id=?)',
        (version, 1, 1)))
    assert 'ix_album_reconciliation_canonical' in plan
    assert 'SCAN album_description_requests' not in plan
