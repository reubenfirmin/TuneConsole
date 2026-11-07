"""Continuing writes through real installed layouts and their TEMP read views."""
from contextlib import closing
import sqlite3

import pytest

from yt_playlist.core import entity_names
from yt_playlist.core.config import AccountConfig
from yt_playlist.core.migrations import runner
from yt_playlist.core.store import Store


@pytest.fixture
def admitted_installed(request, tmp_path, _admitted_installed_schema):
    """Private publication input after real admission, without repeating startup.

    The installed fixture below retains reopen/admission ownership. Publication
    consumers retain their actual SQL, files, revision guards and final format
    validation; no mutable Store or file is shared between cases.
    """
    version = request.param
    template, revisions = _admitted_installed_schema(version)
    store = Store(tmp_path / 'installed.db')
    try:
        template.restore(store)
        for name in ('_schema_compatibility', '_rebuild_source_version', '_schema_recovery_pending'):
            setattr(store, name, getattr(template.source, name))
        assert store._rebuild_source_version is None
        store._bind_repositories()
        assert store.conn.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower() == 'wal'
        store._snapshot_revisions = revisions.bind_revisions(store.conn)
        history = [tuple(row) for row in store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')]
        yield store, version
        assert [tuple(row) for row in store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')] == history
        with closing(sqlite3.connect(store._db_path.as_uri() + '?mode=ro', uri=True)) as reader:
            reader.row_factory = sqlite3.Row
            runner.validate_version(reader, version)
    finally:
        store.conn.close()


@pytest.fixture(params=[79, 93, 102, 107])
def installed(request, tmp_path, _pristine_installed_schema):
    path = tmp_path / 'installed.db'
    setup = Store(path)
    try:
        _pristine_installed_schema(request.param).backup(setup.conn)
        setup.conn.execute('PRAGMA journal_mode=WAL')
    finally:
        setup.conn.close()
    store = Store(path)
    try:
        store.init_schema(defer_metadata=True)
        history = [tuple(row) for row in store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')]
        yield store, request.param
        assert [tuple(row) for row in store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')] == history
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as reader:
            reader.row_factory = sqlite3.Row
            runner.validate_version(reader, request.param)
    finally:
        store.conn.close()


@pytest.mark.parametrize('admitted_installed', [79, 93, 107], indirect=True)
def test_admitted_publication_inputs_keep_pinned_reads_and_real_write_guards(
        admitted_installed, _admitted_installed_schema):
    store, version = admitted_installed
    template, _ = _admitted_installed_schema(version)
    assert store.settings.get_setting('private-publication-input') is None
    store.settings.set_setting('private-publication-input', 'original')
    with store.read_snapshot() as snapshot:
        assert snapshot.store.settings.get_setting('private-publication-input') == 'original'
        store.settings.set_setting('private-publication-input', 'changed')
        assert snapshot.store.settings.get_setting('private-publication-input') == 'original'
        assert not snapshot.publish(lambda: pytest.fail('Published a changed input'))
    with store.read_snapshot() as snapshot:
        assert snapshot.store.settings.get_setting('private-publication-input') == 'changed'
        assert snapshot.publish(lambda: None)
    assert template.source.settings.get_setting('private-publication-input') is None


def test_account_reconciliation_keeps_physical_ids_and_rolls_back(installed):
    store, version = installed
    main = store.accounts.upsert_account('Main', 'main.json', None, True)
    brand = store.accounts.upsert_account('Brand', 'brand.json', 'remote-brand', False)
    configs = [AccountConfig('Brand', 'main.json', None, True, main),
               AccountConfig('Main', 'brand.json', 'remote-brand', False, brand)]
    before = store.accounts.get_accounts()
    table = 'identities' if version < 103 else 'accounts'
    store.conn.execute(f"CREATE TEMP TRIGGER fail_account_update BEFORE UPDATE OF label ON {table} "
                       "WHEN NEW.label='Brand' BEGIN SELECT RAISE(ABORT,'publication interrupted'); END")
    with pytest.raises(sqlite3.IntegrityError, match='publication interrupted'):
        store.accounts.replace_accounts(configs)
    assert store.accounts.get_accounts() == before
    store.conn.execute('DROP TRIGGER fail_account_update')
    assert store.accounts.replace_accounts(configs) == {'Brand': main, 'Main': brand}
    assert store.accounts.get_master_account().id == main
    with pytest.raises(ValueError, match='retargeted'):
        store.accounts.upsert_account('Brand', 'new.json', 'another-remote-account', True)
    assert entity_names.owner(store.conn, 'accounts').table == 'main.' + table
    assert [tuple(row) for row in store.conn.execute(f'SELECT id,label FROM main.{table} ORDER BY id')] == [
        (main, 'Brand'), (brand, 'Main')]


def test_playlist_sync_and_curation_keep_memberships_accounts_and_revisions(installed):
    store, _ = installed
    accounts = [store.accounts.upsert_account('Main', 'main.json', None, True),
                store.accounts.upsert_account('Other', 'other.json', None, False)]
    song = store.tracks.upsert_track('upload', 'Song', 'Artist', None, 123)
    memberships = [store.playlists.upsert_playlist(account, 'LM', 'Liked Music', 0, '', 1)
                   for account in accounts]
    assert len(set(memberships)) == 2
    for account, membership in zip(accounts, memberships):
        revision = store.playlists.playlist_revision(account, 'LM')
        result = store.playlists.publish_playlist_snapshot(account, 'LM', 'Updated', [song], 2,
                                                          expected_revision=revision)
        assert result is not None
        playlist = store.playlists.get_playlist(membership)
        assert playlist.account_id == account and playlist.provider_playlist_id == 'LM'
        assert playlist.playlist_scope_id == f'@account:{account}:LM'
        assert [row[0] for row in store.conn.execute('SELECT track_id FROM playlist_tracks WHERE playlist_id=?',
                                                   (membership,))] == [song]
        store.playlists.set_playlist_group('LM', 'Favorites', account_id=account)
        store.playlists.hide_playlist('LM', account_id=account)
        assert f'@account:{account}:LM' in store.playlists.get_hidden_playlists()
        store.playlists.unhide_playlist('LM', account_id=account)
        assert f'@account:{account}:LM' not in store.playlists.get_hidden_playlists()
        store.playlists.set_playlist_title(membership, 'Renamed', 3)
        assert store.playlists.get_playlist(membership).title == 'Renamed'
    with pytest.raises(ValueError, match='Choose an account'):
        store.playlists.playlist_reference('LM')
    store.overlaps.suppress_overlap('First', 'Second', 4)
    assert store.overlaps.get_suppressed_overlap_pairs() == {frozenset(('First', 'Second'))}
    store.overlaps.unsuppress_overlap('First', 'Second')
    assert not store.overlaps.get_suppressed_overlap_pairs()
    store.overlaps.keep_overlap_pair('First', 'Second', 5)
    assert store.overlaps.get_overlap_kept_pairs() == {frozenset(('First', 'Second'))}
    store.overlaps.ignore_cleanup('First', 'empty', 6)
    assert store.overlaps.get_cleanup_ignored() == {'empty': {'First'}}
    store.overlaps.unignore_cleanup('First', 'empty')
    assert not store.overlaps.get_cleanup_ignored()
    store.overlaps.ignore_merge('ignored-argument', ['First', 'Second'], 7)
    assert store.overlaps.get_ignored_merge_sigs() == {'First|Second'}


def test_ratings_and_raw_player_events_keep_upsert_order_and_inserted_ids(installed):
    store, _ = installed
    account = store.accounts.upsert_account('Main', 'main.json', None, True)
    store.tracks.upsert_track('upload', 'Song', 'Artist', None, 123)
    store.tracks.record_item_rating(account, 'upload', 'LIKE', source='api', now=10)
    store.tracks.record_item_rating(account, 'upload', 'DISLIKE', source='sync', now=9)
    store.tracks.record_item_rating(account, 'upload', 'LIKE', source='sync', now=11)
    assert store.tracks.retained_rating_statuses(account, ['upload']) == {'upload': 'LIKE'}
    row, = store.conn.execute('SELECT source,observed_at FROM item_ratings').fetchall()
    assert tuple(row) == ('api', 10)
    ids = [store.player_events.record_player_event(account, 'next', 'upload', 3, 123, None, '{}', at)
           for at in (12, 13)]
    assert ids[1] > ids[0]
    assert [row[0] for row in store.conn.execute('SELECT id FROM main.player_events ORDER BY id')] == ids
    with store.read_view() as reader:
        assert [row['account_id'] for row in reader.player_events.player_events_since(12)] == [account, account]
