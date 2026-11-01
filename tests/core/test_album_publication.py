"""Album occurrence cutover proves source custody, grouping and continuing writes."""
from dataclasses import replace
import json
import sqlite3

import pytest

from tests.core.test_album_owner_preparation import finish, seed
from tests.core.test_predecessor_writes import installed
from yt_playlist.core import albums
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import album_occurrences as preparation, album_publication as publication
from yt_playlist.core.store import Store


def stage(conn, size=32):
    preparation.JOB.install(conn)
    for _ in range(2000):
        if preparation.JOB.step(conn).status == 'prepared':
            break
    else:
        pytest.fail('Album preparation did not finish bounded fixture')
    for _ in range(1000):
        result = publication.step(conn, batch_size=size)
        if result.complete:
            return result.id
    pytest.fail('Album publication did not finish bounded fixture')


def rich_source(store):
    album, entry = seed(store)
    second = store.tracks.upsert_track('two', 'Two', 'Artist', None, 10, album_browse_id='partial')
    partial = albums.lookup(store.conn, 'partial')
    with store.conn:
        store.conn.execute("INSERT INTO main.album_provider_ids(provider,provider_id,album_id,is_primary,equivalence_digest,verified_at) "
            "VALUES ('youtube','release-alias',?,0,'verified-fixture',1)", (album,))
        store.conn.execute("INSERT INTO main.album_tracks(album_id,entry_id,position,is_full,source) VALUES (?,?,0,1,'partial fetch')", (partial, second))
        store.conn.execute('INSERT INTO main.album_fetch_state VALUES (?,0,1,4)', (partial,))
        state_only = albums.observe(store.conn, 'state-only', title='Empty capture')
        store.conn.execute('INSERT INTO main.album_fetch_state VALUES (?,NULL,0,0)', (state_only,))
        missing_state = albums.observe(store.conn, 'no-state', title='Retained partial')
        store.conn.execute("INSERT INTO main.album_tracks(album_id,legacy_track_key,is_full,source) VALUES (?,'unknown|subject',1,'legacy')", (missing_state,))
    return album, entry


def projection(conn, tracks, states, links, keys):
    return {
        'tracks':sorted([tuple(row) for row in conn.execute('SELECT album_id,entry_id,recording_id,legacy_track_key,position,disc_number,track_number,is_full,source FROM '+tracks)], key=repr),
        'states':list(map(tuple, conn.execute('SELECT * FROM '+states+' ORDER BY album_id'))),
        'links':list(map(tuple, conn.execute('SELECT browse_id,recording_id,entry_id,video_id,source FROM '+links+' ORDER BY browse_id,entry_id'))),
        'keys':list(map(tuple, conn.execute('SELECT * FROM '+keys+' ORDER BY browse_id')))}


