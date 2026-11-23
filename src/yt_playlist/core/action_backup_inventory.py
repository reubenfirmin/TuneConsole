"""Ordered playlist backup references owned by an action."""
import json


COLUMN = 'backup_inventory'
DECLARATION = f"{COLUMN} TEXT NOT NULL DEFAULT '[]' CHECK(json_type({COLUMN}) IS 'array')"


def decode(action, encoded):
    return [dict(item, action_id=action) for item in json.loads(encoded)]


def records(conn, action):
    row = conn.execute(f'SELECT {COLUMN} FROM actions WHERE id=?', (action,)).fetchone()
    return decode(action, row[0]) if row else []


def append(conn, action, position, reference, retained):
    filename, digest, size, availability = reference
    valid_file = (isinstance(retained, str) and bool(retained) and isinstance(digest, str)
                  and len(digest) == 64 and all(char in '0123456789abcdef' for char in digest)
                  and type(size) is int and size > 0)
    if (type(position) is not int or position < 0 or not isinstance(filename, str) or not filename
            or not (availability == 'verified' and valid_file
                    or availability == 'missing_historical' and retained is digest is None and size is None)):
        raise ValueError('Invalid action backup reference')
    items = records(conn, action)
    if any(item['position'] == position or item['filename'] == filename for item in items):
        raise ValueError('Action already owns this backup position or filename')
    items.append(dict(action_id=action, position=position, filename=filename,
                      retained_filename=retained, digest=digest, bytes=size, availability=availability))
    if not conn.execute(f'UPDATE actions SET {COLUMN}=? WHERE id=?', (encode(items), action)).rowcount:
        raise ValueError('Action backup requires an existing action')


def encode(items):
    return json.dumps([{key: value for key, value in dict(item).items() if key != 'action_id'}
                       for item in sorted(items, key=lambda item: item['position'])])


def relocate(conn, action, position, filename):
    item = conn.execute(f"""SELECT j.key FROM actions a, json_each(a.{COLUMN}) j
        WHERE a.id=? AND j.value->>'position'=?""", (action, position)).fetchone()
    if item is None:
        raise ValueError('Missing retained action backup')
    conn.execute(f'UPDATE actions SET {COLUMN}=json_set({COLUMN},?,?) WHERE id=?',
                 (f'$[{item[0]}].retained_filename', filename, action))
