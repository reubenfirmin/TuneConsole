"""Verify the connection ownership guaranteed by the lookup_read boundary."""
from concurrent.futures import ThreadPoolExecutor
from tests.derived_rating_support import seed_dislike
from contextlib import ExitStack
import sqlite3
import threading

import pytest

from yt_playlist.core.activity import WorkClosing, cancellable_work
from yt_playlist.repos.base import Repo, lookup_read


def assert_lock_owned(lock, expected):
    # Both lock types are reentrant. Acquisition by this thread plus exclusion
    # of a different thread proves ownership without a lock-specific property.
    assert lock.acquire(blocking=False)
    lock.release()

    def available():
        acquired = lock.acquire(blocking=False)
        if acquired:
            lock.release()
        return acquired

    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(available).result(timeout=2) is (not expected)


class LookupProbe(Repo):
    @lookup_read
    def value(self, observed, failure=None):
        assert_lock_owned(self._lock, True)
        observed.append(self._store)
        value = self.conn.execute("SELECT value FROM settings WHERE key='probe'").fetchone()[0]
        if failure is not None:
            raise failure('controlled method failure')
        return value


def test_composed_read_keeps_its_original_snapshot_after_an_independent_commit(file_backed_store):
    store = file_backed_store
    store.settings.set_setting('probe', 'original')
    try:
        with store.read_snapshot() as snapshot:
            outer = snapshot.store
            assert outer.settings.get_setting('probe') == 'original'
            store.settings.set_setting('probe', 'new commit')
            with outer.read_view() as nested:
                assert nested.settings.get_setting('probe') == 'original'
            # Closing the nested composition must leave its enclosing view usable.
            assert outer.settings.get_setting('probe') == 'original'
        assert store.settings.get_setting('probe') == 'new commit'
    finally:
        store.conn.close()


def test_reporting_composition_reads_new_commits_without_a_transaction(file_backed_store):
    store = file_backed_store
    store.settings.set_setting('probe', 'original')
    with store.read_view() as outer:
        assert outer.settings.get_setting('probe') == 'original'
        assert not outer.conn.in_transaction
        store.settings.set_setting('probe', 'new commit')
        with outer.read_view() as nested:
            assert nested is outer
            assert nested.settings.get_setting('probe') == 'new commit'
            assert not nested.conn.in_transaction
        assert outer.settings.get_setting('probe') == 'new commit'


@pytest.mark.parametrize('route', ['memory', 'available', 'owned', 'private'])
@pytest.mark.parametrize('failure', [None, ValueError, WorkClosing])
def test_lookup_owns_its_connection_and_releases_on_every_exit(request, route, failure):
    store = request.getfixturevalue('store' if route == 'memory' else 'file_backed_store')
    store.settings.set_setting('probe', 'committed')
    observed = []
    try:
        with ExitStack() as stack:
            owner = store
            if route == 'private':
                owner = stack.enter_context(store.read_view())
            if route == 'owned':
                stack.enter_context(store._lock)
                store.conn.execute("UPDATE settings SET value='uncommitted' WHERE key='probe'")
                stack.callback(store.conn.rollback)
            if failure is None:
                assert LookupProbe(owner).value(observed) == (
                    'uncommitted' if route == 'owned' else 'committed')
            else:
                with pytest.raises(failure, match='controlled method failure'):
                    LookupProbe(owner).value(observed, failure)
            assert observed == [owner]
            assert_lock_owned(owner._lock, route == 'owned')
        assert_lock_owned(store._lock, False)
        if route == 'private':
            with pytest.raises(sqlite3.ProgrammingError, match='closed'):
                owner.conn.execute('SELECT 1')
        assert store.settings.get_setting('probe') == 'committed'
        with pytest.raises(WorkClosing), cancellable_work(lambda: True):
            LookupProbe(store).value(observed)
        assert len(observed) == 1, 'Cancellation must happen before entering the method'
        assert_lock_owned(store._lock, False)
    finally:
        store.conn.close()


