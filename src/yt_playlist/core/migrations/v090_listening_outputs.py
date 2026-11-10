"""Move listening display editions and their exact chunk index out of the bucket."""
from pathlib import Path
import sqlite3

from yt_playlist.core import listening_outputs
from .runner import MigrationError


def statements():
    pending = ''
    for line in Path(__file__).with_name('schema_listening_outputs.sql').read_text().splitlines(True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ''
    if pending.strip():
        raise MigrationError('Incomplete listening output schema')


def apply(conn, context):
    rows = {surface: (raw, at) for surface, raw, at in conn.execute(
        "SELECT surface,payload,built_at FROM rec_proposals WHERE surface IN ('listening_analytics','listening_edition') "
        "OR substr(surface,1,16)='listening_chunk:'")}
    plan = None
    if rows:
        try:
            plan = listening_outputs.prepare({key.removeprefix('listening_chunk:'): value[0]
                    for key, value in rows.items() if key.startswith('listening_chunk:')},
                rows.get('listening_analytics', (None, None))[0],
                rows.get('listening_edition', (None, None))[0],
                rows.get('listening_analytics', (None, None))[1], historical=True,
                output_times={key: value[1] for key, value in rows.items()})
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            raise MigrationError(f'Invalid listening output; original retained: {exc}') from exc
    for sql in statements():
        conn.execute(sql)
    if plan:
        listening_outputs.publish(conn, plan)
        conn.executemany('DELETE FROM rec_proposals WHERE surface=?', [(key,) for key in rows])
