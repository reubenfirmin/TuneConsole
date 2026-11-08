"""Canonical visits, source folds and retained offer precision share one selection."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import threading

import pytest

from tests.core.test_predecessor_writes import consumer_installed as installed
from tests.independent_schema_support import independent_regular_ddl
from tests.core.test_discovery_owner_preparation import seed
from tests.core.test_discovery_artist_publication import ready as artist_ready
from tests.genre_catalog_support import bound_catalog
from tests.prepared_fixture_support import assert_upgrade_phase, custody_predecessor
from tests.store_fixture_support import publication_consumer_reads, reuse_reporting_reader
from yt_playlist.core import schema_inspection, artists, collection_recovery, preparation_files
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import discovery_publication as publication, discovery_subjects as preparation
from yt_playlist.core.store import Store


pytestmark = pytest.mark.usefixtures('publication_consumer_reads')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Publication owners do not construct an HTTP application."""


def prepare(conn):
    # These are completed lower inputs for publication. Reuse the production
    # table-inspection session while real workers build them; all validators,
    # source copying and file writes still run.
    with schema_inspection.reuse_tables(conn):
        artifact, stage = artist_ready(conn)
        artifact.publish(conn, stage=stage)
        return prepare_subjects(conn)


def prepare_subjects(conn, *, installed=False):
    with schema_inspection.reuse_tables(conn):
        owner = publication.owner(conn)
        for job in owner.jobs:
            if not installed:
                job.install(conn)
            elif job.progress(conn).status == 'prepared':
                continue
            for _ in range(500):
                if job.step(conn, batch_size=16).status == 'prepared':
                    break
            else:
                pytest.fail('Discovery subject fixture did not prepare')
        return owner


def ready(conn, size=16):
    prepare(conn)
    return stage_discovery(conn, size=size)


def stage_discovery(conn, size=16):
    owner = publication.owner(conn)
    for _ in range(500):
        stage = owner.step(conn, batch_size=size)
        if stage.complete:
            return owner, stage.id
    pytest.fail('Discovery subject fixture did not stage')


@pytest.mark.parametrize('version', [79, 85, 86, 93, 94, 100])
def test_discovery_schema_input_keeps_history_and_real_private_guards(version, _bounded_installed_schema):
    template = _bounded_installed_schema(version)
    store = template.source
    assert template.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == version
    assert store._snapshot_revisions.conn is store.conn
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('version', [79, 85, 86, 93, 94, 100])
def test_discovery_seed_input_keeps_exact_catalog_owners_and_legacy_visits(version, discovery_seed_inputs):
    template = discovery_seed_inputs(version)
    source = template.source
    account, aid, rid, album = template.result
    assert account and aid and rid and album
    assert source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert source.conn.execute('SELECT count(*) FROM main.discovery_visible_impressions').fetchone()[0] == 5
    assert source.conn.execute('SELECT offered_count FROM main.discovered_artists').fetchone()[0] == 9
    assert not source.conn.execute('PRAGMA foreign_key_check').fetchone()


def retained_discovery_rows(conn, table):
    return [{name: Path(value).read_bytes() if name.endswith('filename') and value is not None else value
             for name, value in dict(row).items()} for row in conn.execute('SELECT * FROM ' + table)]


def prepare_selected_discovery(store):
    with pytest.MonkeyPatch.context() as patch, reuse_reporting_reader(store, patch), schema_inspection.reuse_tables(store.conn):
        owner, stage = stage_discovery(store.conn)
        owner.publish(store.conn, stage=stage)
        captured = {owner.table('input_' + kind): retained_discovery_rows(store.conn, owner.table('input_' + kind))
                    for kind in owner.kinds}
        return stage, store.discovery.get_discovered_artists(), owner.table('stages'), captured


@pytest.fixture(scope='module')
def discovery_seed_inputs(staged_custody_templates, _bounded_installed_schema):
    def get(version):
        return staged_custody_templates(version, 'discovery-subject-seeds', seed, admit=False,
                                       completed=_bounded_installed_schema(version))
    return get


