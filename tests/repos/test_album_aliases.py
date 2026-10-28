"""Exact source comparisons preserve editions, ownership, order and historical shares."""
from copy import deepcopy
import json
import threading

import pytest

from yt_playlist.core import album_aliases, albums
from yt_playlist.core.migrations import Migration, MigrationError, migrate
from yt_playlist.core.migrations import registry, v062_album_aliases
from yt_playlist.core.migrations.runner import validate_version
from yt_playlist.core.store import Store


OLD = 'MPREb_KUo26z3y6bY'
ALIAS = 'MPREb_JVmmhCojzaW'


def release():
    return {'title': 'Dying Surfer Meets His Maker', 'year': '2015',
            'artists': [{'name': 'All Them Witches'}], 'trackCount': 9,
            'tracks': [{'videoId': f'video-{i}', 'title': f'Track {i}',
                        'duration': '3:01', 'isExplicit': False} for i in range(9)]}


def test_exact_aliases_aggregate_once_and_preserve_accounts_playback_and_shares(store, tmp_path):
    payload = release()
    first = store.identities.upsert_identity('First', 'c', None, True)
    second = store.identities.upsert_identity('Second', 'c2', None, False)
    playlist = store.playlists.upsert_playlist(first, 'P', 'Mix', 1, 'h', 0)
    entry = store.tracks.upsert_track('video-0', 'Track 0', 'All Them Witches', payload['title'], 181,
                                    album_browse_id=OLD)
    store.tracks.upsert_track('video-0', 'Track 0', 'All Them Witches', payload['title'], 181,
                             album_browse_id=ALIAS)
    store.playlists.set_playlist_tracks(playlist, [entry])
    for bid, owner in ((OLD, first), (ALIAS, first), (ALIAS, second)):
        store.collection.add_saved_album({'browse': bid, 'title': payload['title'],
                                          'artist': 'All Them Witches'}, identity_id=owner)
    store.history.import_play_events(first, [('track 0|all them witches', 'video-0', 100)])
    before_revision = store.collection.saved_album_revision(first)
    with store.conn:
        store.conn.execute('INSERT INTO album_share_snapshots VALUES (?,?,?)', (ALIAS, 'Frozen', '["video-0"]'))
    old_id, alias_id = store.collection.album_id(OLD), store.collection.album_id(ALIAS)
    assert old_id != alias_id
    assert store.collection.observe_album_representation(OLD, payload, now=200)
    assert store.collection.observe_album_representation(ALIAS, payload, now=201)
    assert store.collection.album_id(ALIAS) == old_id
    assert store.collection.saved_album_ids() == {OLD, ALIAS}
    assert len(store.collection.get_saved_albums()) == 1
    assert len(store.collection.get_saved_albums(identity_id=second)) == 1
    owners = store.collection.saved_album_owners(ALIAS)[ALIAS]
    assert {row['identity_id'] for row in owners} == {first, second}
    album, = store.collection.artist_albums('All Them Witches')
    assert (album['songs'], album['plays'], album['n_pls'], album['saved']) == (1, 1, 1, True)
    assert store.collection.collection_artists()[0]['n_albums'] == 1
    assert store.collection.playback_source(video_id='video-0')['browse'] == OLD
    assert store.collection.playback_source(album_browse_id=ALIAS)['browse'] == ALIAS
    assert len(store.charts.album_tracks_detail(ALIAS)) == 1
    assert store.artists.album_credits(ALIAS)
    assert store.conn.execute('SELECT video_ids_json FROM album_share_snapshots WHERE browse_id=?',
                              (ALIAS,)).fetchone()[0] == '["video-0"]'
    archive = store.conn.execute("SELECT payload FROM identity_state_archive WHERE kind='album_alias_reconciliation'").fetchone()
    assert len(json.loads(archive[0])['album_library_memberships']) == 3
    assert store.collection.saved_album_revision(first) > before_revision
    # A remote snapshot admitted before reconciliation cannot erase the merged ownership.
    assert store.collection.replace_saved_album_snapshots({first: []}, expected_revisions={first: before_revision}) == set()
    with store.conn:
        store.conn.execute('DELETE FROM album_library_memberships WHERE identity_id=?', (first,))
    assert store.collection.saved_album_ids() == {OLD, ALIAS}
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    validate_version(store.conn, registry.MIGRATIONS[-1].version)


