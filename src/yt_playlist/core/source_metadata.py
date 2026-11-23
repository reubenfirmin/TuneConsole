"""Maintain the effective fields owned by a single upload or unresolved import."""
from . import catalog_metadata, metadata_choice_selections, runtime_metadata_choices, track_sources
from .metadata_contract import ACTIVE_SQL, PREDECESSOR_ACTIVE_SQL, with_genre_keys


def _refresh(entry):
    values = catalog_metadata._refresh_values(source=True).replace('NEW.id', entry).replace('NEW.video_id',
        f'(SELECT video_id FROM track_sources WHERE id={entry})')
    return f"UPDATE track_sources SET ({','.join(track_sources.SOURCE_COLUMNS)})=({values}) WHERE id={entry};"


def refresh_range(conn, lower=None, upper=None, *, table='track_sources', where=None, args=(), predecessor=False):
    values = catalog_metadata._refresh_values(source=True).replace('NEW.', 'target.')
    if not predecessor:
        values = metadata_choice_selections.upgrade_selection_sql(with_genre_keys(
            values.replace(PREDECESSOR_ACTIVE_SQL, ACTIVE_SQL)
            .replace('metadata_runtime_choices', runtime_metadata_choices.VIEW)))
    values = values.replace('FROM chosen c JOIN track_sources s', f'FROM chosen c JOIN "{table}" s')
    sql = f"UPDATE \"{table}\" AS target SET ({','.join(track_sources.SOURCE_COLUMNS)})=({values})"
    if where is not None:
        conn.execute(sql + ' WHERE ' + where, args)
    elif lower is None:
        conn.execute(sql)
    else:
        conn.execute(sql + ' WHERE id BETWEEN ? AND ?', (lower, upper))


def guards():
    raw = ','.join((*track_sources.FIELDS, 'video_id'))
    changed = ' OR '.join(f'OLD.{field} IS NOT NEW.{field}' for field in (*track_sources.FIELDS, 'video_id'))
    yield f'CREATE TRIGGER track_source_metadata_insert AFTER INSERT ON track_sources BEGIN {_refresh("NEW.id")} END'
    yield (f'CREATE TRIGGER track_source_metadata_update AFTER UPDATE OF {raw} ON track_sources '
           f'WHEN {changed} BEGIN {_refresh("NEW.id")} END')
    for owner, column in (('metadata_choice_heads', 'observation_id'), ('metadata_runtime_choices', 'revision')):
        for event, row in (('INSERT', 'NEW'), (f'UPDATE OF {column}', 'NEW'), ('DELETE', 'OLD')):
            event_name = event.split()[0].lower()
            yield (f'CREATE TRIGGER track_source_metadata_{owner}_{event_name} AFTER {event} ON {owner} '
                   f'BEGIN {_refresh(row + ".entry_id")} END')
