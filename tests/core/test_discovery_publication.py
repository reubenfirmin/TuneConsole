"""Canonical visits, source folds and retained offer precision share one selection."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading

import pytest

from tests.core.test_predecessor_writes import installed
from tests.core.test_discovery_owner_preparation import seed
from tests.core.test_discovery_artist_publication import ready as artist_ready
from yt_playlist.core import artists, collection_recovery
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import discovery_publication as publication, discovery_subjects as preparation
from yt_playlist.core.store import Store


def prepare(conn):
    owner = publication.owner(conn)
    artifact, stage = artist_ready(conn)
    artifact.publish(conn, stage=stage)
    for job in owner.jobs:
        job.install(conn)
        for _ in range(500):
            if job.step(conn, batch_size=16).status == 'prepared':
                break
        else:
            pytest.fail('Discovery subject fixture did not prepare')
    return owner


def ready(conn, size=16):
    owner = prepare(conn)
    for _ in range(500):
        stage = owner.step(conn, batch_size=size)
        if stage.complete:
            return owner, stage.id
    pytest.fail('Discovery subject fixture did not stage')


def read(conn, owner, stage, name):
    return [dict(row) for row in conn.execute('SELECT * FROM '+owner.table(name)+' WHERE stage=?', (stage,))]


@pytest.mark.parametrize('installed', [79, 85, 86, 100], indirect=True)
def test_views_deduplicate_canonical_visits_and_preserve_legacy_residue_and_origins(installed):
    store, version = installed
    account, aid, rid, album = seed(store)
    with store.conn:
        # Two retained spellings of one visible artist visit count once. Keep
        # the earliest observation even when its captured spelling sorts later.
        store.conn.execute("INSERT INTO main.discovery_visible_impressions VALUES ('visit','artist',?,2)", ('0'+str(aid),))
        store.conn.execute("INSERT INTO main.recording_aliases VALUES ('song alias',?,'captured')", (rid,))
        store.conn.execute("INSERT INTO main.discovery_visible_impressions VALUES ('visit','track','song alias',1)")
        store.conn.execute('UPDATE main.album_discovery_memberships SET offered_count=5,first_shown=0,last_shown=1')
    original = {table: list(map(tuple, store.conn.execute('SELECT * FROM main.'+table))) for table in
        (*preparation.KEYS, 'discovered_artists', 'schema_migrations')}
    owner, stage = ready(store.conn, size=1)
    owner.publish(store.conn, stage=stage)
    visits = read(store.conn, owner, stage, 'discovery_impressions')
    assert sorted(((r['view_id'], r['recording_id'], r['artist_id'], r['shown_at']) for r in visits),
        key=lambda row: (row[0], row[1] is not None)) == [
        ('other', None, aid, 4), ('visit', None, aid, 2), ('visit', rid, None, 1)]
    unresolved = read(store.conn, owner, stage, 'unresolved_discovery_impressions')
    assert sorted((r['original_key'], r['reason']) for r in unresolved) == [('absent', 'missing_owner'), ('unknown', 'unsupported_kind')]
    candidate, = read(store.conn, owner, stage, 'discovered_artists')
    assert (candidate['artist_id'], candidate['offered_count'], candidate['first_shown'], candidate['last_shown']) == (aid, 2, 2, 4)
    assert (candidate['legacy_offered_count'], candidate['legacy_first_shown'], candidate['legacy_last_shown']) == (14, 0, 4)
    assert collection_recovery.read(candidate['recovery_filename'], candidate['recovery_digest'], candidate['recovery_bytes'],
        kind='discovery-candidate', owner=aid) == {'because': ['Anchor', 'Anchor'], 'fits': ['Playlist']}
    release, = read(store.conn, owner, stage, 'album_discovery_memberships')
    assert (release['album_id'], release['offered_count'], release['first_shown'], release['legacy_offered_count']) == (album, 0, None, 5)
    track, = read(store.conn, owner, stage, 'discovered_tracks')
    assert (track['recording_id'], track['offered_count'], track['first_shown'], track['source_browse_id']) == (rid, 1, 1, 'radio:song')
    imported = [row for row in read(store.conn, owner, stage, 'discovery_origins') if row['kind'] == 'spotify_export']
    assert [(row['account_id'], row['import_key'], row['resolution_status']) for row in imported] == [(account, None, 'legacy_account')]
    assert next(row for row in read(store.conn, owner, stage, 'discovery_origins') if row['kind'] == 'legacy_unknown')['legacy_source_key'] == 'artist:missing-provider'
    for table, rows in original.items():
        assert list(map(tuple, store.conn.execute('SELECT * FROM main.'+table))) == rows
    assert owner.step(store.conn).id == stage
    owner.publish(store.conn, stage=stage)
    assert store.conn.execute('SELECT count(*) FROM '+owner.table('generations')).fetchone()[0] == 1
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_unknown_artists_stay_explicit_and_classification_custody_is_complete(installed):
    store, _ = installed
    _, aid, _, _ = seed(store)
    with store.conn:
        store.conn.execute("INSERT INTO main.artist_scans(artist,artist_id,scanned_at) VALUES ('missing',NULL,1)")
        store.conn.execute("INSERT INTO main.discovered_artists(artist,artist_id,because,fits) VALUES ('missing',NULL,'malformed','[]')")
        store.conn.execute("INSERT INTO main.genre_classification_legacy(artist_id,raw_label,resolution_reason) VALUES (?,'Unknown captured','unknown_genre')", (aid,))
    owner, stage = ready(store.conn)
    owner.publish(store.conn, stage=stage)
    assert len(read(store.conn, owner, stage, 'discovered_artists')) == 1
    captured = read(store.conn, owner, stage, 'input_artists')
    assert any(row['artist_id'] is None and row['artist_resolution'] == 'missing_canonical_artist' and row['explanation_status'] == 'malformed' for row in captured)
    assert len(read(store.conn, owner, stage, 'input_scans')) == 2
    assert len(read(store.conn, owner, stage, 'artist_scans')) == 1
    retained = read(store.conn, owner, stage, 'input_classifications')
    assert retained and json.loads(retained[0]['source_snapshot'])['raw_label'] == 'Unknown captured'
    # The fixture also validates its installed format at teardown. Restore the
    # deliberately unresolved owners after proving they were retained explicitly.
    with store.conn:
        store.conn.execute("DELETE FROM main.artist_scans WHERE artist='missing'")
        store.conn.execute("DELETE FROM main.discovered_artists WHERE artist='missing'")


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_representative_mismatch_fails_instead_of_silently_losing_track(installed):
    store, _ = installed
    seed(store)
    original = store.conn.execute('SELECT entry_id FROM main.discovery_memberships').fetchone()[0]
    other = store.tracks.upsert_track('other', 'Other', 'Other', None, 1)
    with store.conn:
        guard = store.conn.execute("SELECT sql FROM main.sqlite_schema WHERE name='discovery_membership_update'").fetchone()[0]
        # Model pre-existing damage; supported writers already reject this.
        store.conn.execute('DROP TRIGGER discovery_membership_update')
        store.conn.execute('UPDATE main.discovery_memberships SET entry_id=?', (other,))
        store.conn.execute(guard)
    owner, stage = ready(store.conn)
    with pytest.raises(MigrationError, match='representative'):
        owner.publish(store.conn, stage=stage)
    assert owner.selected(store.conn) is None
    with store.conn:
        store.conn.execute('UPDATE main.discovery_memberships SET entry_id=?', (original,))


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_origin_aliases_fold_and_keep_exact_contributions(installed):
    store, _ = installed
    seed(store)
    store.discovery.select_starter_artist('Starter', 'UC-first', None, True, 1)
    aid = store.discovery.starter_artists()[0]['artist_id']
    with store.conn:
        store.conn.execute("INSERT INTO main.artist_provider_ids VALUES ('youtube','UC-second',?)", (aid,))
        store.conn.execute("INSERT INTO main.discovery_artist_sources VALUES (?,'artist_start:UC-second',NULL,5)", (aid,))
    owner, stage = ready(store.conn)
    owner.publish(store.conn, stage=stage)
    starts = [row for row in read(store.conn, owner, stage, 'discovery_origins') if row['kind'] == 'artist_start']
    assert len(starts) == 1 and starts[0]['artist_id'] == aid
    folded = [row for row in read(store.conn, owner, stage, 'discovery_artist_sources') if row['artist_id'] == aid]
    assert len(folded) == 1 and folded[0]['score'] == 5
    retained = [row for row in read(store.conn, owner, stage, 'input_artist_sources') if row['subject_artist_id'] == aid]
    assert len(retained) == 2
    assert {json.loads(row['source_snapshot'])['source'] for row in retained} == {'artist_start:UC-first', 'artist_start:UC-second'}


@pytest.mark.parametrize('installed', [79, 93], indirect=True)
def test_selected_readers_use_one_generation_and_writes_reconcile_without_losing_visits(installed):
    store, _ = installed
    _, aid, _, _ = seed(store)
    other = Store(store._db_path)
    try:
        other.init_schema(defer_metadata=True)
        with other.read_view() as pinned:
            assert pinned.discovery.get_discovered_artists()[0]['offered_count'] == 9
            owner, stage = ready(store.conn)
            owner.publish(store.conn, stage=stage)
            assert pinned.discovery.get_discovered_artists()[0]['offered_count'] == 9
        with other.read_view() as selected:
            old = {'discovered_artists', 'artist_scans', *preparation.KEYS}
            def deny_old(action, table, column, database, view):
                return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table in old else sqlite3.SQLITE_OK
            selected.conn.set_authorizer(deny_old)
            try:
                candidate, = selected.discovery.get_discovered_artists()
                assert (candidate['artist_id'], candidate['offered_count'], candidate['first_shown']) == (aid, 2, 3)
                assert candidate['because'] == ['Anchor', 'Anchor']
                assert selected.discovery.discovery_artist_view(10, 7)[0]['legacy_offered_count'] == 14
                assert selected.discovery.discovery_album_view(10, 7)[0]['first_shown'] is None
                assert selected.discovery.get_discovered_tracks()[0]['offered_count'] == 1
                assert selected.discovery.discovery_track_view(10, 7)[0]['offered_count'] == 1
                assert selected.discovery.discovered_tracks_by_keys(['song|artist'])['song|artist']['offered_count'] == 1
                assert selected.discovery.identity_keys_for_video('song') == ['song|artist']
                assert selected.discovery.discovery_sources_for_track('song|artist')[0]['source_token'] == 'radio:song'
                assert selected.discovery.discovered_source_tokens('radio') == {'radio:song'}
                assert selected.discovery.present_home_tiles(['release'], [aid])[1][aid]['artist_id'] == aid
                assert selected.discovery.starter_artists() == []
                assert selected.discovery.starter_tracks() == []
                selected.discovery.artists_due_for_scan(10)
            finally:
                selected.conn.set_authorizer(None)
        regular.migrate(other.conn)
        store.discovery.record_discovery_impressions('artist', ['@artist:'+str(aid)], 'new-visit', 8)
        assert owner.selected(store.conn) is None
        assert other.discovery.get_discovered_artists()[0]['offered_count'] == 10
        successor, newer = ready(store.conn)
        assert newer != stage
        successor.publish(store.conn, stage=newer)
        candidate, = other.discovery.get_discovered_artists()
        assert (candidate['offered_count'], candidate['first_shown'], candidate['last_shown']) == (3, 3, 8)
        assert other.discovery.discovery_artist_view(10, 7)[0]['legacy_offered_count'] == 14
        assert len(read(store.conn, owner, stage, 'discovery_impressions')) == 3
        assert len(read(store.conn, owner, newer, 'discovery_impressions')) == 4
    finally:
        other.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_cleanup_uses_verified_expiry_and_keeps_unverified_album_clock(installed):
    store, _ = installed
    seed(store)
    with store.conn:
        store.conn.execute('UPDATE main.album_discovery_memberships SET offered_count=5,first_shown=0,last_shown=1')
    owner, stage = ready(store.conn)
    owner.publish(store.conn, stage=stage)
    result = store.discovery.gc_discovery_pool(8*86400, 7, [])
    assert result == {'albums': 0, 'artists': 1, 'tracks': 1}
    assert store.conn.execute('SELECT count(*) FROM main.album_discovery_memberships').fetchone()[0] == 1
    assert not store.conn.execute('SELECT 1 FROM main.discovered_artists').fetchone()
    assert not store.conn.execute('SELECT 1 FROM main.discovery_memberships').fetchone()
    assert owner.selected(store.conn) is None
    # Removing the candidate never removes the retained publication or receipt.
    assert read(store.conn, owner, stage, 'discovered_artists')[0]['offered_count'] == 2
    assert store.conn.execute('SELECT count(*) FROM main.discovery_visible_impressions').fetchone()[0] == 5


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_canonical_artist_collision_preserves_first_metadata_last_explanation_and_each_raw_owner(installed):
    store, _ = installed
    _, previous, _, _ = seed(store)
    target = artists.ensure(store.conn, 'Canonical', provider='youtube', provider_id='UC-canonical')
    with store.conn:
        store.conn.execute("INSERT INTO main.artist_redirects(retired_id,artist_id,source,created_at) VALUES (?,?,'controlled predecessor',1)", (previous, target))
        store.conn.execute("UPDATE main.discovered_artists SET thumbnail='first.jpg',genre_key=NULL,unresolved_genre='First captured'")
        store.conn.execute('INSERT INTO main.discovered_artists(artist,artist_id,score,because,fits,thumbnail,found_at,offered_count) '
            "VALUES ('second',?,8,'[\"Last anchor\"]','[]','last.jpg',2,6)", (target,))
    owner, stage = ready(store.conn)
    owner.publish(store.conn, stage=stage)
    candidate, = store.discovery.get_discovered_artists()
    assert (candidate['artist_id'], candidate['artist'], candidate['score'], candidate['thumbnail'], candidate['genre'], candidate['found_at']) == (
        target, 'Canonical', 8, 'first.jpg', 'First captured', 1)
    assert candidate['because'] == ['Last anchor']
    assert candidate['offered_count'] == 2
    assert store.discovery.discovery_artist_view(10, 7)[0]['legacy_offered_count'] == 18
    assert {row['captured_artist_id'] for row in read(store.conn, owner, stage, 'input_artists')} == {previous, target}
    assert store.conn.execute('SELECT count(*) FROM main.discovered_artists').fetchone()[0] == 2
    assert store.discovery.gc_discovery_pool(8*86400, 7, [])['artists'] == 1
    assert not store.conn.execute('SELECT 1 FROM main.discovered_artists').fetchone()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('damage', ['clock', 'identity', 'original', 'missing', 'classification'])
def test_independent_publication_check_rejects_damaged_preparation(installed, damage):
    store, _ = installed
    _, aid, _, _ = seed(store)
    with store.conn:
        store.conn.execute("INSERT INTO main.genre_classification_legacy(artist_id,raw_label,resolution_reason) VALUES (?,'Captured','unknown_genre')", (aid,))
    owner = prepare(store.conn)
    with store.conn:
        if damage == 'clock':
            store.conn.execute('UPDATE '+preparation.SOURCES+' SET first_found_at=999')
        elif damage == 'identity':
            store.conn.execute('UPDATE '+preparation.IMPRESSIONS+' SET artist_id=999 WHERE artist_id IS NOT NULL')
        elif damage == 'original':
            store.conn.execute('UPDATE '+preparation.ORIGINALS+" SET snapshot=json_set(snapshot,'$.scanned_at',999) WHERE source_owner='artist_scans'")
        elif damage == 'missing':
            store.conn.execute('DELETE FROM '+preparation.SOURCES)
        else:
            store.conn.execute('UPDATE '+preparation.CLASSIFICATIONS+" SET raw_label='Altered'")
    for _ in range(20):
        stage = owner.step(store.conn)
        if stage.complete:
            break
    assert stage.complete
    with pytest.raises(MigrationError, match='differs|omits'):
        owner.publish(store.conn, stage=stage.id)
    assert owner.selected(store.conn) is None
    assert not store.conn.execute('SELECT 1 FROM '+owner.table('generations')).fetchone()
    assert store.discovery.get_discovered_artists()[0]['offered_count'] == 9
    regular.migrate(store.conn)


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_interrupted_stage_and_head_resume_without_partial_selection_or_stale_publish(installed, monkeypatch):
    store, _ = installed
    seed(store)
    owner = prepare(store.conn)
    first = owner.step(store.conn, batch_size=1)
    assert first.owner == 'sources' and first.copied == 1 and not first.complete
    with store.conn:
        store.conn.execute('CREATE TRIGGER controlled_subject_stage_failure BEFORE INSERT ON '+owner.table('input_sources')+
            " BEGIN SELECT RAISE(ABORT,'controlled discovery interruption'); END")
    with pytest.raises(sqlite3.IntegrityError, match='controlled discovery interruption'):
        owner.step(store.conn, batch_size=1)
    assert owner.stage(store.conn, first.id) == first
    regular.migrate(store.conn)
    with store.conn:
        store.conn.execute('DROP TRIGGER controlled_subject_stage_failure')
    reopened = Store(store._db_path)
    try:
        for _ in range(30):
            stage = owner.step(reopened.conn, batch_size=1)
            if stage.complete:
                break
        assert stage.complete and stage.id == first.id
        with reopened.conn:
            reopened.conn.execute('CREATE TRIGGER controlled_subject_head_failure BEFORE INSERT ON '+owner.table('head')+
                " BEGIN SELECT RAISE(ABORT,'controlled head interruption'); END")
        with pytest.raises(sqlite3.IntegrityError, match='controlled head interruption'):
            owner.publish(reopened.conn, stage=stage.id)
        assert not reopened.conn.execute('SELECT 1 FROM '+owner.table('generations')).fetchone()
        with reopened.conn:
            reopened.conn.execute('DROP TRIGGER controlled_subject_head_failure')
        verify = type(owner).verify
        def changed(self, conn, identity):
            result = verify(self, conn, identity)
            with sqlite3.connect(store._db_path) as writer:
                writer.execute('UPDATE main.discovery_sources SET last_found_at=20')
            return result
        monkeypatch.setattr(type(owner), 'verify', changed)
        with pytest.raises(MigrationError, match='reconciled|changed'):
            owner.publish(reopened.conn, stage=stage.id)
        assert owner.selected(reopened.conn) is None
        monkeypatch.setattr(type(owner), 'verify', verify)
        owner, later = ready(reopened.conn)
        assert later != stage.id
        owner.publish(reopened.conn, stage=later)
        assert read(reopened.conn, owner, later, 'discovery_sources')[0]['last_found_at'] == 20
        with pytest.raises(sqlite3.IntegrityError, match='[Rr]etained'):
            with reopened.conn:
                reopened.conn.execute('DELETE FROM '+owner.table('input_sources')+' WHERE stage=?', (stage.id,))
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_slow_verification_leaves_real_writes_and_regular_migrations_available(installed, monkeypatch):
    store, _ = installed
    _, aid, _, _ = seed(store)
    owner, stage = ready(store.conn)
    entered, release = threading.Event(), threading.Event()
    original = collection_recovery.read
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(15)
        return original(*args, **kwargs)
    monkeypatch.setattr(collection_recovery, 'read', slow)
    def publish():
        with sqlite3.connect(store._db_path, timeout=.1) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            with pytest.raises(MigrationError, match='reconciled|changed'):
                owner.publish(conn, stage=stage)
    with ThreadPoolExecutor() as pool:
        task = pool.submit(publish)
        try:
            assert entered.wait(15)
            store.discovery.record_discovery_impressions('artist', ['@artist:'+str(aid)], 'actual-writer', 9)
            regular.migrate(store.conn)
        finally:
            release.set()
        task.result(timeout=15)
    assert owner.selected(store.conn) is None
    assert store.discovery.get_discovered_artists()[0]['offered_count'] == 10


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_repeated_stage_schema_validation_does_not_reserve_writer(installed, monkeypatch):
    store, _ = installed
    _, aid, _, _ = seed(store)
    owner, stage = ready(store.conn)
    entered, release = threading.Event(), threading.Event()
    validate, worker_thread = type(owner).validate, None
    def slow(self, conn):
        result = validate(self, conn)
        if threading.get_ident() == worker_thread:
            entered.set()
            assert release.wait(15)
        return result
    monkeypatch.setattr(type(owner), 'validate', slow)
    def step():
        nonlocal worker_thread
        worker_thread = threading.get_ident()
        with sqlite3.connect(store._db_path, timeout=.1) as conn:
            conn.row_factory = sqlite3.Row
            with pytest.raises(MigrationError, match='reconciled|selected'):
                owner.step(conn)
    with ThreadPoolExecutor() as pool:
        task = pool.submit(step)
        try:
            assert entered.wait(15)
            # WAL permits both of these writers while schema inspection owns
            # only a reader. The previous BEGIN IMMEDIATE blocked them here.
            store.discovery.record_discovery_impressions('artist', ['@artist:'+str(aid)], 'during-schema-check', 9)
            regular.migrate(store.conn)
        finally:
            release.set()
        task.result(timeout=15)


@pytest.mark.parametrize('installed', [100], indirect=True)
def test_frozen_native_upgrade_keeps_retained_inputs_and_prefers_native_readers(installed, tmp_path):
    store, _ = installed
    seed(store)
    owner, stage = ready(store.conn)
    owner.publish(store.conn, stage=stage)
    expected = store.discovery.get_discovered_artists()
    retained = {kind: list(map(tuple, store.conn.execute('SELECT * FROM '+owner.table('input_'+kind)))) for kind in owner.kinds}
    native = Store(tmp_path/'native-discovery.db')
    try:
        store.conn.backup(native.conn)
        runner.migrate(native.conn, registry=registry.MIGRATIONS)
        regular.migrate(native.conn)
        native.init_schema(defer_metadata=True)
        assert owner.selected(native.conn) is None
        assert native.discovery.get_discovered_artists() == expected
        for kind, rows in retained.items():
            assert list(map(tuple, native.conn.execute('SELECT * FROM '+owner.table('input_'+kind)))) == rows
        assert not native.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        native.conn.close()


@pytest.mark.parametrize('installed', [79], indirect=True)
@pytest.mark.parametrize('version', [86, 100])
def test_partly_upgraded_genre_layout_admits_earlier_retained_publication(installed, version, tmp_path):
    store, _ = installed
    seed(store)
    owner, stage = ready(store.conn)
    owner.publish(store.conn, stage=stage)
    partial = Store(tmp_path/'partial-discovery.db')
    try:
        store.conn.backup(partial.conn)
        runner.migrate(partial.conn, registry=registry.MIGRATIONS[:version])
        partial.init_schema(defer_metadata=True)
        regular.migrate(partial.conn)
        assert publication.read_owners(partial.conn).revision is None
        assert partial.discovery.get_discovered_artists()[0]['offered_count'] == 9
        assert partial.conn.execute('SELECT copied FROM '+owner.table('stages')+' WHERE id=?', (stage,)).fetchone()[0] > 0
        assert partial.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == version
    finally:
        partial.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_listening_upgrade_preserves_selected_discovery_views_until_native_takeover(installed, tmp_path):
    store, _ = installed
    seed(store)
    owner, stage = ready(store.conn)
    owner.publish(store.conn, stage=stage)
    expected = store.discovery.get_discovered_artists()
    partial = Store(tmp_path/'listening-upgrade.db')
    try:
        store.conn.backup(partial.conn)
        runner.migrate(partial.conn, registry=registry.MIGRATIONS[:100])
        partial.init_schema(defer_metadata=True)
        regular.migrate(partial.conn)
        assert publication.read_owners(partial.conn).revision == stage
        assert partial.discovery.get_discovered_artists() == expected
        assert partial.discovery.discovery_album_view(10, 7)[0]['first_shown'] is None
        assert partial.discovery.get_discovered_tracks()[0]['offered_count'] == 1
    finally:
        partial.conn.close()