@pytest.mark.parametrize('installed', [79, 93, 98], indirect=True)
def test_preopened_projection_groups_match_frozen_native_without_losing_originals(installed, tmp_path):
    store, _ = installed
    album, _ = rich_source(store)
    sources = {owner:[dict(row) for row in store.conn.execute('SELECT * FROM main.'+owner+' ORDER BY '+key)]
        for owner,key in [('album_tracks','id'), ('album_fetch_state','album_id')]}
    preopened = Store(store._db_path)
    native = Store(tmp_path / 'native.db')
    try:
        preopened.init_schema(defer_metadata=True)
        store.conn.backup(native.conn)
        runner.migrate(native.conn, registry=registry.MIGRATIONS)
        native.init_schema(defer_metadata=True)
        identity = stage(store.conn, size=1)
        evidence = store.conn.execute('SELECT evidence_changes FROM rec_preparation WHERE id=1').fetchone()[0]
        publication.publish(store.conn, identity=identity)
        assert store.conn.execute('SELECT evidence_changes FROM rec_preparation WHERE id=1').fetchone()[0] == evidence+1
        actual = projection(store.conn, *(publication.relation(store.conn, owner) for owner in publication.RELATIONS))
        expected = projection(native.conn, 'main.album_tracks', 'main.album_fetch_state', 'main.album_track_links', 'main.album_tracklist_keys')
        assert actual == expected
        # A reader opened before installation sees the destination. Mask the
        # fallback branch's original columns to prove they supply no values.
        def mask_original(operation, owner, column, *unused):
            return sqlite3.SQLITE_IGNORE if operation == sqlite3.SQLITE_READ and owner in sources else sqlite3.SQLITE_OK
        preopened.conn.set_authorizer(mask_original)
        try:
            assert preopened.collection.cached_album_tracklist_ids() == {'release','release-alias'}
            assert preopened.collection.has_published_album_tracklist('release-alias')
            assert not preopened.collection.has_published_album_tracklist('partial')
        finally:
            preopened.conn.set_authorizer(None)
        assert preopened.charts.album_tracks_detail('release') == native.charts.album_tracks_detail('release')
        assert preopened.charts.album_tracks_detail('partial') == native.charts.album_tracks_detail('partial')
        assert preopened.charts.album_browse_ids() == native.charts.album_browse_ids()
        assert preopened.charts.corpus_distribution('album') == native.charts.corpus_distribution('album')
        assert preopened.collection.complete_album_tracklist_ids() == native.collection.complete_album_tracklist_ids()
        assert preopened.rec_query.owned_albums() == native.rec_query.owned_albums()
        assert preopened.tracks.album_tracks_to_enrich('release-alias') == native.tracks.album_tracks_to_enrich('release-alias')
        assert preopened.tracks.album_tracks_for_waterfall('partial') == native.tracks.album_tracks_for_waterfall('partial')
        assert preopened.enrichment_reports.conflict_count_for_album('release') == native.enrichment_reports.conflict_count_for_album('release')
        assert preopened.enrichment_reports.unresolved_conflicts_for_album('partial') == native.enrichment_reports.unresolved_conflicts_for_album('partial')
        assert preopened.enrichment_queue._select_enrich_batch(5) == native.enrichment_queue._select_enrich_batch(5)
        assert [tuple(row)[1:] for row in store.conn.execute('SELECT * FROM '+publication.table('groups')+' WHERE stage=? ORDER BY album_id', (identity,))] == [
            (row['album_id'],row['provider_id'],store.conn.execute('SELECT count(*) FROM main.album_tracks WHERE album_id=? AND is_full=1', (row['album_id'],)).fetchone()[0],
             row['order_status'],row['status'],row['expected_count'],row['fetched_at'])
            for row in native.conn.execute("SELECT * FROM main.album_tracklists WHERE capture_kind='legacy_full' ORDER BY album_id")]
        for owner, expected_rows in sources.items():
            key = 'id' if owner == 'album_tracks' else 'album_id'
            assert [dict(row) for row in store.conn.execute('SELECT * FROM main.'+owner+' ORDER BY '+key)] == expected_rows
        regular.validate(preopened.conn)
        assert store.conn.execute('SELECT count(*) FROM main.catalog_entries').fetchone()[0] == 2
    finally:
        native.conn.close()
        preopened.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('failure', ['field','reference','provider','extra','omitted','state','original','group','group-omitted'])
