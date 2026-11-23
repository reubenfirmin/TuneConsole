"""Check current Discover eligibility on a private installed library image."""
from contextlib import closing
import json
import sqlite3
import sys

from tests.library_image.fixtures import managed_database, report_file
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.core.store import Store
from yt_playlist.rec import home_prepared


def test_actual_discover_rechecks_exact_artist_mutes(managed_library):
    report = {'python': sys.version.split()[0], 'sqlite': sqlite3.sqlite_version, 'examples': []}
    managed_database(report=report)
    now = report['managed_library']['created_at']
    store = Store(managed_library)
    try:
        with closing(store.conn):
            # Exercise the captured Discovery publication and its installed
            # catalog layout without startup workers or schema changes.
            store._bind_repositories()
            store._snapshot_revisions = Revisions(store.conn)
            publication = home_prepared.load_discovery(store)
            assert publication
            with store.read_view() as reader:
                before = home_prepared.discovery(reader, None, now)
                examples = [(field, key, next(row for row in before[field]
                    if row.get('artist_id') is not None
                    and reader.artists.resolve_exact(row['artist']) != row['artist_id']))
                    for field, key in (('discovery_albums', 'browse_id'), ('discovery_artists', 'artist_id'))]

            for field, key, row in examples:
                store.rec_model.record_feedback('for_you', f'artist:@artist:{row["artist_id"]}', 'mute', now=now)
                with store.read_view() as reader:
                    after = home_prepared.discovery(reader, None, now)
                    remaining = {tile[key] for tile in after[field]}
                    report['examples'].append({'field': field, 'artist_id': row['artist_id'],
                        'name': row['artist'], 'title': row.get('title'), 'visible': len(remaining)})
                    assert row[key] not in remaining
                    assert remaining
                    assert home_prepared.load_discovery(reader) == publication
    finally:
        report_file('/tmp/tuneconsole-1497-artist-mutes.json').write_text(json.dumps(report, indent=2) + '\n')
