from yt_playlist.core.migrations import migrate
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.store import Store
from tests.historical_support import upgrade_fixture


def test_upgrade_removes_only_the_retired_override():
    store = Store(':memory:')
    try:
        migrate(store.conn, registry=MIGRATIONS[:42])
        store.conn.executemany('INSERT INTO settings(key,value) VALUES (?,?)', [
            ('rec_param:palette_absence_penalty', '1.5'),
            ('rec_param:breadth_bias', '-0.5'),
            ('lastfm_api_key', 'keep'),
        ])
        store.conn.commit()
        upgrade_fixture(store)
        assert store.settings.get_setting('rec_param:palette_absence_penalty') is None
        assert store.settings.get_setting('rec_param:breadth_bias') == '-0.5'
        assert store.settings.get_setting('lastfm_api_key') == 'keep'
    finally:
        store.conn.close()