@pytest.mark.parametrize('difference', ['order', 'bonus', 'video', 'duration', 'explicit', 'year',
                                       'artist', 'partial', 'missing-duration', 'missing-explicit'])
def test_equal_titles_do_not_establish_aliases(store, difference):
    original, changed = release(), release()
    if difference == 'order':
        changed['tracks'].reverse()
    elif difference == 'bonus':
        changed['tracks'].append({**changed['tracks'][-1], 'videoId': 'bonus'})
        changed['trackCount'] = 10
    elif difference == 'video':
        changed['tracks'][0]['videoId'] = 'different-upload'
    elif difference == 'duration':
        changed['tracks'][0]['duration'] = '3:02'
    elif difference == 'explicit':
        changed['tracks'][0]['isExplicit'] = True
    elif difference == 'year':
        changed['year'] = '2016'
    elif difference == 'artist':
        changed['artists'] = [{'name': 'Another Artist'}]
    elif difference == 'partial':
        changed['continuation'] = 'unfinished'
    elif difference == 'missing-duration':
        changed['tracks'][0].pop('duration')
    else:
        changed['tracks'][0].pop('isExplicit')
    store.collection.add_saved_album({'browse': OLD, 'title': original['title'], 'artist': 'All Them Witches'})
    store.collection.add_saved_album({'browse': ALIAS, 'title': changed['title'], 'artist': 'All Them Witches'})
    store.collection.observe_album_representation(OLD, original)
    store.collection.observe_album_representation(ALIAS, changed)
    assert store.collection.album_id(OLD) != store.collection.album_id(ALIAS)


def test_historical_bonus_tracks_prevent_reconciliation_even_when_current_pages_match(store):
    payload = release()
    store.tracks.upsert_track('bonus', 'Surface-to-Air Whistle', 'All Them Witches', payload['title'], 181,
                             album_browse_id=OLD)
    store.collection.observe_album_representation(OLD, payload)
    store.collection.observe_album_representation(ALIAS, payload)
    assert store.collection.album_id(OLD) != store.collection.album_id(ALIAS)
    assert store.charts.album_tracks_detail(OLD)[0]['video_id'] == 'bonus'


def test_interrupted_reconciliation_rolls_back_all_memberships_and_identity_changes(store, monkeypatch):
    payload = release()
    for bid in (OLD, ALIAS):
        store.collection.add_saved_album({'browse': bid, 'title': payload['title'], 'artist': 'All Them Witches'})
    store.collection.observe_album_representation(OLD, payload)
    before = list(store.conn.iterdump())
    merge = album_aliases._merge

    def interrupted(*args):
        merge(*args)
        raise RuntimeError('interrupted reconciliation')

    monkeypatch.setattr(album_aliases, '_merge', interrupted)
    with pytest.raises(RuntimeError, match='interrupted reconciliation'):
        store.collection.observe_album_representation(ALIAS, payload)
    assert list(store.conn.iterdump()) == before
    assert store.collection.album_id(OLD) != store.collection.album_id(ALIAS)


def test_discovery_state_and_pending_tiles_resolve_either_alias_once(store):
    payload = release()
    for bid in (OLD, ALIAS):
        store.discovery.upsert_discovered_album(bid, 'All Them Witches', payload['title'], '2015', None, 1)
        store.collection.observe_album_representation(bid, payload)
    assert len(store.discovery.get_discovered_albums()) == 1
    tiles, _ = store.discovery.present_home_tiles([ALIAS], [])
    assert tiles[ALIAS]['title'] == payload['title']
    store.discovery.record_discovery_impressions('album', [OLD, ALIAS], 'same-view', 10)
    assert store.discovery.get_discovered_albums()[0]['offered_count'] == 1
    assert store.discovery.delete_discovered_albums([ALIAS]) == 1
    assert not store.discovery.get_discovered_albums()


