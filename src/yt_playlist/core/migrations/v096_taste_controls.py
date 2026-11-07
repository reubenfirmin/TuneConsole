"""Retain taste recovery, ordered comparisons and experiment decisions by owner."""
from pathlib import Path
import sqlite3

from yt_playlist.core import experiment_decision_state, taste_control_state
from .runner import MigrationError

KEYS = frozenset({'taste_settings_checkpoint_v1','taste_comparison_baseline_v2','ppr_experiment_decisions'})


def fences():
    keys = ','.join("'"+key+"'" for key in sorted(KEYS))
    for event in ('INSERT','UPDATE'):
        yield (f'CREATE TRIGGER taste_control_settings_{event.lower()} BEFORE {event} ON settings '
               f'WHEN NEW.key IN ({keys}) BEGIN '
               "SELECT RAISE(ABORT,'Taste controls require their domain owner'); END")


def statements():
    pending = ''
    for line in Path(__file__).with_name('schema_taste_controls.sql').read_text().splitlines(True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ''
    if pending.strip():
        raise MigrationError('Incomplete taste control schema')
    yield from fences()


def apply(conn,context):
    sources = {key:value for key,value in conn.execute('SELECT key,value FROM settings') if key in KEYS}
    try:
        for sql in statements():
            conn.execute(sql)
        taste_control_state.put(conn,{key:raw for key,raw in sources.items() if key in taste_control_state.KEYS},legacy=True)
        experiment_decision_state.put(conn,{key:raw for key,raw in sources.items() if key in experiment_decision_state.KEYS},legacy=True)
        conn.executemany('DELETE FROM settings WHERE key=?',[(key,) for key in sources])
    except (ValueError,TypeError,KeyError,sqlite3.IntegrityError) as exc:
        raise MigrationError(f'Taste control conversion failed; original retained: {exc}') from exc
