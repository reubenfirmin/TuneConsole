"""Disposable ordering owns disk files, bounded cursors and cancellation cleanup."""
from contextlib import nullcontext
from functools import partial
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

import pytest

from yt_playlist.core.activity import WorkClosing, cancellable_work
from yt_playlist.repos import group_ordering, home_selection
from yt_playlist.repos.group_ordering import GroupOrderingRepo
from yt_playlist.controllers.home_selection import HomeSelection
from yt_playlist.rec.home_selection_policy import CandidateFilter, query
from yt_playlist.repos.home_selection import HomeInputRepo, HomeSelectionRepo


@pytest.mark.parametrize('repo_type', [GroupOrderingRepo, HomeSelectionRepo, HomeInputRepo])
@pytest.mark.parametrize('exit_kind', ['complete', 'early', 'cancel'])
def test_private_disk_storage_closes_and_removes_files(tmp_path, monkeypatch, repo_type, exit_kind):
    owner = group_ordering if repo_type is GroupOrderingRepo else home_selection
    monkeypatch.setattr(owner, 'TemporaryDirectory', partial(TemporaryDirectory, dir=tmp_path))
    opened = {}

    def use_storage():
        with repo_type.open() as repo:
            conn = repo._conn
            path = Path(conn.execute('PRAGMA database_list').fetchone()[2])
            opened.update(conn=conn, path=path)
            assert path.is_file() and path.parent.parent == tmp_path
            assert conn.execute('PRAGMA cache_size').fetchone()[0] == -1024
            assert conn.execute('PRAGMA mmap_size').fetchone()[0] == 0
            if repo_type is GroupOrderingRepo:
                repo.add([(1, 'a', 'Artist', 'Album', 0, 0, .7)])
                rows = repo.ranked()
                assert next(rows)['key'] == 'a'
                rows.close()
            elif repo_type is HomeSelectionRepo:
                repo.add_candidates([(7, 1, 'a', 'Artist', '', 'jazz', 1, 2000, 1, 0, 1)])
                rows = repo.ranked(query(CandidateFilter(7)))
                assert next(rows)['key'] == 'a'
                rows.close()
            else:
                repo.add([('a', 'jazz', False)])
                assert repo.get(['a']) == {'a': ('jazz', False)}
            if exit_kind == 'early':
                return
            if exit_kind == 'cancel':
                raise WorkClosing('controlled cancellation')
            conn.commit()

    expected = pytest.raises(WorkClosing) if exit_kind == 'cancel' else nullcontext()
    with expected:
        use_storage()
    conn, path = opened['conn'], opened['path']
    assert not path.parent.exists() and not list(tmp_path.iterdir())
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        conn.execute('SELECT 1')


@pytest.mark.parametrize('repo_type', [GroupOrderingRepo, HomeSelectionRepo])
def test_ordered_cursor_checks_cancellation_at_the_next_page(repo_type):
    with repo_type.open() as repo:
        if repo_type is GroupOrderingRepo:
            repo.add([(i, str(i), '', '', 0, 0, .5) for i in range(300)])
            rows = repo.ranked()
        else:
            repo.add_candidates([(7, i, str(i), '', '', 'jazz', 1, 2000, 1, 0, 1)
                                 for i in range(300)])
            rows = repo.ranked(query(CandidateFilter(7)))
        assert [next(rows)['key'] for _ in range(128)] == [str(i) for i in range(128)]
        stopped = False
        with cancellable_work(lambda: stopped):
            stopped = True
            with pytest.raises(WorkClosing):
                next(rows)
            stopped = False
        # The cancelled iterator released its cursor; the repository is still usable.
        repo._conn.execute('DROP TABLE ' + ('ordering' if repo_type is GroupOrderingRepo else 'candidates'))


def test_home_filtering_steering_and_seen_keys_share_the_same_candidate_set():
    with HomeSelectionRepo.open() as repository:
        repo = HomeSelection(repository)
        repo.add_candidates([
            (7, 0, 'a', 'A', '', 'jazz', 1, 1980, 1, 0, 1),
            (7, 1, 'b', 'B', '', 'jazz', 1, 2000, 1, 0, 0),
            (7, 2, 'c', 'C', '', 'rock', 1, 2020, 1, .3, 1),
            (7, 3, 'd', 'D', '', 'jazz', 1, 2010, 2, -.5, 1),
            (7, 4, 'e', 'E', '', 'jazz', 1, 2005, 1, 0, 1),
            (7, 5, 'f', 'F', '', 'jazz', 1, 2020, 1, 0, 1),
        ])
        selected = CandidateFilter(7, year_band=1, years=(1990, 2010), include=('jazz',), eligible=True)
        repo.add_ppr([('mode', 'd', 2), ('mode', 'e', 3)])
        assert repo.count(selected) == 2
        assert [row['key'] for row in repo.ranked(selected)] == ['d', 'e']
        assert [row['key'] for row in repo.ranked(selected, mode='mode', ranker='ppr', scale=3)] == ['e', 'd']
        assert [row['key'] for row in repo.ranked(selected, ranker='cosine')] == ['d', 'e']
        repo.considered(1, selected)
        assert [tuple(row) for row in repo.considered_rows(1)] == [('d', -.5), ('e', 0)]
        assert repo.has_negative_ordering()
        repo.mark_seen(selected, ['f'])
        remaining = CandidateFilter(7, include=('jazz',), eligible=True, unseen=True)
        assert [row['key'] for row in repo.ranked(remaining)] == ['a']


def test_group_ordering_prefers_fit_bucket_then_taste_and_stable_pool_rank():
    with GroupOrderingRepo.open() as repo:
        repo.add([(0, 'weak-fit', '', '', 1, -10, .1),
                  (1, 'low-taste', '', '', 0, -.2, .8),
                  (3, 'later-tie', '', '', 0, -.9, .8),
                  (2, 'earlier-tie', '', '', 0, -.9, .8)])
        assert [row['key'] for row in repo.ranked()] == [
            'earlier-tie', 'later-tie', 'low-taste', 'weak-fit']
