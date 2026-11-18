"""Shared-model pending visibility and exact SQL completion on a tiny owner."""
from contextlib import closing
import sqlite3
from threading import RLock
from types import SimpleNamespace

import pytest

from yt_playlist.core.db_diagnostics import DiagnosticConnection
from yt_playlist.repos import vibe_model_file_writes as writes
from yt_playlist.repos.vibe_models import VibeModelsRepo


@pytest.mark.parametrize('mismatch', ['id', 'generation', 'size'])
def test_wrong_model_cannot_complete_committed_pending_work(tmp_path, mismatch):
    with closing(sqlite3.connect(tmp_path / 'models.db', factory=DiagnosticConnection)) as conn:
        repo = VibeModelsRepo(SimpleNamespace(conn=conn, _lock=RLock()))
        identity = repo.reserve_file('a'*64, 50)
        with closing(sqlite3.connect(tmp_path / 'models.db')) as observer:
            assert observer.execute('SELECT id,file_status FROM vibe_model_file_writes').fetchone() == (identity, 'pending')
        assert repo.file_reference('a'*64) is None
        with pytest.raises(ValueError):
            repo.complete_file(identity+1 if mismatch == 'id' else identity,
                               'b'*64 if mismatch == 'generation' else 'a'*64,
                               51 if mismatch == 'size' else 50)
        assert repo.file_reference('a'*64) is None
        repo.complete_file(identity, 'a'*64, 50)
        assert repo.file_reference('a'*64) == identity
        writes.validate(conn)