def test_aliases_preserve_repeated_full_occurrences_and_each_frozen_share(store):
    from yt_playlist.library.album_catalog import remember_album
    payload = release()
    payload['tracks'][-1] = deepcopy(payload['tracks'][0])
    for bid in (OLD, ALIAS):
        remember_album(store, bid, payload)
    assert store.collection.album_id(OLD) == store.collection.album_id(ALIAS)
    expected = [row['videoId'] for row in payload['tracks']]
    # Fetched entries are catalog-only; folding them into library controls share readiness.
    with store.conn:
        store.conn.execute("INSERT OR IGNORE INTO library_memberships(entry_id,origin) "
                           "SELECT entry_id,'test' FROM album_tracks WHERE is_full=1")
    assert [row['video_id'] for row in store.charts.album_tracks_detail(ALIAS)] == expected
    assert store.collection.cached_album_share(OLD)['video_ids'] == expected
    assert store.collection.cached_album_share(ALIAS)['video_ids'] == expected
    changed = deepcopy(payload)
    changed['tracks'][0]['videoId'] = 'changed-edition'
    remember_album(store, ALIAS, changed)
    assert [row['video_id'] for row in store.charts.album_tracks_detail(OLD)] == expected
    changed['tracks'][0].pop('isExplicit')
    remember_album(store, ALIAS, changed)
    remember_album(store, OLD, changed)
    assert [row['video_id'] for row in store.charts.album_tracks_detail(OLD)] == expected


def test_preferred_id_can_use_full_cache_captured_under_its_verified_alias(store):
    from yt_playlist.library.album_catalog import remember_album
    payload = release()
    store.tracks.upsert_track('video-0', 'Track 0', 'All Them Witches', payload['title'], 181, album_browse_id=OLD)
    anonymous_comparison = deepcopy(payload)
    for track in anonymous_comparison['tracks']:
        track.pop('isExplicit')
    remember_album(store, ALIAS, anonymous_comparison)
    store.collection.observe_album_representation(OLD, payload)
    store.collection.observe_album_representation(ALIAS, payload)
    with store.conn:
        store.conn.execute("INSERT OR IGNORE INTO library_memberships(entry_id,origin) "
                           "SELECT entry_id,'test' FROM album_tracks WHERE is_full=1")
    assert store.collection.album_id(OLD) == store.collection.album_id(ALIAS)
    assert store.collection.is_album_tracklist_complete(OLD)
    assert store.collection.cached_album_share(OLD)['video_ids'] == [track['videoId'] for track in payload['tracks']]
    assert store.conn.execute('SELECT browse_id FROM album_share_snapshots').fetchone()[0] == ALIAS


@pytest.mark.parametrize('first', [OLD, ALIAS])
@pytest.mark.parametrize('changed', [False, True])
def test_concurrent_accepted_album_responses_publish_coherent_proofs_and_tracks(
        tmp_path, monkeypatch, first, changed):
    from yt_playlist.library.album_catalog import remember_album
    store = Store(tmp_path / 'responses.db')
    store.init_schema()
    second = ALIAS if first == OLD else OLD
    current, older = release(), release()
    if changed:
        older['tracks'][0]['videoId'] = 'older-edition-video'
    accepted, resume, attempted = threading.Event(), threading.Event(), threading.Event()
    errors, boundary = [], []
    real_observe = store.collection.observe_album_representation

    def observe(browse, payload, **kwargs):
        result = real_observe(browse, payload, **kwargs)
        if threading.current_thread() is old_worker:
            boundary.append(store._lock.held_by_current_thread)
            accepted.set()
            assert resume.wait(15)
        return result

    def remember(browse, payload, *, signal=False):
        try:
            if signal:
                attempted.set()
            remember_album(store, browse, payload)
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(store.collection, 'observe_album_representation', observe)
    old_worker = threading.Thread(target=remember, args=(first, older), daemon=True)
    new_worker = threading.Thread(target=remember, args=(second, current), kwargs={'signal': True}, daemon=True)
    old_worker.start()
    try:
        assert accepted.wait(15)
        new_worker.start()
        assert attempted.wait(15)
        assert boundary == [True]
    finally:
        resume.set()
        old_worker.join(15)
        if new_worker.ident is not None:
            new_worker.join(15)
    try:
        assert not old_worker.is_alive() and not new_worker.is_alive() and not errors
        assert (store.collection.album_id(first) == store.collection.album_id(second)) is (not changed)
        for browse, payload in ((first, older), (second, current)):
            digest, videos = album_aliases.fingerprint(payload)
            assert store.conn.execute('SELECT digest FROM album_provider_fingerprints '
                "WHERE provider='youtube' AND provider_id=?", (browse,)).fetchone()[0] == digest
            album_id = store.collection.album_id(browse)
            actual = [row[0] for row in store.conn.execute('SELECT e.video_id FROM album_tracks t '
                'JOIN catalog_entries e ON e.id=t.entry_id WHERE t.album_id=? AND t.is_full=1 '
                'ORDER BY t.position,t.id', (album_id,))]
            assert actual == videos
            assert json.loads(store.conn.execute('SELECT video_ids_json FROM album_share_snapshots '
                'WHERE browse_id=?', (browse,)).fetchone()[0]) == videos
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        store.conn.close()


