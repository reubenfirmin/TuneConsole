"""Move compiled findings to numerical inputs and bounded report owners."""
from pathlib import Path
import sqlite3

from yt_playlist.core import choice_learning_state, recommendation_reports
from .runner import MigrationError

# These are the finite v93 child owners, including the surface they may serve.
# Keep this numbered contract unchanged when adding a future learning owner.
CHILDREN = (
    ('choice_learning_parameters', None), ('choice_learning_exclusions', None),
    ('choice_mode_estimates', 'choice_bandit'),
    ('choice_group_estimates', 'group_choice_bandit'),
    ('choice_group_opportunities', 'group_choice_bandit'),
    ('choice_learning_group_roots', 'group_choice_bandit'),
    ('choice_interest_contexts', 'choice_interests'),
    ('choice_interest_features', 'choice_interests'),
    ('choice_interest_tracks', 'choice_interests'),
    ('choice_album_interests', 'choice_interests'),
    ('choice_album_track_days', 'choice_interests'),
)


def fences():
    """A head freezes its inputs; removing it allows atomic owner retirement."""
    for table, surface in CHILDREN:
        owner = (f" AND surface='{surface}'" if surface else
                 " AND surface IN ('choice_bandit','group_choice_bandit')")
        for event in ('INSERT', 'UPDATE'):
            yield (f'CREATE TRIGGER {table}_owner_{event.lower()} BEFORE {event} ON {table} WHEN NOT EXISTS('
                   f'SELECT 1 FROM choice_learning_publications WHERE id=NEW.publication_id{owner}) '
                   "BEGIN SELECT RAISE(ABORT,'Wrong choice learning owner'); END;")
        for event in ('INSERT', 'UPDATE', 'DELETE'):
            references = ('NEW',) if event == 'INSERT' else ('OLD',) if event == 'DELETE' else ('NEW', 'OLD')
            condition = ' OR '.join(f'publication_id={reference}.publication_id' for reference in references)
            yield (f'CREATE TRIGGER {table}_{event.lower()} BEFORE {event} ON {table} WHEN EXISTS('
                   f'SELECT 1 FROM choice_learning_heads WHERE {condition}) '
                   "BEGIN SELECT RAISE(ABORT,'Published choice learning is immutable'); END;")
    for event in ('UPDATE', 'DELETE'):
        yield (f'CREATE TRIGGER choice_learning_publications_{event.lower()} BEFORE {event} '
               'ON choice_learning_publications WHEN EXISTS(SELECT 1 FROM choice_learning_heads '
               "WHERE publication_id=OLD.id) BEGIN SELECT RAISE(ABORT,'Choice learning is retained'); END;")


def statements():
    for name in ('schema_choice_learning.sql', 'schema_recommendation_reports.sql'):
        pending = ''
        for line in Path(__file__).with_name(name).read_text().splitlines(True):
            pending += line
            if sqlite3.complete_statement(pending):
                yield pending
                pending = ''
        if pending.strip():
            raise MigrationError('Incomplete findings schema: ' + name)
    yield from fences()


def apply(conn, context):
    sources = []
    for surface in sorted(choice_learning_state.SURFACES):
        row = conn.execute('SELECT payload,built_at FROM rec_proposals WHERE surface=?', (surface,)).fetchone()
        if row:
            try:
                value = choice_learning_state.decode(surface, row[0])
                sources.append((surface, value, row[1]))
            except (ValueError, TypeError, OverflowError) as exc:
                raise MigrationError(f'Invalid {surface}; original learning state retained: {exc}') from exc
    reports = []
    for surface in sorted(recommendation_reports.SURFACES):
        row = conn.execute('SELECT payload,built_at FROM rec_proposals WHERE surface=?', (surface,)).fetchone()
        if row:
            try:
                reports.append(recommendation_reports.prepare(surface, row[0], row[1]))
            except (ValueError, TypeError, OverflowError) as exc:
                raise MigrationError(f'Invalid {surface}; original report retained: {exc}') from exc
    for sql in statements():
        conn.execute(sql)
    for surface, value, at in sources:
        choice_learning_state.write(conn, surface, value, at, historical=True)
        conn.execute('DELETE FROM rec_proposals WHERE surface=?', (surface,))
    for report in reports:
        recommendation_reports.publish(conn, report)
        conn.execute('DELETE FROM rec_proposals WHERE surface=?', (report.surface,))
