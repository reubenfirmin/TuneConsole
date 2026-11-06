"""Ordinary reporting reads do not retain a SQLite transaction."""
import sqlite3
from contextlib import closing

import pytest

from yt_playlist.core.store import Store


@pytest.mark.parametrize('tracked', [False, True])
def test_report_reads_commits_between_queries_without_opening_a_transaction(file_backed_store, monkeypatch, tracked):
    store, statements = file_backed_store, []
    store.settings.set_setting('report-input', 'before')
    configure = Store._configure_connection

    def traced(reader):
        configure(reader)
        reader.conn.set_trace_callback(statements.append)

    monkeypatch.setattr(Store, '_configure_connection', traced)
    with store.read_view(tracked=tracked) as reader:
        assert not reader.conn.in_transaction
        assert reader.settings.get_setting('report-input') == 'before'
        assert reader.settings.get_settings_with_prefix('report-') == {'report-input': 'before'}
        with closing(sqlite3.connect(store._db_path)) as writer, writer:
            writer.execute("UPDATE settings SET value='after' WHERE key='report-input'")
        assert reader.settings.get_setting('report-input') == 'after'
        assert reader.settings.get_settings_with_prefix('report-') == {'report-input': 'after'}
        assert not reader.conn.in_transaction
    controls = [sql for sql in statements if sql.split()[0].upper() in
                {'BEGIN', 'SAVEPOINT', 'COMMIT', 'ROLLBACK', 'RELEASE'}]
    assert controls == []
