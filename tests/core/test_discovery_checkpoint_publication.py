"""Independent discovery checkpoint selection and continuing installed writes."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading

import pytest

from tests.core.test_discovery_checkpoint_preparation import finish, seed
from tests.core.test_listening_preparation import connect
from tests.core.test_predecessor_writes import installed
from yt_playlist.core import discovery_checkpoints, provider_frontiers
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import discovery_checkpoint_publication as publication
from yt_playlist.core.preparation import discovery_checkpoints as preparation
from yt_playlist.core.store import Store


def ready(conn, name, size=1):
    job, owner = preparation.JOBS[name], publication.OWNERS[name]
    job.install(conn)
    finish(job, conn)
    previous = 0
    for _ in range(100):
        stage = owner.step(conn, batch_size=size)
        assert 0 <= stage.copied-previous <= size
        previous = stage.copied
        if stage.complete:
            return stage.id
    pytest.fail('Checkpoint publication did not complete')


@pytest.mark.parametrize('installed', [79, 93], indirect=True)
def test_selected_reads_use_typed_rows_and_preserve_exact_source_keys(installed):
    store, _ = installed
    values = seed(store)
    store.discovery.upsert_discovered_artist('Artist', 2, ['because'], ['fits'], '', 100)
    preopened = Store(store._db_path)
    try:
        preopened.init_schema(defer_metadata=True)
        # Keep an old WAL snapshot across the whole preparation/migration job.
        # This fixture lifetime exceeds an ordinary bounded serving read; the
        # later preopened.read_view still exercises that public read contract.
        with connect(store) as pinned, connect(store) as job:
            pinned.execute('PRAGMA query_only=ON')
            pinned.execute('BEGIN')
            pinned.execute('SELECT 1 FROM sqlite_schema LIMIT 1').fetchone()
            for name, owner in publication.OWNERS.items():
                identity = ready(job, name)
                owner.publish(job, stage=identity)
                assert owner.selected(pinned) is None
                assert owner.selected(preopened.conn) == identity
                for suffix, raw in values[name].items():
                    assert owner.read(job, identity, preparation.PREFIXES[name]+suffix)['raw_value'] == raw
            regular.migrate(job)
        with preopened.read_view() as reader:
            def mask(operation, table, column, *unused):
                return sqlite3.SQLITE_IGNORE if operation == sqlite3.SQLITE_READ and table == 'settings' and column == 'value' else sqlite3.SQLITE_OK
            reader.conn.set_authorizer(mask)
            assert discovery_checkpoints.fetched(reader.conn, 'release')
            assert discovery_checkpoints.fetched(reader.conn, 'unknown-release')
            assert not discovery_checkpoints.fetched(reader.conn, 'absent-release')
            assert provider_frontiers.read(reader.conn, 'known') == {
                'cursor':7, 'cutoff':10, 'fingerprint':'exact été', 'revision':1}
            assert provider_frontiers.read(reader.conn, 'unknown')['revision'] == 3
            assert provider_frontiers.read(reader.conn, 'absent') is None
            # The noncanonical spelling "01" must not introduce a duplicate
            # artist or override the exact installed key's retry clock.
            assert reader.discovery.artist_images_due([1], 123.6) == [{'artist_id':1, 'artist':'Artist'}]
            assert reader.discovery.artist_images_due([1], 123) == []
            reader.conn.set_authorizer(None)
        for name, rows in values.items():
            for suffix, raw in rows.items():
                assert store.settings.get_setting(preparation.PREFIXES[name]+suffix) == raw
    finally:
        preopened.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_old_writers_withdraw_only_their_family_and_keep_retained_custody(installed):
    store, _ = installed
    seed(store)
    with connect(store) as job:
        identities = {}
        for name, owner in publication.OWNERS.items():
            identities[name] = ready(job, name)
            owner.publish(job, stage=identities[name])
    with store._lock, store.conn:
        provider_frontiers.start(store.conn, 'known', 30, 'replacement')
    with connect(store) as job:
        owner = publication.OWNERS['provider-refresh']
        assert owner.selected(job) is None
        assert publication.OWNERS['fetch-history'].selected(job) == identities['fetch-history']
        assert publication.OWNERS['artist-images'].selected(job) == identities['artist-images']
        assert provider_frontiers.read(job, 'known')['fingerprint'] == 'replacement'
        newer = ready(job, 'provider-refresh')
        owner.publish(job, stage=newer)
        assert newer != identities['provider-refresh']
        assert owner.read(job, identities['provider-refresh'], provider_frontiers.PREFIX+'known')['fingerprint'] == 'exact été'
        with pytest.raises(sqlite3.IntegrityError, match='future cleanup'):
            job.execute('DELETE FROM '+owner.table('rows'))
        job.rollback()
    with store._lock, store.conn:
        discovery_checkpoints.remember(store.conn, 'later')
    with connect(store) as job:
        assert publication.OWNERS['fetch-history'].selected(job) is None
        assert publication.OWNERS['provider-refresh'].selected(job) == newer
        assert discovery_checkpoints.fetched(job, 'later')


@pytest.mark.parametrize('installed', [79], indirect=True)
@pytest.mark.parametrize('damage', ['missing', 'extra', 'typed', 'original'])
def test_source_audit_rejects_omissions_extra_rows_and_false_preparation(installed, damage):
    store, _ = installed
    seed(store)
    name = 'provider-refresh'
    owner, definition = publication.OWNERS[name], preparation.JOBS[name]
    with connect(store) as job:
        definition.install(job)
        finish(definition, job)
        source = provider_frontiers.PREFIX+'known'
        with job:
            if damage == 'missing':
                job.execute('DELETE FROM '+preparation.TABLES[name]+' WHERE source=?', (source,))
            elif damage == 'extra':
                row = dict(job.execute('SELECT * FROM '+preparation.TABLES[name]+' WHERE source=?', (source,)).fetchone())
                row['source'] += '-extra'
                job.execute('INSERT INTO '+preparation.originals(name)+' VALUES (?,?,?)', (row['source'], row['source_digest'], '{}'))
                job.execute('INSERT INTO '+preparation.TABLES[name]+' VALUES ('+','.join('?' for _ in row)+')', tuple(row.values()))
            elif damage == 'typed':
                job.execute('UPDATE '+preparation.TABLES[name]+' SET cursor=88 WHERE source=?', (source,))
            else:
                job.execute('UPDATE '+preparation.originals(name)+" SET raw_value='{}' WHERE source=?", (source,))
        identity = ready(job, name)
        with pytest.raises(MigrationError, match='omits installed|no installed source|differ from installed'):
            owner.publish(job, stage=identity)
        assert owner.selected(job) is None
        regular.migrate(job)


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_interrupted_batch_and_final_selection_resume_without_moving_checkpoint(installed):
    store, _ = installed
    seed(store)
    name, owner = 'fetch-history', publication.OWNERS['fetch-history']
    with connect(store) as job:
        definition = preparation.JOBS[name]
        definition.install(job)
        finish(definition, job)
        first = owner.step(job, batch_size=1)
        with job:
            job.execute('CREATE TRIGGER interrupt_checkpoint BEFORE INSERT ON '+owner.table('rows')+
                        " BEGIN SELECT RAISE(ABORT,'controlled batch interruption'); END")
        with pytest.raises(sqlite3.IntegrityError, match='controlled batch interruption'):
            owner.step(job, batch_size=1)
        assert owner.stage(job, first.id) == first
        regular.migrate(job)
        with job:
            job.execute('DROP TRIGGER interrupt_checkpoint')
    store.settings.set_setting('ordinary-between-checkpoints', 'works')
    with connect(store) as job:
        while not (stage := owner.step(job, batch_size=1)).complete:
            pass
        assert stage.id == first.id
        with job:
            job.execute('CREATE TRIGGER interrupt_checkpoint_head BEFORE INSERT ON '+owner.table('head')+
                        " BEGIN SELECT RAISE(ABORT,'controlled selection interruption'); END")
        with pytest.raises(sqlite3.IntegrityError, match='controlled selection interruption'):
            owner.publish(job, stage=stage.id)
        assert owner.selected(job) is None
        assert not job.execute('SELECT 1 FROM '+owner.table('generations')).fetchone()
        with job:
            job.execute('DROP TRIGGER interrupt_checkpoint_head')
        owner.publish(job, stage=stage.id)


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_slow_verification_allows_real_write_and_regular_ddl_then_refuses_stale_selection(installed, monkeypatch):
    store, _ = installed
    seed(store)
    owner = publication.OWNERS['provider-refresh']
    with connect(store) as job:
        identity = ready(job, owner.name)
    entered, release = threading.Event(), threading.Event()
    describe = preparation.VALUES[owner.name]
    def held(conn, row, raw, digest):
        assert conn.in_transaction
        entered.set()
        assert release.wait(10)
        return describe(conn, row, raw, digest)
    monkeypatch.setitem(preparation.VALUES, owner.name, held)
    def publish():
        with connect(store) as job:
            owner.publish(job, stage=identity)
    with ThreadPoolExecutor(1) as executor:
        task = executor.submit(publish)
        try:
            assert entered.wait(5)
            with store._lock, store.conn:
                provider_frontiers.start(store.conn, 'known', 31, 'during-verification')
            with connect(store) as job:
                regular.migrate(job)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(MigrationError, match='reconciled preparation|changed during|changed after'):
            task.result(timeout=10)
    with connect(store) as job:
        assert owner.selected(job) is None


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_reference_change_after_verification_is_fenced_before_selection(installed, monkeypatch):
    store, _ = installed
    seed(store)
    owner = publication.OWNERS['provider-refresh']
    verify = publication.Publication.verify
    with connect(store) as job:
        identity = ready(job, owner.name)
        def raced(self, conn, stage):
            result = verify(self, conn, stage)
            with store._lock, store.conn:
                store.conn.execute("UPDATE enrichment_provider_config SET status='disabled' WHERE provider='known'")
            return result
        monkeypatch.setattr(publication.Publication, 'verify', raced)
        with pytest.raises(MigrationError, match='reconciled preparation|changed after'):
            owner.publish(job, stage=identity)
        assert owner.selected(job) is None
        assert not job.execute('SELECT 1 FROM '+owner.table('generations')).fetchone()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_separate_native_upgrade_preserves_custody_and_has_precedence(installed, tmp_path):
    store, _ = installed
    seed(store)
    # The historical conversion admits only existing references and one exact
    # spelling per native artist retry row. The additive publisher retains all.
    with store.conn:
        for name, suffix in (('fetch-history','unknown-release'), ('artist-images','01'),
                             ('artist-images','999'), ('provider-refresh','unknown')):
            store.conn.execute('DELETE FROM settings WHERE key=?', (preparation.PREFIXES[name]+suffix,))
    with connect(store) as job:
        saved = {}
        for name, owner in publication.OWNERS.items():
            identity = ready(job, name)
            owner.publish(job, stage=identity)
            saved[name] = [tuple(row) for row in job.execute('SELECT * FROM '+owner.table('rows')+' ORDER BY source')]
    upgraded = Store(tmp_path / 'native-checkpoints.db')
    try:
        store.conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS)
        upgraded.init_schema(defer_metadata=True)
        for name, owner in publication.OWNERS.items():
            assert owner.selected(upgraded.conn) is None
            assert [tuple(row) for row in upgraded.conn.execute('SELECT * FROM '+owner.table('rows')+' ORDER BY source')] == saved[name]
        assert provider_frontiers.read(upgraded.conn, 'known')['fingerprint'] == 'exact été'
        assert discovery_checkpoints.fetched(upgraded.conn, 'release')
        regular.migrate(upgraded.conn)
    finally:
        upgraded.conn.close()