@pytest.mark.parametrize('failure', [None, ValueError, WorkClosing])
def test_busy_lookup_owns_private_connection_and_closes_it_on_every_exit(file_backed_store, failure):
    store = file_backed_store
    store.settings.set_setting('probe', 'committed')
    entered, release = threading.Event(), threading.Event()
    observed = []

    def writer():
        with store._lock:
            store.conn.execute("UPDATE settings SET value='uncommitted' WHERE key='probe'")
            try:
                entered.set()
                assert release.wait(5), 'Lookup waited for the serving connection'
            finally:
                store.conn.rollback()

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(writer)
            try:
                assert entered.wait(2)
                if failure is None:
                    assert LookupProbe(store).value(observed) == 'committed'
                else:
                    with pytest.raises(failure, match='controlled method failure'):
                        LookupProbe(store).value(observed, failure)
                assert not pending.done(), 'The private read must finish while publication is active'
                assert len(observed) == 1
                reader = observed[0]
                assert reader is not store and reader._read_only
                assert_lock_owned(reader._lock, False)
                with pytest.raises(sqlite3.ProgrammingError, match='closed'):
                    reader.conn.execute('SELECT 1')
            finally:
                release.set()
            pending.result(timeout=2)
        assert_lock_owned(store._lock, False)
        assert store.settings.get_setting('probe') == 'committed'
    finally:
        store.conn.close()


