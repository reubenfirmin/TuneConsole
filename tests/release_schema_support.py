"""The last released installed layout, before pending bounded changes."""
from yt_playlist.core.migrations import current, regular
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.migrations.runner import Context, RELEASE_CHANGES


def release_schema(conn, *, changes=RELEASE_CHANGES):
    current.bootstrap(conn, MIGRATIONS, 1.)
    conn.execute(regular.HISTORY_SQL)
    for change in regular.CHANGES[:changes]:
        change.apply(conn, Context(1.))
        change.validate(conn)
        conn.execute('INSERT INTO schema_changes VALUES (?,?,?)',
                     (change.version, change.name, 1.))


def future_schema(monkeypatch, name, sql):
    """A synthetic next release must ship its new definition with its change."""
    from copy import deepcopy
    from yt_playlist.core import destination_schema
    document = deepcopy(destination_schema.definition())
    document['regular_history'].append([len(regular.CHANGES) + 1, name])
    for key in ('installed_statements', 'destination_statements'):
        document[key].append(sql)
    monkeypatch.setattr(destination_schema, 'definition', lambda: document)
