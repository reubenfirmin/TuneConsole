"""Move display counters and cooldowns to the settings for each surface."""
from itertools import groupby
from operator import itemgetter

from .. import recommendation_impressions
from ..schema_contract import MigrationError
from ..schema_inspection import definitions


NAME = 'own_recommendation_impression_counts_on_settings'
RETIRED = 'rec_impressions'


def install(conn, _context):
    rows = conn.execute(f'SELECT surface,item_key,views,last_shown FROM {RETIRED} ORDER BY surface,item_key')
    for surface, group in groupby(rows, key=itemgetter(0)):
        recommendation_impressions.save(conn, surface, {item: [views, at] for _, item, views, at in group})
    conn.execute(f'DROP TABLE {RETIRED}')


def validate(conn):
    if definitions(conn, (RETIRED,)):
        raise MigrationError('Recommendation impression counters were not retired')
