"""The first mode offered in each lane of a Home menu rotation."""
import json
import sqlite3

from . import legacy_mode_payloads, worker_settings


PREFIX = 'taste_mode_offers:'
VIEW = 'taste_mode_offers'


def offers(conn, epoch):
    return json.loads(worker_settings.read(conn, PREFIX + str(epoch), '{}'))


def save(conn, epoch, values):
    worker_settings.save(conn, PREFIX + str(epoch), json.dumps(values, ensure_ascii=False))


def record(conn, epoch, rows, now):
    epoch, now = int(epoch), float(now)
    with conn.atomic(join=True):
        values = offers(conn, epoch)
        updated = False
        for row in rows:
            lane, mode_id = row[0], int(row[1])
            if lane is None or now != now or str(lane) in values:
                continue
            if not legacy_mode_payloads.exists(conn, mode_id):
                raise sqlite3.IntegrityError('FOREIGN KEY constraint failed')
            values[str(lane)] = [mode_id, None, row[2] if len(row) > 2 else None, now]
            updated = True
        if updated:
            save(conn, epoch, values)


def view_sql():
    return f'''CREATE VIEW {VIEW} AS
        SELECT CAST(substr(s.key,{len(PREFIX) + 1}) AS INTEGER) AS epoch,
            offer.key AS lane, offer.value->>0 AS mode_id, offer.value->>2 AS ranker,
            offer.value->>3 AS created_at, offer.value->>1 AS legacy_mode_id
        FROM settings s CROSS JOIN json_each(s.value) offer WHERE s.key GLOB '{PREFIX}*' '''
