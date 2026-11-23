"""Keep each rotation's mode offers together with Home settings."""
from itertools import groupby
from operator import itemgetter

from .. import mode_offers
from ..schema_contract import MigrationError
from ..schema_inspection import definitions


NAME = 'own_mode_offers_by_home_rotation'
RETIRED = 'rec_mode_impressions'


def install(conn, _context):
    rows = conn.execute(f'''SELECT epoch,lane,mode_id,legacy_mode_id,ranker,created_at
                           FROM {RETIRED} ORDER BY epoch,lane''')
    for epoch, group in groupby(rows, key=itemgetter(0)):
        mode_offers.save(conn, epoch, {lane: [mode, legacy, ranker, at] for _, lane, mode, legacy, ranker, at in group})
    conn.execute(f'DROP TABLE {RETIRED}')
    conn.execute(mode_offers.view_sql())


def validate(conn):
    if set(definitions(conn, (RETIRED, mode_offers.VIEW))) != {mode_offers.VIEW}:
        raise MigrationError('Mode offers were not moved to Home settings')