@pytest.fixture(scope='module')
def discovery_prepared_inputs(staged_custody_templates, discovery_seed_inputs):
    def artists(store):
        owner, stage = artist_ready(store.conn)
        owner.publish(store.conn, stage=stage)

    def subjects(store):
        prepare_subjects(store.conn)

    builders = {'artists': artists, 'subjects': subjects}
    inputs = {}

    def get(version, phase='subjects'):
        completed = discovery_seed_inputs(version)
        for name, build in builders.items():
            completed = staged_custody_templates(version, 'discovery-prepared:' + name, build,
                                                admit=False, completed=completed)
            inputs[version, name] = completed
            if name == phase:
                return completed
        raise ValueError(phase)

    def release(version):
        sources = []
        for key, template in inputs.items():
            if key[0] == version:
                assert not template.source.conn.in_transaction
                template.source._fixture_released = True
                template.source.conn.close()
                sources.append(template.source)
        return sources

    get.release = release
    return get


@pytest.mark.parametrize('phase,version', [
    ('artists', 79), ('artists', 93), ('subjects', 79), ('subjects', 93)])
def test_discovery_prepared_input_keeps_real_files_and_cold_phase(discovery_prepared_inputs, version, phase):
    template = discovery_prepared_inputs(version, phase)
    assert_upgrade_phase(template, version)
    assert template.source.discovery.get_discovered_artists()[0]['offered_count'] == 9
    references = tuple(preparation_files.references(template.source.conn))
    assert references
    assert all(preparation_files._read(item['reference']) for item in references)


@pytest.fixture(scope='module')
def discovery_upgrade_inputs(upgraded_custody_inputs, discovery_prepared_inputs):
    def get(version, target, **options):
        return upgraded_custody_inputs(version, target, 'selected-discovery-upgrade',
            prepare_selected_discovery, prepared=discovery_prepared_inputs(version), **options)

    get.release = lambda version: upgraded_custody_inputs.release(version, 'selected-discovery-upgrade')
    return get


@pytest.fixture
def prepared_discovery_store(version, discovery_prepared_inputs, tmp_path, monkeypatch):
    with discovery_prepared_inputs(version).open(tmp_path / 'prepared-discovery.db') as store:
        with reuse_reporting_reader(store, monkeypatch), schema_inspection.reuse_tables(store.conn):
            yield store
        with closing(sqlite3.connect(store._db_path.as_uri() + '?mode=ro', uri=True)) as reader:
            reader.row_factory = sqlite3.Row
            runner.validate_version(reader, version)


@pytest.fixture
def canonical_discovery_store(version, canonical_discovery_inputs, tmp_path):
    template = canonical_discovery_inputs(version)
    with template.open(tmp_path / 'canonical-discovery.db') as store:
        with schema_inspection.reuse_tables(store.conn):
            yield store, template.result
        with closing(sqlite3.connect(store._db_path.as_uri() + '?mode=ro', uri=True)) as reader:
            reader.row_factory = sqlite3.Row
            with schema_inspection.reuse_tables(reader):
                runner.validate_version(reader, version)


def assert_retained_discovery(template):
    stage, _, stages, captured = template.result
    assert template.source.conn.execute('SELECT copied FROM ' + stages + ' WHERE id=?',
                                       (stage,)).fetchone()[0] > 0
    for table, expected in captured.items():
        assert retained_discovery_rows(template.source.conn, table) == expected


@pytest.mark.parametrize('version', [79, 93])
def test_selected_discovery_upgrade_input_keeps_published_values_and_originals(discovery_upgrade_inputs, version):
    template = discovery_upgrade_inputs(version, version)
    assert_upgrade_phase(template, version)
    assert_retained_discovery(template)
    assert template.source.discovery.get_discovered_artists() == template.result[1]


@pytest.mark.parametrize('version,target,phase', [
    (version, target, phase)
    for version, targets in ((79, (81, 86, 88, 93, 94, 97, 98, 99, 100)), (93, (94, 97, 98, 99, 100)))
    for target in targets for phase in ('input', 'converted')])
