"""Explicit preparation of bounded recommendation work and demand documents.

The installed proposal remains authoritative and is retained exactly. These
jobs never activate destination repositories or delete the original document.
Each known document has a finite domain limit (100 signatures or 24 requests).
"""
import argparse
import json

from .. import album_note_requests, home_offers, recommendation_work
from .rows import Job, main as run


def original(name, surfaces):
    choices = ','.join(repr(s) for s in surfaces)
    table = 'prepared_' + name + '_originals'
    return table, f'''CREATE TABLE {table} (
        surface TEXT PRIMARY KEY CHECK(surface IN ({choices})),
        payload TEXT NOT NULL, built_at REAL
    ) WITHOUT ROWID'''


def retain(conn, name, row):
    conn.execute(f'INSERT INTO prepared_{name}_originals VALUES (?,?,?) '
                 'ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload,built_at=excluded.built_at',
                 (row['surface'], row['payload'], row['built_at']))


WORK = ('daily_model_schedule', 'tv_preparation')
OFFERS = ('home_shown',)
REQUESTS = ('album_note_requests',)
BYTE_LIMITS = {'work': 16 * 1024, 'offers': 256 * 1024, 'album_requests': 256 * 1024}


def projection(name):
    return ('surface', f'substr(CAST(payload AS BLOB),1,{BYTE_LIMITS[name] + 1}) AS payload', 'built_at')


def document(name, row):
    """Reject oversized originals before parsing or retaining a partial value."""
    row = dict(row)
    payload = row['payload']
    encoded = payload.encode('utf-8') if isinstance(payload, str) else bytes(payload)
    if len(encoded) > BYTE_LIMITS[name]:
        raise ValueError(name + ' document exceeds its preparation byte budget')
    row['payload'] = encoded.decode('utf-8')
    return row


def work(conn, owner, key, row):
    row = document('work', row)
    surface = row['surface']
    value = recommendation_work.parse(surface, json.loads(row['payload']), row['built_at'])
    table = 'prepared_model_selection_schedule' if surface == WORK[0] else 'prepared_tv_preparation_state'
    conn.execute('DELETE FROM ' + table)
    if value is not None:
        conn.execute('INSERT INTO ' + table + ' VALUES (1,' + ','.join('?' for _ in value) + ')', value)
    retain(conn, 'work', row)


def delete_work(conn, owner, key):
    surface, = json.loads(key)
    if surface not in WORK:
        return
    table = 'prepared_model_selection_schedule' if surface == WORK[0] else 'prepared_tv_preparation_state'
    conn.execute('DELETE FROM ' + table)
    conn.execute('DELETE FROM prepared_work_originals WHERE surface=?', (surface,))


def offers(conn, owner, key, row):
    row = document('offers', row)
    parsed = home_offers.parse(json.loads(row['payload']), row['built_at'])
    conn.execute('DELETE FROM prepared_home_offer_signatures')
    conn.execute('DELETE FROM prepared_home_offer_dedup')
    if parsed is not None:
        epoch, signatures = parsed
        conn.execute('INSERT INTO prepared_home_offer_dedup VALUES (1,?,?)', (epoch, row['built_at']))
        conn.executemany('INSERT INTO prepared_home_offer_signatures VALUES (?)', [(s,) for s in signatures])
    retain(conn, 'offers', row)


def delete_offers(conn, owner, key):
    if json.loads(key) != ['home_shown']:
        return
    conn.execute('DELETE FROM prepared_home_offer_signatures')
    conn.execute('DELETE FROM prepared_home_offer_dedup')
    conn.execute('DELETE FROM prepared_offers_originals')


def requests(conn, owner, key, row):
    row = document('album_requests', row)
    parsed = album_note_requests.parse(json.loads(row['payload']), row['built_at'])
    conn.execute('DELETE FROM prepared_album_note_requests')
    for fields in parsed:
        browse, artist, title, year_text, year_number, thumbnail, until = fields
        reference, reason = album_note_requests._reference(conn, browse)
        conn.execute('INSERT INTO prepared_album_note_requests VALUES (?,?,?,?,?,?,?,?,?,?)',
                     (browse, reference, reason, artist, title, year_text, year_number, thumbnail, until, row['built_at']))
    retain(conn, 'album_requests', row)


def delete_requests(conn, owner, key):
    if json.loads(key) != ['album_note_requests']:
        return
    conn.execute('DELETE FROM prepared_album_note_requests')
    conn.execute('DELETE FROM prepared_album_requests_originals')


def selected(surfaces):
    return {'rec_proposals': '{prefix}surface IN (' + ','.join(repr(s) for s in surfaces) + ')'}


JOBS = {
    'work': Job('work', 79, 81, {'rec_proposals': ('surface',)}, (
        original('work', WORK),
        ('prepared_model_selection_schedule', '''CREATE TABLE prepared_model_selection_schedule (
            id INTEGER PRIMARY KEY CHECK(id=1), next_at REAL NOT NULL, updated_at REAL NOT NULL)'''),
        ('prepared_tv_preparation_state', '''CREATE TABLE prepared_tv_preparation_state (
            id INTEGER PRIMARY KEY CHECK(id=1), failed INTEGER NOT NULL CHECK(failed IN (0,1)),
            observed_at REAL, updated_at REAL NOT NULL)'''),
    ), work, delete_work, filters=selected(WORK), projections={'rec_proposals': projection('work')}),
    'home-offers': Job('offers', 79, 82, {'rec_proposals': ('surface',)}, (
        original('offers', OFFERS),
        ('prepared_home_offer_dedup', '''CREATE TABLE prepared_home_offer_dedup (
            id INTEGER PRIMARY KEY CHECK(id=1), epoch INTEGER NOT NULL CHECK(typeof(epoch)='integer' AND epoch>=0),
            built_at REAL NOT NULL)'''),
        ('prepared_home_offer_signatures', '''CREATE TABLE prepared_home_offer_signatures (
            signature TEXT PRIMARY KEY CHECK(length(CAST(signature AS BLOB)) BETWEEN 1 AND 1024)) WITHOUT ROWID'''),
    ), offers, delete_offers, filters=selected(OFFERS), projections={'rec_proposals': projection('offers')}),
    'album-requests': Job('album_requests', 79, 83, {'rec_proposals': ('surface',)}, (
        original('album_requests', REQUESTS),
        ('prepared_album_note_requests', '''CREATE TABLE prepared_album_note_requests (
            browse_id TEXT PRIMARY KEY, album_provider_id TEXT, unresolved_reason TEXT,
            artist TEXT NOT NULL, title TEXT NOT NULL, year_text TEXT, year_number INTEGER,
            thumbnail TEXT, requested_until REAL, updated_at REAL,
            CHECK((album_provider_id IS NULL)=(unresolved_reason IS NOT NULL)),
            CHECK(year_text IS NULL OR year_number IS NULL)) WITHOUT ROWID'''),
    ), requests, delete_requests, dependencies=('album_provider_ids',), filters=selected(REQUESTS),
        projections={'rec_proposals': projection('album_requests')}),
}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument('job', choices=tuple(JOBS))
    args, remaining = parser.parse_known_args(argv)
    return run(JOBS[args.job], remaining)


if __name__ == '__main__':
    raise SystemExit(main())
