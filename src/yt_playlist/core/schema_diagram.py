"""Read the installed SQLite entities and declared foreign keys for Schema map."""
from collections import defaultdict
from itertools import groupby


def diagram(conn):
    """Return a detached inventory. Callers pin the schema for the whole read."""
    entities = {}
    relations = conn.execute('''
        SELECT s.name, p.type, s.sql FROM main.sqlite_schema s
        JOIN pragma_table_list p ON p.schema = 'main' AND p.name = s.name
        WHERE p.type IN ('table', 'view', 'virtual') ORDER BY s.name
    ''')
    for name, kind, sql in relations:
        if not name.startswith('sqlite_'):
            entities[name] = dict(id=name, kind=kind, sql=sql, columns=[])

    names = {name.lower(): name for name in entities}
    for table, column, declared, required, default, primary, hidden in conn.execute('''
        SELECT s.name, c.name, c.type, c."notnull", c.dflt_value, c.pk, c.hidden
        FROM main.sqlite_schema s CROSS JOIN pragma_table_xinfo(s.name, 'main') c
        WHERE s.type IN ('table', 'view') ORDER BY s.name, c.cid
    '''):
        if table in entities:
            entities[table]['columns'].append(dict(name=column, type=declared, required=bool(required),
                                                   default=default, pk=primary, hidden=hidden))

    primary = {name: [column['name'] for column in sorted(entity['columns'], key=lambda col: col['pk'])
                      if column['pk']] for name, entity in entities.items()}
    unique = defaultdict(list)
    indexes = conn.execute('''
        SELECT s.name, i.name, c.name
        FROM main.sqlite_schema s CROSS JOIN pragma_index_list(s.name, 'main') i
        CROSS JOIN pragma_index_info(i.name, 'main') c
        WHERE s.type = 'table' AND i."unique" AND NOT i.partial ORDER BY s.name, i.name, c.seqno
    ''')
    for (table, _), columns in groupby(indexes, key=lambda row: (row[0], row[1])):
        fields = frozenset(row[2] for row in columns)
        if None not in fields:
            unique[table].append(fields)

    edges = []
    references = conn.execute('''
        SELECT s.name, f.id, f."table", f."from", f."to", f.on_update, f.on_delete
        FROM main.sqlite_schema s CROSS JOIN pragma_foreign_key_list(s.name, 'main') f
        WHERE s.type = 'table' ORDER BY s.name, f.id, f.seq
    ''')
    for (source, key, parent, on_update, on_delete), rows in groupby(
            references, key=lambda row: (*row[:3], *row[5:])):
        if source not in entities:
            continue
        target = names.get(parent.lower(), parent)
        pairs = [(row[3], row[4]) for row in rows]
        source_columns = [left for left, _ in pairs]
        target_columns = (primary.get(target, []) if all(right is None for _, right in pairs)
                          else [right for _, right in pairs])
        for column in entities[source]['columns']:
            if column['name'] in source_columns:
                column['fk'] = True
        fields = set(source_columns)
        unique_child = (any(key_fields <= fields for key_fields in unique[source])
                        or bool(primary[source] and set(primary[source]) <= fields))
        edges.append(dict(id=f'{source}:{key}', source=source, target=target, columns=source_columns,
                          referenced_columns=target_columns, on_update=on_update, on_delete=on_delete,
                          many=not unique_child))

    return dict(entities=list(entities.values()), edges=edges)