def test_selected_discovery_upgrade_phase_keeps_originals(discovery_upgrade_inputs, version, target, phase):
    template = discovery_upgrade_inputs(version, target, phase=phase)
    assert_upgrade_phase(template, target if phase == 'converted' else custody_predecessor(version, target))
    assert_retained_discovery(template)


def read(conn, owner, stage, name):
    return [dict(row) for row in conn.execute('SELECT * FROM '+owner.table(name)+' WHERE stage=?', (stage,))]


def rejection_inputs(store):
    aid, = store.conn.execute('SELECT artist_id FROM main.discovered_artists').fetchone()
    with store.conn:
        store.conn.execute("INSERT INTO main.genre_classification_legacy(artist_id,raw_label,resolution_reason) VALUES (?,'Captured','unknown_genre')", (aid,))
    prepare(store.conn)


@pytest.fixture
def rejection_store(tmp_path, version, _bounded_installed_schema, staged_custody_templates, monkeypatch):
    bound_catalog(monkeypatch)
    seeded = staged_custody_templates(version, 'discovery-subject-seeds', seed, admit=False,
                                     completed=_bounded_installed_schema(version))
    template = staged_custody_templates(version, 'discovery-subject-rejection', rejection_inputs,
                                       admit=False, completed=seeded)
    with template.open(tmp_path / 'discovery-rejection.db') as store:
        with reuse_reporting_reader(store, monkeypatch), schema_inspection.reuse_tables(store.conn):
            yield store
        with closing(sqlite3.connect(store._db_path.as_uri() + '?mode=ro', uri=True)) as reader:
            reader.row_factory = sqlite3.Row
            runner.validate_version(reader, version)


def seed_canonical_visits(store, identities):
    account, aid, rid, album = identities
    with store.conn:
        # Two retained spellings of one visible artist visit count once. Keep
        # the earliest observation even when its captured spelling sorts later.
        store.conn.execute("INSERT INTO main.discovery_visible_impressions VALUES ('visit','artist',?,2)", ('0'+str(aid),))
        store.conn.execute("INSERT INTO main.recording_aliases VALUES ('song alias',?,'captured')", (rid,))
        store.conn.execute("INSERT INTO main.discovery_visible_impressions VALUES ('visit','track','song alias',1)")
        store.conn.execute('UPDATE main.album_discovery_memberships SET offered_count=5,first_shown=0,last_shown=1')
    with schema_inspection.reuse_tables(store.conn):
        artifact, document_stage = artist_ready(store.conn)
        artifact.publish(store.conn, stage=document_stage)
    return account, aid, rid, album


@pytest.fixture(scope='module')
def canonical_discovery_inputs(staged_custody_templates, discovery_seed_inputs):
    inputs = {}

    def get(version, phase='subjects'):
        seeded = discovery_seed_inputs(version)

        def artists(store):
            return seed_canonical_visits(store, seeded.result)

        def subjects(store):
            prepare_subjects(store.conn)
            return seeded.result

        completed = seeded
        for name, build in (('artists', artists), ('subjects', subjects)):
            completed = staged_custody_templates(version, 'canonical-discovery-visits:' + name, build,
                                                  admit=False, completed=completed)
            inputs[version, name] = completed
            if name == phase:
                return completed
        raise ValueError(phase)

    def release(version):
        sources = []
        for (current, _), template in inputs.items():
            if current == version:
                source = template.source
                assert not source.conn.in_transaction
                source._fixture_released = True
                source.conn.close()
                sources.append(source)
        return sources

    get.release = release
    return get


@pytest.mark.parametrize('version,phase', [(version, phase) for phase in ('artists', 'subjects')
                                         for version in (79, 85, 86, 100)])
def test_canonical_discovery_input_keeps_raw_alias_visits_and_album_counter(canonical_discovery_inputs, version, phase):
    template = canonical_discovery_inputs(version, phase)
    store = template.source
    _, _, rid, _ = template.result
    assert_upgrade_phase(template, version)
    assert store.conn.execute('SELECT count(*) FROM main.discovery_visible_impressions').fetchone()[0] == 7
    assert store.conn.execute("SELECT recording_id FROM main.recording_aliases WHERE alias='song alias'").fetchone()[0] == rid
    assert store.conn.execute('SELECT offered_count FROM main.album_discovery_memberships').fetchone()[0] == 5
    assert publication.owner(store.conn).artist.selected(store.conn) is not None
    if phase == 'subjects':
        assert store.conn.execute('SELECT count(*) FROM ' + preparation.IMPRESSIONS).fetchone()[0] > 0