def test_invalid_typed_rows_originals_coverage_and_groups_cannot_publish(installed, failure, monkeypatch):
    store, _ = installed
    seed(store)
    preparation.JOB.install(store.conn)
    finish(preparation.JOB, store.conn)
    with store.conn:
        if failure == 'field':
            store.conn.execute("UPDATE prepared_album_track_rows SET captured_title='different'")
        elif failure == 'reference':
            store.conn.execute("UPDATE prepared_album_track_rows SET entry_id=NULL,source_provider=NULL WHERE is_full=1 AND entry_id IS NOT NULL")
        elif failure == 'provider':
            store.conn.execute("UPDATE prepared_album_fetch_rows SET provider_id_snapshot='different'")
        elif failure == 'extra':
            columns = [row[1] for row in store.conn.execute('PRAGMA table_info(prepared_album_track_rows)')]
            store.conn.execute('INSERT INTO prepared_album_track_rows SELECT '+','.join('999999' if key == 'row_id' else key for key in columns)+' FROM prepared_album_track_rows LIMIT 1')
        elif failure == 'omitted':
            store.conn.execute('DELETE FROM prepared_album_track_rows WHERE row_id=(SELECT max(row_id) FROM prepared_album_track_rows)')
        elif failure == 'state':
            store.conn.execute('DELETE FROM prepared_album_fetch_rows')
        elif failure == 'original':
            store.conn.execute("UPDATE prepared_album_row_originals SET snapshot='{}'")
    group, group_keys = publication.group, publication.group_keys
    if failure == 'group':
        monkeypatch.setattr(publication, 'group', lambda *args:dict(group(*args), status='partial'))
    elif failure == 'group-omitted':
        monkeypatch.setattr(publication, 'group_keys', lambda *args:[])
    identity = stage(store.conn)
    monkeypatch.setattr(publication, 'group', group)
    monkeypatch.setattr(publication, 'group_keys', group_keys)
    with pytest.raises((MigrationError, ValueError)):
        publication.publish(store.conn, identity=identity)
    assert publication.selected(store.conn) is None
    regular.migrate(store.conn)


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_bounded_large_capture_real_writer_snapshot_and_immutable_custody(installed):
    store, _ = installed
    album, entry = seed(store)
    with store.conn:
        store.conn.execute('DELETE FROM main.album_tracks WHERE album_id=? AND is_full=1', (album,))
        store.conn.executemany("INSERT INTO main.album_tracks(album_id,entry_id,position,is_full,source) VALUES (?,?,?,1,'large')",
            [(album, entry, position) for position in range(8192)])
        store.conn.execute('UPDATE main.album_fetch_state SET expected_track_count=8192 WHERE album_id=?', (album,))
    preparation.JOB.install(store.conn)
    for _ in range(300):
        if preparation.JOB.step(store.conn).status == 'prepared':
            break
    first = publication.step(store.conn)
    assert first.copied == 32 and not first.complete
    # A reopened worker resumes the same immutable stage and bounded cursor.
    other = sqlite3.connect(store._db_path)
    other.row_factory = sqlite3.Row
    try:
        assert publication.step(other).copied == 64
    finally:
        other.close()
    identity = stage(store.conn)
    publication.publish(store.conn, identity=identity)
    assert len(store.charts.album_tracks_detail('release')) == 8192
    with store.read_snapshot() as snapshot:
        assert len(snapshot.store.charts.album_tracks_detail('release')) == 8192
        store.collection.cache_album_tracklist('release','Continuing','Artist',None,[],entries=[{'entry_id':entry}]*3,video_ids=['one']*3)
        assert len(snapshot.store.charts.album_tracks_detail('release')) == 8192
        assert not snapshot.publish(lambda:None)
    assert publication.selected(store.conn) is None
    assert len(store.charts.album_tracks_detail('release')) == 3
    next_stage = stage(store.conn)
    publication.publish(store.conn, identity=next_stage)
    assert len(store.charts.album_tracks_detail('release')) == 3
    assert store.conn.execute('SELECT count(*) FROM '+publication.table('tracks')+' WHERE stage=? AND is_full=1', (identity,)).fetchone()[0] == 8192
    for sql in ['DELETE FROM '+publication.table('tracks'), 'UPDATE '+publication.table('groups')+" SET status='partial'",
            'DELETE FROM '+preparation.ORIGINALS,
            'INSERT INTO '+publication.table('states')+' SELECT * FROM '+publication.table('states')+' LIMIT 1']:
        with pytest.raises(sqlite3.IntegrityError, match='retained|cleanup'):
            with store.conn:
                store.conn.execute(sql)


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('when', ['verification','after-verification','empty-after-verification'])
def test_source_and_reference_races_reject_stale_cutover(installed, when, monkeypatch):
    store, _ = installed
    if when != 'empty-after-verification':
        seed(store)
    identity = stage(store.conn)
    other = sqlite3.connect(store._db_path)
    changed = False
    def change():
        nonlocal changed
        if not changed:
            changed = True
            with other:
                if when == 'empty-after-verification':
                    other.execute("INSERT INTO albums(title,metadata_rank) VALUES ('Continuing new album',0)")
                else:
                    other.execute("UPDATE youtube_items SET title='Continuing metadata' WHERE video_id='one'")
    describe, verify = preparation.describe, publication.verify
    def during(*args):
        result = describe(*args)
        assert not store.conn.in_transaction
        change()
        return result
    def after(*args):
        result = verify(*args)
        change()
        return result
    monkeypatch.setattr(preparation, 'describe', during if when == 'verification' else describe)
    monkeypatch.setattr(publication, 'verify', after if when != 'verification' else verify)
    try:
        with pytest.raises(MigrationError, match='changed|reconciled|differs'):
            publication.publish(store.conn, identity=identity)
    finally:
        other.close()
    assert publication.selected(store.conn) is None


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_interrupted_copy_cutover_failed_preparation_and_independent_ddl(installed, monkeypatch):
    store, _ = installed
    album, _ = seed(store)
    preparation.JOB.install(store.conn)
    finish(preparation.JOB, store.conn)
    publication.install(store.conn)
    with store.conn:
        store.conn.execute('CREATE TRIGGER controlled_album_copy BEFORE INSERT ON '+publication.table('tracks')+
            " BEGIN SELECT RAISE(ABORT,'controlled copy'); END")
    with pytest.raises(sqlite3.IntegrityError, match='controlled copy'):
        publication.step(store.conn)
    assert not store.conn.execute('SELECT 1 FROM '+publication.table('stages')).fetchone()
    with store.conn:
        store.conn.execute('DROP TRIGGER controlled_album_copy')
    identity = stage(store.conn)
    with store.conn:
        store.conn.execute('CREATE TRIGGER controlled_album_cutover BEFORE INSERT ON '+publication.table('head')+
            " BEGIN SELECT RAISE(ABORT,'controlled cutover'); END")
    with pytest.raises(sqlite3.IntegrityError, match='controlled cutover'):
        publication.publish(store.conn, identity=identity)
    assert not store.conn.execute('SELECT 1 FROM '+publication.table('generations')).fetchone()
    with store.conn:
        store.conn.execute('DROP TRIGGER controlled_album_cutover')
    publication.publish(store.conn, identity=identity)
    with store.conn:
        store.conn.execute('UPDATE main.album_fetch_state SET fetched_at=0 WHERE album_id=?', (album,))
    def fail(*args):
        preparation.copy(*args)
        raise OSError('controlled row preparation failure')
    with pytest.raises(OSError, match='controlled row'):
        replace(preparation.JOB, copy=fail).step(store.conn)
    failed = preparation.JOB.progress(store.conn)
    def install(conn, context):
        conn.execute('CREATE TABLE independent_album_publication(value TEXT)')
    def validate(conn):
        assert conn.execute("SELECT 1 FROM sqlite_schema WHERE name='independent_album_publication'").fetchone()
    monkeypatch.setattr(regular, 'CHANGES', (*regular.CHANGES, regular.Change(len(regular.CHANGES) + 1,'independent_album_publication',install,validate)))
    regular.migrate(store.conn)
    assert preparation.JOB.progress(store.conn) == failed
    with pytest.raises(MigrationError, match='retry explicitly'):
        preparation.JOB.step(store.conn)
    preparation.JOB.step(store.conn, retry=True)
    publication.publish(store.conn, identity=stage(store.conn))


