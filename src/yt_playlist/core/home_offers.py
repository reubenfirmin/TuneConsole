"""The current Home offer deduplication window, independent of display output."""
import json

from .recommendation_work import finite

MAX_SIGNATURES = 100


def prepared_owner(conn):
    if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='home_offer_dedup'").fetchone():
        return False
    return bool(conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='published_offers'").fetchone()
                and conn.execute('SELECT 1 FROM published_offers WHERE id=1').fetchone())


def available(conn):
    return bool(conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='home_offer_dedup'").fetchone()
                or prepared_owner(conn))


def parse(value, built_at):
    finite(built_at)
    if not isinstance(value, dict):
        raise ValueError('Home offer deduplication must be an object')
    if not value:
        return None
    if set(value) != {'epoch', 'signatures'} or type(value['epoch']) is not int or value['epoch'] < 0:
        raise ValueError('Invalid Home offer window')
    signatures = value['signatures']
    if (not isinstance(signatures, list) or len(signatures) > MAX_SIGNATURES
            or any(not isinstance(item, str) or not 0 < len(item.encode()) <= 1024 for item in signatures)
            or len(set(signatures)) != len(signatures)):
        raise ValueError('Invalid Home offer signatures')
    return value['epoch'], sorted(signatures)


def read(conn):
    prefix = 'prepared_' if prepared_owner(conn) else ''
    row = conn.execute('SELECT epoch FROM ' + prefix + 'home_offer_dedup WHERE id=1').fetchone()
    if row is None:
        return None
    owner = '' if prefix else ' WHERE owner_id=1'
    return {'epoch': row[0], 'signatures': [item[0] for item in conn.execute(
        'SELECT signature FROM ' + prefix + 'home_offer_signatures' + owner + ' ORDER BY signature')]}


def replace(conn, value, built_at):
    if not conn.in_transaction:
        raise RuntimeError('Home offer replacement requires an outer transaction')
    parsed = parse(value, built_at)
    if prepared_owner(conn):
        conn.execute("INSERT INTO rec_proposals(surface,payload,built_at) VALUES ('home_shown',?,?) "
                     'ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload,built_at=excluded.built_at',
                     (json.dumps(value), built_at))
        return
    conn.execute('DELETE FROM home_offer_signatures WHERE owner_id=1')
    if parsed is None:
        conn.execute('DELETE FROM home_offer_dedup WHERE id=1')
        return
    epoch, signatures = parsed
    conn.execute('INSERT INTO home_offer_dedup VALUES (1,?,?) ON CONFLICT(id) '
                 'DO UPDATE SET epoch=excluded.epoch,built_at=excluded.built_at', (epoch, built_at))
    conn.executemany('INSERT INTO home_offer_signatures VALUES (1,?)', [(item,) for item in signatures])


def claim(conn, epoch, signatures, now):
    """The caller holds the writer reservation across read, comparison and replacement."""
    requested = sorted(set(signatures))
    parse({'epoch': epoch, 'signatures': requested}, now)
    previous = read(conn) or {}
    shown = set(previous.get('signatures', [])) if previous.get('epoch') == epoch else set()
    unseen = set(requested) - shown
    if unseen:
        replace(conn, {'epoch': epoch, 'signatures': sorted(shown | unseen)[-MAX_SIGNATURES:]}, now)
    return unseen
