"""The last released installed layout, before pending bounded changes."""
from yt_playlist.core.migrations import current, regular
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.migrations.runner import Context, RELEASE_CHANGES


def release_schema(conn):
    current.bootstrap(conn, MIGRATIONS, 1.)
    conn.execute(regular.HISTORY_SQL)
    for change in regular.CHANGES[:RELEASE_CHANGES]:
        change.apply(conn, Context(1.))
        change.validate(conn)
        conn.execute('INSERT INTO schema_changes VALUES (?,?,?)',
                     (change.version, change.name, 1.))
