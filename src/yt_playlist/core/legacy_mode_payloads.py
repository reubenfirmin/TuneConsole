"""Content-mode payloads attached to their existing musical group identities."""

VIEW = 'legacy_mode_payloads'
FIELDS = ('label', 'families', 'centroid', 'size', 'rep_keys', 'first_seen', 'last_seen', 'space', 'active')
PAYLOAD_FIELDS = (*FIELDS, 'payload_retired_at')


def view_sql():
    fields = ','.join(f'mode_{field} AS {field}' for field in PAYLOAD_FIELDS)
    return (f'CREATE VIEW {VIEW} AS SELECT legacy_mode_id AS mode_id,{fields} '
            'FROM taste_groups WHERE mode_label IS NOT NULL')


def exists(conn, mode_id):
    return conn.execute('SELECT 1 FROM taste_groups WHERE legacy_mode_id=? AND mode_label IS NOT NULL',
                        (mode_id,)).fetchone() is not None


def publish(conn, rows, retired):
    fields = ','.join(f'mode_{field}' for field in FIELDS)
    updates = ','.join(f'mode_{field}=excluded.mode_{field}' for field in FIELDS if field != 'first_seen')
    conn.executemany(f'''INSERT INTO taste_groups(external_key,legacy_mode_id,created_at,{fields})
        VALUES (?,?,?,?,?,?,?,?,?,?,?,1) ON CONFLICT(legacy_mode_id) DO UPDATE SET {updates},
        mode_first_seen=coalesce(taste_groups.mode_first_seen,excluded.mode_first_seen)''',
        ((f'legacy:{mode_id}', mode_id, first_seen, label, families, centroid, size, keys, first_seen, last_seen, space)
         for mode_id, label, families, centroid, size, keys, first_seen, last_seen, space in rows))
    conn.executemany('UPDATE taste_groups SET mode_active=0 WHERE legacy_mode_id=? AND mode_label IS NOT NULL',
                     ((mode_id,) for mode_id in retired))
