"""Use the credit and mode owners' bounded preparation in one rebuild.

Verification derives permitted losses from the captured source, independently
of preparation cursors. Current selections, credit contents and mode identities
remain subject to the ordinary exact-value checks.
"""
from functools import partial

from .. import credit_retention, mode_retention
from . import source
from .build import Batch, Stage


def _prepare(reader, target, cursor, limit, *, domain, at):
    owner, state = ((credit_retention, 'artist_credit_retention') if domain == 'credits'
                    else (mode_retention, 'taste_mode_retention'))
    if cursor is None:
        # An installed prefix may have scanned older rows at an earlier time.
        # Revisit the complete immutable capture once, under this checkpoint.
        target.execute('UPDATE '+state+' SET cursor=0 WHERE id=1')
        if domain == 'modes':
            target.execute('UPDATE taste_mode_retention SET high_water=max(high_water,'
                           '(SELECT coalesce(max(mode_id),0) FROM taste_modes)) WHERE id=1')
    more = owner.step(target, at, activate=True)
    return Batch((cursor or 0)+1, int(more), not more)


def stages(at):
    return tuple(Stage('history:'+domain, partial(_prepare, domain=domain, at=at))
                 for domain in ('credits', 'modes'))


def retired_credit(reader, row, at):
    """Independent eligibility check against the original custody relations."""
    obsolete = row['capture_kind'] in ('legacy_capture', 'selection_snapshot')
    expired = (row['capture_kind'] in ('capture', 'provider_delivery') and row['last_seen'] is not None
               and row['last_seen'] < at-30*86400)
    if not obsolete and not expired:
        return False
    return not any(reader.execute('SELECT 1 FROM '+table+' WHERE observation_id=?', (row['id'],)).fetchone()
                   for table in ('artist_credit_capture_heads', 'entry_artist_selections'))


def verify(reader, target, *, at):
    tables = source.tables(reader)
    normalized = 'credit_id' in source.columns(reader, 'artist_credit_observations')
    original_receipts = 'artist_credit_replays' in tables
    expected_count = 0
    if original_receipts:
        for row in reader.execute('SELECT * FROM artist_credit_replays'):
            actual = target.execute('SELECT * FROM artist_credit_replays WHERE entry_id=? AND source=? '
                                    'AND delivery_key=?', tuple(row)[:3]).fetchone()
            if actual is None or tuple(actual) != tuple(row):
                raise ValueError('Rebuild changed a captured artist replay receipt')
            expected_count += 1
    if normalized:
        for row in reader.execute('SELECT * FROM artist_credit_observations WHERE delivery_key IS NOT NULL'):
            key = (row['entry_id'], row['source'], row['delivery_key'])
            old = (reader.execute('SELECT * FROM artist_credit_replays WHERE entry_id=? AND source=? '
                                  'AND delivery_key=?', key).fetchone() if original_receipts else None)
            if old is not None:
                continue
            actual = target.execute('SELECT * FROM artist_credit_replays WHERE entry_id=? AND source=? '
                                    'AND delivery_key=?', key).fetchone()
            expected = (*key, row['id'], row['credit_id'], int(row['metadata_delivery_id'] is not None))
            if actual is None or tuple(actual) != expected:
                raise ValueError('Rebuild lost or invented an artist replay outcome')
            expected_count += 1
    if target.execute('SELECT count(*) FROM artist_credit_replays').fetchone()[0] != expected_count:
        raise ValueError('Rebuild invented an artist replay receipt')
    credit_state = target.execute('SELECT enabled,cursor,completed,next_scan_at FROM artist_credit_retention '
                                  'WHERE id=1').fetchone()
    if tuple(credit_state) != (1, 0, 1, at+86400):
        raise ValueError('Rebuild left artist capture preparation incomplete')
    high_water = reader.execute('SELECT coalesce(max(mode_id),0) FROM taste_modes').fetchone()[0]
    if 'taste_mode_retention' in tables:
        high_water = max(high_water, reader.execute('SELECT high_water FROM taste_mode_retention WHERE id=1').fetchone()[0])
    state = target.execute('SELECT enabled,cursor,high_water,next_scan_at FROM taste_mode_retention WHERE id=1').fetchone()
    if tuple(state) != (1, 0, high_water, at+86400):
        raise ValueError('Rebuild changed mode allocation or left preparation incomplete')
    retired = 'taste_mode_retired_payloads' in tables
    count = 0
    for row in reader.execute('SELECT mode_id,active FROM taste_modes'):
        old = (reader.execute('SELECT retired_at FROM taste_mode_retired_payloads WHERE mode_id=?',
                              (row['mode_id'],)).fetchone() if retired else None)
        expected = old[0] if old is not None else at if not row['active'] else None
        actual = target.execute('SELECT retired_at FROM taste_mode_retired_payloads WHERE mode_id=?',
                                 (row['mode_id'],)).fetchone()
        if (actual[0] if actual is not None else None) != expected:
            raise ValueError('Rebuild changed a mode retirement receipt')
        count += expected is not None
    if target.execute('SELECT count(*) FROM taste_mode_retired_payloads').fetchone()[0] != count:
        raise ValueError('Rebuild invented a mode retirement receipt')