@pytest.mark.parametrize('operation', [
    'impression_counts', 'pick_counts', 'muted_album_ids', 'video_tracks_detail', 'get_playlist',
    'disliked_identity_keys', 'disliked_key_page', 'identity_key_for_video',
])
def test_home_reads_keep_owned_changes_and_finish_before_a_busy_writer(file_backed_store, operation):
    store = file_backed_store
    entered, release = threading.Event(), threading.Event()
    try:
        if operation in ('impression_counts', 'pick_counts'):
            from tests.mode_fixtures import seed_modes

            seed_modes(store, 7)
            if operation == 'impression_counts':
                store.modes.log_impressions(1, [('wheelhouse', 7)], 100)
                read = lambda: store.modes.impression_counts(50, before=200)
                change = ('INSERT INTO rec_mode_impressions(epoch,lane,mode_id,created_at) '
                          'VALUES (?,?,?,?)', (2, 'wheelhouse', 7, 101))
            else:
                store.modes.log_pick(101, 7, 100)
                store.conn.execute('INSERT INTO playlist_membership_refs(id) VALUES (102)')
                read = lambda: store.modes.pick_counts(50, before=200)
                change = ('INSERT INTO rec_mode_picks(membership_ref_id,mode_id,created_at) '
                          'VALUES (102,7,101)', ())
            committed, uncommitted = {7: 1}, {7: 2}
        elif operation in ('disliked_identity_keys', 'disliked_key_page'):
            keys = []
            for index in range(5):
                video = f'disliked-{index}'
                store.tracks.upsert_track(video, f'Song {index}', 'Artist', None, 100)
                keys.append(store.tracks.identity_key_for_video(video))
            keys.sort()
            for key in (keys[0], keys[1], keys[3], keys[4]):
                seed_dislike(store, key, until=200, now=100)
            # A second surface must not consume a distinct-key page slot, and
            # other feedback must not enter either dislike projection.
            store.rec_model.record_feedback('radio', keys[1], 'dislike', scope='other',
                                            until=200, now=101)
            store.rec_model.record_feedback('for_you', keys[2], 'dismiss', now=101)
            change = ("UPDATE rec_feedback SET kind='dismiss' WHERE item_key=? AND kind='dislike'",
                      (keys[1],))
            if operation == 'disliked_identity_keys':
                read = store.rec_model.disliked_identity_keys
                committed = {keys[0], keys[1], keys[3], keys[4]}
                uncommitted = {keys[0], keys[3], keys[4]}
            else:
                read = lambda: store.rec_model.disliked_key_page(after=keys[0], limit=2)
                committed, uncommitted = [keys[1], keys[3]], [keys[3], keys[4]]
        elif operation == 'identity_key_for_video':
            observed = store.tracks.upsert_track('observed-upload', 'First song', 'Artist', None, 100)
            other = store.tracks.upsert_track('other-upload', 'Other song', 'Artist', None, 100)
            original = store.tracks.catalog_subject(observed)
            target = store.tracks.catalog_subject(other)
            assert original['recording_id'] != target['recording_id']
            assert original['identity_key'] != target['identity_key']
            read = lambda: (store.tracks.identity_key_for_video('observed-upload'),
                            store.tracks.identity_key_for_video('unknown-upload'))
            item_change = ('UPDATE youtube_items SET recording_id=? WHERE video_id=?',
                           (target['recording_id'], 'observed-upload'))
            change = ('UPDATE catalog_entries SET recording_id=? WHERE id=?',
                      (target['recording_id'], observed))
            committed = (original['identity_key'], None)
            uncommitted = (target['identity_key'], None)
        elif operation == 'muted_album_ids':
            artist = store.artists.ensure_artist('Muted', provider='youtube', provider_id='UCmuted')
            store.discovery.upsert_discovered_album('B', 'Muted', 'Record', None, None, 1,
                                                   artist_id=artist)
            store.rec_model.record_feedback('for_you', f'artist:@artist:{artist}', 'mute',
                                            now=1, until=90)
            read = lambda: store.rec_model.muted_album_ids(['B', 'B', 'absent'], 100)
            change = ("UPDATE rec_feedback SET until=150 WHERE artist_id=? AND kind='mute'", (artist,))
            committed, uncommitted = set(), {'B'}
        elif operation == 'video_tracks_detail':
            store.tracks.upsert_track('v1', 'First', 'Artist', None, 100, thumbnail='/committed.png')
            store.tracks.upsert_track('v2', 'Second', 'Artist', None, 100, thumbnail='/second.png')
            read = lambda: [(row['video_id'], row['thumbnail']) for row in
                            store.charts.video_tracks_detail(['v2', 'absent', 'v1', 'v2'])]
            change = ("UPDATE youtube_items SET thumbnail='/uncommitted.png' WHERE video_id='v1'", ())
            committed = [('v2', '/second.png'), ('v1', '/committed.png'), ('v2', '/second.png')]
            uncommitted = [('v2', '/second.png'), ('v1', '/uncommitted.png'), ('v2', '/second.png')]
        else:
            identity = store.accounts.upsert_account('Main', 'bridge', None, True)
            playlist = store.playlists.upsert_playlist(identity, 'P', 'Committed', 0, '', 1)

            def read():
                row = store.playlists.get_playlist(playlist)
                return row.title, row.account_id, row.provider_playlist_id

            change = ("UPDATE playlists SET title='Uncommitted' WHERE id=?", (playlist,))
            committed = ('Committed', identity, 'P')
            uncommitted = ('Uncommitted', identity, 'P')

        def mutate():
            if operation == 'identity_key_for_video':
                # Preserve the deferred composite upload/recording reference,
                # matching the association owner's update order without commit.
                store.conn.execute(*item_change)
            store.conn.execute(*change)
            if operation == 'identity_key_for_video':
                assert store.conn.execute('PRAGMA foreign_key_check').fetchone() is None

        store.conn.commit()
        assert read() == committed

        # A caller that owns the write transaction must keep its own view.
        with store._lock:
            mutate()
            try:
                assert read() == uncommitted
                assert store.conn.in_transaction
            finally:
                store.conn.rollback()
        assert read() == committed

        def writer():
            with store._lock:
                mutate()
                try:
                    entered.set()
                    assert release.wait(10), 'Foreground read waited for the writer'
                finally:
                    store.conn.rollback()

        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(writer)
            try:
                assert entered.wait(2)
                assert pool.submit(read).result(timeout=2) == committed
                assert not pending.done() and not release.is_set()
            finally:
                release.set()
            pending.result(timeout=2)
        assert read() == committed
    finally:
        store.conn.close()