def test_native_upgrade_keeps_custody_and_validates_its_guards(tmp_path):
    path = tmp_path / 'upgrade.db'
    store = Store(path)
    try:
        runner.migrate(store.conn, registry=registry.MIGRATIONS[:98])
        store.init_schema(defer_metadata=True)
        seed(store)
        identity = stage(store.conn)
        publication.publish(store.conn, identity=identity)
        before = list(map(tuple, store.conn.execute('SELECT * FROM '+publication.table('tracks'))))
        store.conn.close()
        store = Store(path)
        runner.migrate(store.conn, registry=registry.MIGRATIONS)
        store.init_schema(defer_metadata=True)
        assert publication.selected(store.conn) is None
        assert publication.relation(store.conn, 'album_tracks') == 'album_tracks'
        assert not store.conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?', (publication.table('read_tracks'),)).fetchone()
        assert list(map(tuple, store.conn.execute('SELECT * FROM '+publication.table('tracks')))) == before
        regular.validate(store.conn)
        with store.conn:
            store.conn.execute('DROP TRIGGER '+publication.table('tracks_delete'))
        with pytest.raises(MigrationError, match='publication object'):
            regular.validate(store.conn)
    finally:
        store.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_compatibility_read_views_remain_required_before_native_ownership(installed):
    store, _ = installed
    seed(store)
    publication.publish(store.conn, identity=stage(store.conn))
    with store.conn:
        store.conn.execute('DROP VIEW '+publication.table('read_tracks'))
    with pytest.raises(MigrationError, match='publication object'):
        regular.validate(store.conn)


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('failure', ['known-order-gap','missing-provider'])
def test_missing_provider_or_complete_order_gap_does_not_publish(installed, failure):
    store, _ = installed
    album, entry = seed(store)
    with store.conn:
        if failure == 'known-order-gap':
            store.conn.execute('DELETE FROM main.album_tracks WHERE album_id=? AND is_full=1', (album,))
            store.conn.executemany("INSERT INTO main.album_tracks(album_id,entry_id,position,is_full,source) VALUES (?,?,?,1,'captured')",
                [(album,entry,2),(album,entry,5)])
            store.conn.execute('UPDATE main.album_fetch_state SET expected_track_count=2 WHERE album_id=?', (album,))
        else:
            store.conn.execute('DELETE FROM main.album_provider_ids WHERE album_id=?', (album,))
    with pytest.raises(MigrationError, match='order|provider'):
        stage(store.conn)
    assert publication.selected(store.conn) is None
    regular.migrate(store.conn)