@pytest.mark.parametrize('version', [79, 85, 86, 100])
def test_views_deduplicate_canonical_visits_and_preserve_legacy_residue_and_origins(canonical_discovery_store):
    store, (account, aid, rid, album) = canonical_discovery_store
    original = {table: list(map(tuple, store.conn.execute('SELECT * FROM main.'+table))) for table in
        (*preparation.KEYS, 'discovered_artists', 'schema_migrations')}
    owner, stage = stage_discovery(store.conn)
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


@pytest.mark.parametrize('version', [79, 93])
def test_prepared_discovery_reopen_preserves_sql_and_private_writable_guards(prepared_discovery_store):
    store = prepared_discovery_store
    before = list(store.conn.iterdump())
    other = Store(store._db_path)
    try:
        other.init_schema(defer_metadata=True)
        assert list(store.conn.iterdump()) == before
        assert other._snapshot_revisions.conn is other.conn
        assert other.discovery.get_discovered_artists()[0]['offered_count'] == 9
        other.settings.set_setting('reopened-discovery', 'private write')
        assert store.settings.get_setting('reopened-discovery') == 'private write'
    finally:
        other.conn.close()


@pytest.mark.parametrize('version', [79, 93])
def test_pinned_discovery_reader_keeps_its_generation_during_publication(prepared_discovery_store):
    store = prepared_discovery_store
    with store.read_snapshot() as snapshot:
        pinned = snapshot.store
        assert pinned.discovery.get_discovered_artists()[0]['offered_count'] == 9
        owner, stage = stage_discovery(store.conn)
        owner.publish(store.conn, stage=stage)
        assert pinned.discovery.get_discovered_artists()[0]['offered_count'] == 9
    assert store.discovery.get_discovered_artists()[0]['offered_count'] == 2


@pytest.mark.parametrize('version', [79, 93])
def test_selected_discovery_consumers_read_exact_values_without_legacy_tables(discovery_upgrade_inputs, version, tmp_path):
    template = discovery_upgrade_inputs(version, version)
    with template.open(tmp_path / 'discovery-consumers.db') as other:
        aid = other.conn.execute('SELECT artist_id FROM main.discovered_artists').fetchone()[0]
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


@pytest.mark.parametrize('version', [79, 93])
def test_discovery_writes_withdraw_and_reconcile_published_visits(discovery_upgrade_inputs, version, tmp_path, monkeypatch):
    prepared = discovery_upgrade_inputs(version, version)
    with prepared.open(tmp_path / 'published-discovery.db') as store, reuse_reporting_reader(store, monkeypatch) as reader, schema_inspection.reuse_tables(store.conn):
        reconcile_discovery_visits(store, reader)


def reconcile_discovery_visits(store, other):
    aid = store.conn.execute('SELECT artist_id FROM main.discovered_artists').fetchone()[0]
    owner = publication.owner(store.conn)
    stage = owner.selected(store.conn)
    assert stage is not None
    # Concurrent regular DDL has its own publication-verification tests below.
    # This case owns actual visit invalidation and the next published generation.
    store.discovery.record_discovery_impressions('artist', ['@artist:'+str(aid)], 'new-visit', 8)
    assert owner.selected(store.conn) is None
    assert other.discovery.get_discovered_artists()[0]['offered_count'] == 10
    # These workers were installed by the cold preparation owner. Reconcile
    # their real pending writes without repeating unchanged schema admission.
    artifact, document_stage = artist_ready(store.conn, installed=True)
    artifact.publish(store.conn, stage=document_stage)
    prepare_subjects(store.conn, installed=True)
    successor, newer = stage_discovery(store.conn)
    assert newer != stage
    successor.publish(store.conn, stage=newer)
    candidate, = other.discovery.get_discovered_artists()
    assert (candidate['offered_count'], candidate['first_shown'], candidate['last_shown']) == (3, 3, 8)
    assert other.discovery.discovery_artist_view(10, 7)[0]['legacy_offered_count'] == 14
    assert len(read(store.conn, owner, stage, 'discovery_impressions')) == 3
    assert len(read(store.conn, owner, newer, 'discovery_impressions')) == 4


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


