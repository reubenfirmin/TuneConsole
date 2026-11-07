"""Order cold conversions by hash route in the stable rebuild candidate.

Online conversion visits originals in identity/date order. Reusing that order
for a whole library repeatedly decompresses and rewrites unrelated trie leaves.
This private SQL work list keeps adjacent archive keys together. The canonical
domain writers still own eligibility, exact contents and atomic publication.
"""
import hashlib
import json

from .. import legacy_evidence, legacy_journals, policy_history, policy_config
from ...repos import policy_storage
from .build import Batch, Stage, quote

WORK = '_database_rebuild_archive_order'


def stages(now):
    def plan(reader, target, cursor, limit):
        target.execute('CREATE TABLE '+WORK+'(owner TEXT NOT NULL,route TEXT NOT NULL,'
                       'identity TEXT NOT NULL,PRIMARY KEY(owner,route,identity)) WITHOUT ROWID')
        def route(value):
            return hashlib.sha256(value.encode()).hexdigest()
        # These functions exist only while constructing this private work list.
        target.create_function('rebuild_archive_route',1,route,deterministic=True)
        target.create_function('rebuild_archive_key',2,legacy_journals.key,deterministic=True)
        try:
            count = 0
            for domain, table in legacy_evidence.DOMAINS.items():
                if target.execute('SELECT 1 FROM sqlite_schema WHERE name=?', (table,)).fetchone():
                    count += target.execute('INSERT INTO '+WORK+' SELECT ?,rebuild_archive_route('
                        'rebuild_archive_key(kind,source_id)),rebuild_archive_key(kind,source_id) FROM '+quote(table),
                        (domain,)).rowcount
            count += target.execute('INSERT INTO '+WORK+" SELECT 'policies',rebuild_archive_route(version),version "
                'FROM recommendation_policies p WHERE created_at<? AND '+policy_storage.operational_protection(target)
                +' AND EXISTS(SELECT 1 FROM recommendation_impression_policies i WHERE i.policy_version=p.version)',
                (now-policy_config.GRACE_SECONDS,)).rowcount
        finally:
            target.create_function('rebuild_archive_route',1,None)
            target.create_function('rebuild_archive_key',2,None)
        return Batch(rows=count, done=True)

    def convert(owner):
        def copy(reader, target, cursor, limit):
            pending = list(target.execute('SELECT route,identity FROM '+WORK+
                ' WHERE owner=? ORDER BY route,identity LIMIT ?', (owner, min(limit, 128))))
            selected = [row[1] for row in pending]
            if not selected:
                if owner != 'policies':
                    legacy_journals.prepare_batch(target, owner, min(limit, 128))
                return Batch(done=True)
            if owner == 'policies':
                count = policy_history.prepare_batch(target, now, min(limit, 128), versions=selected)
                removed = target.executemany('DELETE FROM '+WORK+' WHERE owner=? AND route=? AND identity=? '
                    'AND NOT EXISTS(SELECT 1 FROM recommendation_policies p WHERE p.version='+WORK+'.identity)',
                    [(owner, route, identity) for route, identity in pending]).rowcount
            else:
                count = legacy_journals.prepare_batch(target, owner, min(limit, 128), keys=map(json.loads, selected))
                table = quote(legacy_evidence.DOMAINS[owner])
                removed = target.executemany('DELETE FROM '+WORK+' WHERE owner=? AND route=? AND identity=? '
                    'AND NOT EXISTS(SELECT 1 FROM '
                    +table+' p WHERE p.kind=json_extract('+WORK+'.identity,\'$[0]\') '
                    'AND p.source_id=json_extract('+WORK+'.identity,\'$[1]\'))',
                    [(owner, route, identity) for route, identity in pending]).rowcount
            if removed != count or count == 0:
                raise ValueError('Archive work list did not advance with its domain publication: '+owner)
            return Batch((cursor or 0) + count, rows=count)
        return copy

    def finish(reader, target, cursor, limit):
        if target.execute('SELECT 1 FROM '+WORK+' LIMIT 1').fetchone():
            raise ValueError('Archive conversion left unfinished originals')
        target.execute('DROP TABLE '+WORK)
        return Batch(done=True)

    return (Stage('archive-order', plan),
            *(Stage('archives:' + owner, convert(owner)) for owner in (*legacy_journals.DOMAINS, 'policies')),
            Stage('archive-order-finish', finish))
