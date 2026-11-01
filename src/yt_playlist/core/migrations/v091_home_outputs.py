"""Give last-good Home output a bounded file publication and explicit roots."""
from pathlib import Path
import sqlite3

from yt_playlist.core import home_outputs
from .runner import MigrationError


def statements():
    pending = ''
    for line in Path(__file__).with_name('schema_home_outputs.sql').read_text().splitlines(True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ''
    if pending.strip():
        raise MigrationError('Incomplete Home output schema')


def apply(conn, context):
    plans = []
    for surface in sorted(home_outputs.SURFACES):
        row = conn.execute('SELECT payload,built_at FROM rec_proposals WHERE surface=?', (surface,)).fetchone()
        if row:
            try:
                plan = home_outputs.prepare(surface, *row)
                home_outputs.policy_references(plan.value)
                plans.append(plan)
            except (ValueError, TypeError, KeyError, OverflowError) as exc:
                raise MigrationError(f'Invalid {surface}; original display retained: {exc}') from exc
    for sql in statements():
        conn.execute(sql)
    for plan in plans:
        home_outputs.publish(conn, plan, historical=True)
        conn.execute('DELETE FROM rec_proposals WHERE surface=?', (plan.surface,))