def test_populated_upgrade_rolls_back_then_restarts_without_changing_source_ids(tmp_path, monkeypatch):
    path = tmp_path / 'installed.db'
    installed = Store(path)
    migrate(installed.conn, registry=registry.MIGRATIONS[:61])
    with installed.conn:
        albums.save(installed.conn, {'browse': OLD, 'title': 'Album', 'artist': 'Artist'})
        installed.conn.execute('INSERT INTO album_share_snapshots VALUES (?,?,?)', (OLD, 'Frozen', '["source"]'))
    before = list(installed.conn.iterdump())
    alias_migration = registry.MIGRATIONS[61]

    def fail(conn, context):
        v062_album_aliases.apply(conn, context)
        raise RuntimeError('interrupted alias upgrade')

    with monkeypatch.context() as patch:
        patch.setattr(registry, 'MIGRATIONS', (*registry.MIGRATIONS[:61], Migration(alias_migration.version, alias_migration.name, fail)))
        with pytest.raises(MigrationError, match='interrupted alias upgrade'):
            migrate(installed.conn)
    assert list(installed.conn.iterdump()) == before
    installed.init_schema()
    assert installed.collection.saved_album_ids() == {OLD}
    assert installed.conn.execute('SELECT is_primary FROM album_provider_ids').fetchone()[0] == 1
    assert installed.conn.execute('SELECT video_ids_json FROM album_share_snapshots').fetchone()[0] == '["source"]'
    state = list(installed.conn.iterdump())
    installed.conn.close()
    reopened = Store(path)
    try:
        reopened.init_schema()
        assert list(reopened.conn.iterdump()) == state
        validate_version(reopened.conn, registry.MIGRATIONS[-1].version)
    finally:
        reopened.conn.close()
    assert not (tmp_path / 'backups').exists()


def test_verified_aliases_survive_restart_without_repeating_reconciliation(tmp_path):
    path = tmp_path / 'aliases.db'
    store = Store(path)
    store.init_schema()
    payload = release()
    for bid in (OLD, ALIAS):
        store.collection.observe_album_representation(bid, payload, now=100)
    canonical = store.collection.album_id(OLD)
    store.conn.close()
    reopened = Store(path)
    try:
        reopened.init_schema()
        assert reopened.collection.album_id(ALIAS) == canonical
        reopened.collection.observe_album_representation(ALIAS, payload, now=200)
        assert reopened.conn.execute('SELECT COUNT(*) FROM album_alias_reconciliations').fetchone()[0] == 1
        assert reopened.conn.execute('SELECT COUNT(*) FROM album_provider_fingerprints').fetchone()[0] == 2
        assert reopened.conn.execute('SELECT COUNT(*) FROM album_primary_provider_ids').fetchone()[0] == 1
        assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        reopened.conn.close()
