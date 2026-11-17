"""Exact SQL verification for supported current-format copies."""
from .. import vibe_versions
from . import source
from .retention_plan import quote


def _exact_sql(reader, target, table, destination, mapping):
    """Compare every original value and SQLite type, including implicit row IDs."""
    def original(name):
        expression = 's.'+quote(source.keys(reader, table)[0] if name == '_legacy_id' else name)
        if name == vibe_versions.COLUMNS.get(table) and mapping[name] == name+'_id':
            return '(SELECT id FROM main.vibe_versions WHERE value='+expression+' COLLATE BINARY)'
        return expression
    info = list(reader.execute('PRAGMA table_info('+quote(table)+')'))
    keys = (['_legacy_id'] if '_legacy_id' in mapping else
            [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]])
    join = ' AND '.join(original(key)+' IS t.'+quote(mapping[key])+' COLLATE BINARY' for key in keys)
    equal = ' AND '.join('('+original(name)+' IS t.'+quote(field)+' COLLATE BINARY AND typeof('
        +original(name)+')=typeof(t.'+quote(field)+'))' for name, field in mapping.items())
    changed = target.execute('SELECT 1 FROM rebuild_source.'+quote(table)+' s LEFT JOIN main.'
        +quote(destination)+' t ON '+join+' WHERE NOT ('+equal+') LIMIT 1').fetchone()
    if changed:
        raise ValueError('Direct rebuild changed retained values or IDs: '+table)
    expected = reader.execute('SELECT count(*) FROM '+quote(table)).fetchone()[0]
    actual = target.execute('SELECT count(*) FROM '+quote(destination)).fetchone()[0]
    if actual < expected or actual != expected and table != 'vibe_versions':
        raise ValueError('Direct rebuild changed retained row count: '+table)
