"""Callback failures must still close the SQLite connection and its cursors."""
import sqlite3

import pytest

from yt_playlist.core import db_handles


def test_close_finishes_all_lifetimes_before_reporting_a_callback_error(tmp_path):
    connection = db_handles.connect(tmp_path / 'library.db')
    cursor = connection.execute('SELECT 1')
    calls = []

    def failed_reader():
        assert connection.execute('SELECT 2').fetchone() == (2,)
        calls.append('failed reader')
        raise RuntimeError('controlled retirement failure')

    def other_reader():
        assert connection.execute('SELECT 3').fetchone() == (3,)
        calls.append('other reader')

    def file_cleanup():
        assert connection._handle_closed
        calls.append('file')

    connection._before_close_callbacks.extend([failed_reader, other_reader])
    connection._close_callbacks.append(file_cleanup)
    with pytest.raises(RuntimeError, match='controlled retirement failure'):
        connection.close()
    assert calls == ['failed reader', 'other reader', 'file']
    assert connection._handle_closed
    assert not connection._before_close_callbacks and not connection._close_callbacks
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        cursor.fetchone()
    connection.close()
