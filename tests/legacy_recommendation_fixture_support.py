"""Published current-format inputs for recommendation consumers."""
from contextlib import closing

import pytest

from tests.recommendation_support import seed_compact_group_history
from tests.store_fixture_support import initialize_consumer
from yt_playlist.core.store import Store


@pytest.fixture
def recommendations(tmp_path, _pristine_current_schema):
    store = Store(tmp_path / 'recommendations.db')
    with closing(store.conn):
        initialize_consumer(store, _pristine_current_schema)
        seed_compact_group_history(store)
        yield store
