"""Explicit actual-library check of the runner's per-test workspace boundary."""
import pytest

from tests.library_image.fixtures import managed_database
from yt_playlist.core.store import Store


@pytest.mark.parametrize('value', ['first', 'second'])
def test_each_case_gets_fresh_sql_and_files(value):
    database = managed_database()
    if database is None:
        pytest.skip('Run through scripts/test-library.py')
    marker = database.parent / '.case-marker'
    assert not marker.exists(), 'A previous test left its private file here'
    store = Store(database)
    try:
        store._bind_repositories()
        assert store.conn.execute('SELECT count(*) FROM recordings').fetchone()[0] > 10000
        assert store.settings.get_setting('library_image_case_probe') is None
        store.settings.set_setting('library_image_case_probe', value)
        marker.write_text(value)
        reopened = Store(database)
        try:
            reopened._bind_repositories()
            assert reopened.settings.get_setting('library_image_case_probe') == value
            assert marker.read_text() == value
        finally:
            reopened.conn.close()
    finally:
        store.conn.close()