@pytest.mark.parametrize('version', [93])
def test_rejection_input_keeps_completed_subjects_and_captured_classification(version, staged_custody_templates, discovery_seed_inputs):
    template = staged_custody_templates(version, 'discovery-subject-rejection', rejection_inputs,
                                       admit=False, completed=discovery_seed_inputs(version))
    store = template.source
    assert store.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert store.conn.execute('SELECT count(*) FROM '+preparation.IMPRESSIONS).fetchone()[0] > 0
    assert store.conn.execute('SELECT raw_label FROM '+preparation.CLASSIFICATIONS).fetchone()[0] == 'Captured'
    assert publication.owner(store.conn).selected(store.conn) is None
    assert store.discovery.get_discovered_artists()[0]['offered_count'] == 9


@pytest.mark.parametrize('version', [93])
@pytest.mark.parametrize('damage', ['clock', 'identity', 'original', 'missing', 'classification'])
def test_independent_publication_check_rejects_damaged_preparation(
        rejection_store, damage):
    store = rejection_store
    # Completed artist documents and subject rows are inputs to this independent
    # publication verifier. Each case damages its own restored SQL/files, then
    # runs the real map writer, verifier and selection. Concurrent regular DDL
    # is exercised by the two publication-verification concurrency cases below.
    owner = publication.owner(store.conn)
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


def interrupted_discovery_input(store):
    owner = publication.owner(store.conn)
    first = owner.step(store.conn, batch_size=1)
    assert first.owner == 'sources' and first.copied == 1 and not first.complete
    with store.conn:
        store.conn.execute('CREATE TRIGGER controlled_subject_stage_failure BEFORE INSERT ON '+owner.table('input_sources')+
            " BEGIN SELECT RAISE(ABORT,'controlled discovery interruption'); END")
    with pytest.raises(sqlite3.IntegrityError, match='controlled discovery interruption'):
        owner.step(store.conn, batch_size=1)
    assert owner.stage(store.conn, first.id) == first
    def install(conn, context):
        conn.execute('CREATE TABLE independent_preparation_ddl(value TEXT)')

    def validate(conn):
        assert conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='independent_preparation_ddl'").fetchone()

    regular.migrate(store.conn, changes=(regular.Change(1, 'independent_preparation_ddl', install, validate),))
    with store.conn:
        store.conn.execute('DROP TRIGGER controlled_subject_stage_failure')
    return first


@pytest.fixture(scope='module')
def interrupted_discovery_template(staged_custody_templates, discovery_prepared_inputs):
    def get():
        return staged_custody_templates(79, 'interrupted-discovery-stage', interrupted_discovery_input,
            admit=False, completed=discovery_prepared_inputs(79))
    return get


def test_interrupted_discovery_stage_keeps_exact_checkpoint_and_allows_independent_ddl(interrupted_discovery_template):
    template = interrupted_discovery_template()
    owner = publication.owner(template.source.conn)
    assert owner.stage(template.source.conn, template.result.id) == template.result
    assert owner.selected(template.source.conn) is None
    assert template.source.conn.execute('SELECT count(*) FROM independent_preparation_ddl').fetchone()[0] == 0
    assert template.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1


