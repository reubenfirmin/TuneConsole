"""TV cutovers retain exact evidence and keep all continuing writers usable."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_predecessor_writes import consumer_installed as installed
from tests.core.test_tv_state_preparation import aliases, finish, seed
from tests.test_tv_stream import video
from tests.store_fixture_support import publication_consumer_reads
from yt_playlist.core import tv_recommendation_state as state
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import tv_publication as publication, tv_state as preparation
from yt_playlist.core.store import Store


pytestmark = pytest.mark.usefixtures('publication_consumer_reads')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """TV SQL and file publication do not start the HTTP lifecycle."""


@pytest.mark.parametrize('version', [79, 93, 94])
def test_tv_publication_input_keeps_real_history_and_snapshot_guards(_bounded_installed_schema, version):
    source = _bounded_installed_schema(version).source
    assert source.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == version
    assert source._snapshot_revisions.conn is source.conn
    assert not source.conn.execute('PRAGMA foreign_key_check').fetchone()


def prepare(owner, conn, *, size=128):
    owner.worker.install(conn)
    return finish(owner.worker, conn, size=size).prepared_revision


def rejection_inputs(store, *, surface, with_aliases):
    _, originals = seed(store)
    if with_aliases:
        aliases(store.conn)
    owner = publication.SOURCES[surface]
    revision = prepare(owner, store.conn)
    assert owner.selected(store.conn) is None
    return originals, revision


def publish_all(store):
    aliases(store.conn)
    for owner in publication.OWNERS.values():
        owner.publish(store.conn, revision=prepare(owner, store.conn))


def published_writer_inputs(store):
    values, originals = seed(store)
    publish_all(store)
    return values, originals


@pytest.mark.parametrize('version', [79, 93])
def test_tv_continuing_writer_input_keeps_all_five_actual_publications(
        version, _bounded_installed_schema, staged_custody_templates):
    template = staged_custody_templates(version, 'tv-continuing-writers', published_writer_inputs,
        completed=_bounded_installed_schema(version), admit=False)
    _, originals = template.result
    assert len(publication.OWNERS) == 5
    for owner in publication.OWNERS.values():
        assert owner.selected(template.source.conn) == 0
        assert owner.value(template.source.conn, 0) == originals[owner.source]
    assert not template.source.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_selected_presence_reads_no_display_document(installed, monkeypatch):
    store, _ = installed
    values, _ = seed(store)
    publish_all(store)
    def forbidden(*args, **kwargs):
        pytest.fail('Presence read a selected TV document')
    for owner in publication.OWNERS.values():
        monkeypatch.setattr(type(owner), 'value', forbidden)
    for surface in values:
        assert store.rec_proposals.has_proposals(surface)


@pytest.mark.parametrize('installed', [79, 93, 94], indirect=True)
@pytest.mark.parametrize('surface', tuple(publication.SOURCES))
def test_all_owners_work_for_preopened_and_reopened_readers(installed, surface):
    store, _ = installed
    values, originals = seed(store)
    other = Store(store._db_path)
    try:
        other.init_schema(defer_metadata=True)
        aliases(store.conn)
        owner = publication.SOURCES[surface]
        owner.publish(store.conn, revision=prepare(owner, store.conn))
        assert owner.selected(other.conn) == 0
        assert other.rec_proposals._proposal_json(surface) == originals[surface]
        assert other.rec_proposals.get_proposals(surface) == values[surface]
        assert other.rec_proposals.has_proposals(surface)
        assert not other._tv_recommendations
        if surface == 'tv_visual_checks':
            assert other.tv.static_ids(2) == {'visual00001'}
        elif surface == 'tv_continuations':
            assert other.tv.receipt('10::request:with:colons') == values[surface]['10::request:with:colons']
        regular.validate(other.conn)
    finally:
        other.conn.close()
    reopened = Store(store._db_path)
    try:
        reopened.init_schema(defer_metadata=True)
        assert reopened.rec_proposals.get_proposals(surface) == values[surface]
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('surface', tuple(publication.SOURCES))
def test_absence_empty_shapes_and_recreated_sources_remain_distinct(installed, surface):
    store, _ = installed
    aliases(store.conn)
    owner = publication.SOURCES[surface]
    owner.publish(store.conn, revision=prepare(owner, store.conn))
    assert owner.selected(store.conn) == 0
    assert store.rec_proposals.get_proposals(owner.source) is None
    assert not store.rec_proposals.has_proposals(owner.source)
    store.rec_proposals.put_proposals(owner.source, {}, None)
    assert owner.selected(store.conn) is None
    owner.publish(store.conn, revision=prepare(owner, store.conn))
    assert store.rec_proposals.get_proposals(owner.source) == {}
    assert not store.rec_proposals.has_proposals(owner.source)
    revision = owner.selected(store.conn)
    with store.conn:
        store.conn.execute('DELETE FROM rec_proposals WHERE surface=?', (owner.source,))
    assert owner.selected(store.conn) is None
    owner.publish(store.conn, revision=prepare(owner, store.conn))
    assert store.rec_proposals.get_proposals(owner.source) is None
    assert owner.value(store.conn, revision) == '{}'


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('surface,column,value', [
    ('tv_pool', 'field_mask', 0), ('tv_artist_artwork', 'artist_id', 999),
    ('tv_artist_artwork_requests', 'requested_at', 99),
    ('tv_visual_checks', 'sample_times', 99), ('tv_continuations', 'target_stream', 'wrong'),
])
def test_each_owner_rejects_unverified_typed_state(installed, surface, column, value):
    store, _ = installed
    owner = publication.SOURCES[surface]
    originals, revision = rejection_inputs(store, surface=surface, with_aliases=True)
    table = {'tv_pool':'tv_pool_state', 'tv_artist_artwork':'tv_artist_artwork',
        'tv_artist_artwork_requests':'tv_artist_requests', 'tv_visual_checks':'tv_visual_samples',
        'tv_continuations':'tv_continuation_receipts'}[surface]
    if column == 'sample_times':
        column = 'at'
    with store.conn:
        store.conn.execute('UPDATE '+preparation.relation(table)+' SET '+column+'=?'+
            (' WHERE resolution_reason IS NULL' if column == 'artist_id' else ''), (value,))
    with pytest.raises(MigrationError, match='differs'):
        owner.publish(store.conn, revision=revision)
    assert owner.selected(store.conn) is None
    assert store.rec_proposals._proposal_json(surface) == originals[surface]


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('failure', ['time', 'missing', 'extra', 'original'])
def test_time_completeness_and_original_file_are_verified(installed, failure):
    store, _ = installed
    owner = publication.OWNERS['pool']
    _, revision = rejection_inputs(store, surface=owner.source, with_aliases=False)
    with store.conn:
        if failure == 'time':
            store.conn.execute('UPDATE '+preparation.tables(owner.source)[0]+' SET built_at=1')
        elif failure == 'missing':
            store.conn.execute('DELETE FROM '+preparation.relation('tv_pool_items')+' WHERE position=1')
        elif failure == 'extra':
            store.conn.execute('INSERT INTO '+preparation.relation('tv_pool_attempts')+
                '(source,preparation_revision,pool_id,position,raw_key,resolution_reason) VALUES (?,?,1,2,?,?)',
                (owner.source, revision, 'extra', 'missing_recording'))
        else:
            Path(store.conn.execute('SELECT filename FROM '+preparation.tables(owner.source)[2]).fetchone()[0]).write_bytes(b'corrupt')
    with pytest.raises((MigrationError, OSError, ValueError)):
        owner.publish(store.conn, revision=revision)
    assert owner.selected(store.conn) is None


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('timing', ['files', 'rows'])
def test_source_and_custody_races_cannot_select_stale_tv_state(installed, timing, monkeypatch):
    store, _ = installed
    seed(store)
    owner = publication.OWNERS['pool']
    revision = prepare(owner, store.conn)
    other = sqlite3.connect(store._db_path, timeout=.01)
    read = preparation.read_original
    def files(conn, header):
        assert not conn.in_transaction
        raw = read(conn, header)
        with other:
            other.execute('UPDATE rec_proposals SET built_at=1 WHERE surface=?', (owner.source,))
        return raw
    def rows(conn, header, value):
        publication.verify_rows(conn, header, value)
        with other:
            other.execute('UPDATE '+preparation.relation('tv_pool_state')+' SET at=99')
    try:
        if timing == 'files':
            monkeypatch.setattr(publication.TVPreparation, 'read_original', staticmethod(files))
            target = owner
        else:
            target = replace(owner, verify_rows=rows)
        with pytest.raises(MigrationError, match='changed|reconciled'):
            target.publish(store.conn, revision=revision)
        assert owner.selected(store.conn) is None
    finally:
        other.close()


@pytest.mark.parametrize('installed', [79, 93], indirect=True)
def test_real_continuing_writers_withdraw_selections_and_reconcile(installed, staged_custody):
    store, _ = installed
    values, originals = staged_custody(installed, 'tv-continuing-writers', published_writer_inputs, admit=False)
    with store.read_snapshot() as snapshot:
        assert snapshot.store.tv.receipt('10::request:with:colons') == values['tv_continuations']['10::request:with:colons']
        store.tv.complete_receipt('11:stream:next', {'playlist_id':21, 'stream':'continued', 'items':[video(3)]}, 99)
        assert not snapshot.publish(lambda:None)
    assert publication.OWNERS['continuations'].selected(store.conn) is None
    assert publication.OWNERS['pool'].selected(store.conn) == 0
    store.tv.request_artwork('new', 'Artist', 99)
    assert publication.OWNERS['artwork-requests'].selected(store.conn) is None
    assert store.tv.publish_artwork('new', {'images':['https://example.test/new'], 'at':99}, 99, (None, None), complete=True)
    assert publication.OWNERS['artwork'].selected(store.conn) is None
    assert 'new' not in store.rec_proposals.get_proposals('tv_artist_artwork_requests')
    store.tv.publish_visual('visual00002', {'verdict':'moving', 'version':2}, 99)
    store.rec_proposals.put_proposals('tv_pool', {'items':[video(4)], 'at':99}, 99)
    for owner in publication.OWNERS.values():
        owner.publish(store.conn, revision=prepare(owner, store.conn))
        assert owner.value(store.conn, 0) == originals[owner.source]
    assert store.tv.static_ids(2) == {'visual00001'}
    assert store.tv.receipt('11:stream:next')['items'] == [video(3)]
    regular.validate(store.conn)


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_legacy_artwork_publication_survives_later_request_removal_failure(installed, monkeypatch):
    store, _ = installed
    values, _ = seed(store)
    publish_all(store)
    other = sqlite3.connect(store._db_path, timeout=.001)
    read = store.rec_proposals.get_proposals
    writes = store.rec_proposals.put_proposals
    checked = []
    def outside_transaction(surface):
        assert not store.conn.in_transaction
        other.execute('BEGIN IMMEDIATE')
        other.rollback()
        checked.append(surface)
        return read(surface)
    monkeypatch.setattr(store.rec_proposals, 'get_proposals', outside_transaction)
    try:
        store.tv.request_artwork('new', 'Artist', 99)
        store.tv.publish_visual('visual00002', {'verdict':'moving'}, 99)
        store.tv.complete_receipt('11:s:r', {'playlist_id':21, 'stream':'next'}, 99)
        def fail(surface, value, now):
            if surface == 'tv_artist_artwork_requests':
                raise RuntimeError('controlled request removal failure')
            return writes(surface, value, now)
        monkeypatch.setattr(store.rec_proposals, 'put_proposals', fail)
        with pytest.raises(RuntimeError, match='controlled request removal failure'):
            store.tv.publish_artwork('new', {'images':[]}, 99, (None, None), complete=True)
        monkeypatch.setattr(store.rec_proposals, 'get_proposals', read)
        assert checked
        assert read('tv_artist_artwork') == {**values['tv_artist_artwork'], 'new': {'images': []}}
        assert read('tv_artist_artwork_requests')['new'] == 'Artist'
        assert publication.OWNERS['artwork'].selected(store.conn) is None
        assert not store.conn.in_transaction
    finally:
        other.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_selected_custody_missing_file_and_failed_recovery_keep_ddl_independent(installed, monkeypatch):
    store, _ = installed
    _, originals = seed(store)
    owner = publication.OWNERS['pool']
    owner.publish(store.conn, revision=prepare(owner, store.conn))
    with pytest.raises(sqlite3.IntegrityError, match='retained'):
        with store.conn:
            store.conn.execute('UPDATE '+preparation.relation('tv_pool_items')+' SET title=title')
    filename = store.conn.execute('SELECT filename FROM '+preparation.tables(owner.source)[2]).fetchone()[0]
    Path(filename).unlink()
    def fail(*args):
        assert not store.conn.in_transaction
        raise OSError('controlled TV recovery failure')
    with pytest.raises(OSError, match='controlled TV recovery failure'):
        replace(owner.worker, repair_files=fail).step(store.conn, retry=True)
    before = owner.worker.progress(store.conn)
    assert before.status == 'failed'
    store.init_schema(defer_metadata=True)
    monkeypatch.setattr(regular, 'CHANGES', (regular.Change(1, 'independent_tv_publication',
        lambda conn, context: conn.execute('CREATE TABLE independent_tv_publication(value TEXT)'),
        lambda conn: conn.execute('SELECT value FROM independent_tv_publication LIMIT 0')),))
    regular.migrate(store.conn)
    assert owner.worker.progress(store.conn) == before
    with pytest.raises(MigrationError, match='retry explicitly'):
        owner.worker.step(store.conn)
    owner.worker.step(store.conn, retry=True)
    owner.publish(store.conn, revision=owner.worker.progress(store.conn).prepared_revision)
    assert store.rec_proposals._proposal_json(owner.source) == originals[owner.source]
