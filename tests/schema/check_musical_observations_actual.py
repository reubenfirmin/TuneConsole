"""Compare native source selections on a private installed-library copy."""
from contextlib import closing
import json
import sqlite3

import pytest

from tests.library_image.fixtures import managed_database, report_file
from yt_playlist.core.store import Store


def test_installed_musical_source_selection():
    report = {}
    path = managed_database(report=report)
    if path is None:
        pytest.skip('Requires a private managed library snapshot')
    sources = ('SELECT source_key,provider,scope,entity_id,revision,pending_position,pending_cursor,'
               'musical_profile_version,musical_profile_digest,musical_profile_prepared_at,musical_profile_publication '
               'FROM rec_musical_sources ORDER BY source_key')
    observations = ('SELECT id,delivery_id,position,field,kind,unit '
                    'FROM native_metadata_observations ORDER BY id')
    with closing(sqlite3.connect(path)) as conn:
        previous = list(conn.execute('SELECT source_key,field,observation_id '
                                     'FROM musical_source_observations ORDER BY 1,2'))
        source_rows = list(conn.execute(sources))
        original_observations = list(conn.execute(observations))

    for _ in range(2):
        store = Store(path)
        with closing(store.conn) as conn:
            store.init_schema()
            assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='musical_source_observations'").fetchone()
            assert [tuple(row) for row in conn.execute(
                'SELECT s.source_key,f.key,f.atom FROM rec_musical_sources s '
                'JOIN json_each(s.selected_observations) f ORDER BY 1,2')] == previous
            assert [tuple(row) for row in conn.execute(sources)] == source_rows
            assert [tuple(row) for row in conn.execute(observations)] == original_observations
            assert not conn.execute('PRAGMA foreign_key_check').fetchone()

    report.update(selected_links=len(previous), source_rows=len(source_rows),
                  historical_observations=len(original_observations), reopened=True, foreign_key_errors=0)
    report_file('musical-source-observations.json').write_text(json.dumps(report, indent=2) + '\n')