def test_interrupted_stage_and_head_resume_without_partial_selection_or_stale_publish(
        interrupted_discovery_template, tmp_path, monkeypatch, independent_regular_ddl):
    template = interrupted_discovery_template()
    first = template.result
    with template.open(tmp_path / 'restarted-discovery.db') as reopened, schema_inspection.reuse_tables(reopened.conn):
        store = reopened
        owner = publication.owner(reopened.conn)
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
        with schema_inspection.reuse_tables(reopened.conn):
            artifact, document_stage = artist_ready(reopened.conn, installed=True)
            artifact.publish(reopened.conn, stage=document_stage)
            prepare_subjects(reopened.conn, installed=True)
            owner, later = stage_discovery(reopened.conn)
        assert later != stage.id
        owner.publish(reopened.conn, stage=later)
        assert read(reopened.conn, owner, later, 'discovery_sources')[0]['last_found_at'] == 20
        with pytest.raises(sqlite3.IntegrityError, match='[Rr]etained'):
            with reopened.conn:
                reopened.conn.execute('DELETE FROM '+owner.table('input_sources')+' WHERE stage=?', (stage.id,))
@pytest.mark.parametrize('version', [93])
def test_slow_verification_leaves_real_writes_and_regular_migrations_available(
        rejection_store, monkeypatch, independent_regular_ddl):
    store = rejection_store
    aid, = store.conn.execute('SELECT artist_id FROM main.discovered_artists').fetchone()
    owner, stage = stage_discovery(store.conn)
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


@pytest.mark.parametrize('version', [93])
def test_repeated_stage_schema_validation_does_not_reserve_writer(prepared_discovery_store, monkeypatch, independent_regular_ddl):
    store = prepared_discovery_store
    aid = store.conn.execute('SELECT artist_id FROM main.discovered_artists').fetchone()[0]
    owner, stage = stage_discovery(store.conn)
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


@pytest.mark.parametrize('version', [86, 100])
def test_partly_upgraded_genre_layout_admits_earlier_retained_publication(discovery_upgrade_inputs, version, tmp_path, independent_regular_ddl):
    prepared = discovery_upgrade_inputs(79, version)
    stage, _, stages, _ = prepared.result
    partial = Store(tmp_path/'partial-discovery.db')
    try:
        prepared.restore(partial)
        with schema_inspection.reuse_tables(partial.conn):
            partial.init_schema(defer_metadata=True)
            regular.migrate(partial.conn)
        assert publication.read_owners(partial.conn).revision is None
        assert partial.discovery.get_discovered_artists()[0]['offered_count'] == 9
        assert partial.conn.execute('SELECT copied FROM '+stages+' WHERE id=?', (stage,)).fetchone()[0] > 0
        assert partial.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == version
    finally:
        partial.conn.close()


@pytest.mark.parametrize('version', [93])
def test_listening_upgrade_preserves_selected_discovery_views_until_native_takeover(discovery_upgrade_inputs, version, tmp_path, monkeypatch, independent_regular_ddl):
    prepared = discovery_upgrade_inputs(version, 100)
    stage, expected, _, _ = prepared.result
    partial = Store(tmp_path/'listening-upgrade.db')
    try:
        prepared.restore(partial)
        with schema_inspection.reuse_tables(partial.conn):
            partial.init_schema(defer_metadata=True)
            regular.migrate(partial.conn)
        with reuse_reporting_reader(partial, monkeypatch):
            assert publication.read_owners(partial.conn).revision == stage
            assert partial.discovery.get_discovered_artists() == expected
            assert partial.discovery.discovery_album_view(10, 7)[0]['first_shown'] is None
            assert partial.discovery.get_discovered_tracks()[0]['offered_count'] == 1
    finally:
        partial.conn.close()


@pytest.mark.parametrize('version', [79, 93])
def test_selected_discovery_upgrade_inputs_release_each_predecessor(discovery_upgrade_inputs, version):
    sources = discovery_upgrade_inputs.release(version)
    assert sources
    for source in sources:
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            source.conn.execute('SELECT 1')


@pytest.mark.parametrize('version', [79, 93])
def test_prepared_discovery_inputs_release_each_lower_owner(discovery_prepared_inputs, version):
    sources = discovery_prepared_inputs.release(version)
    assert sources
    for source in sources:
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            source.conn.execute('SELECT 1')


@pytest.mark.parametrize('version', [79, 85, 86, 100])
def test_canonical_discovery_inputs_release_their_files_and_connection(canonical_discovery_inputs, version):
    sources = canonical_discovery_inputs.release(version)
    assert len(sources) == 2
    for source in sources:
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            source.conn.execute('SELECT 1')
