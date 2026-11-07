"""Released policy messages and candidate collections admitted by the rebuild."""
from dataclasses import replace
import hashlib
import json
import math

from .. import json_cache
from ..evidence_ledger import canonical
from ..schema_contract import MigrationError
from ...rec import bundle_storage
from ...repos import proposal_cache


BASE_FIELD = '_policy_base'


def _link_sql(where='1'):
    return ('INSERT INTO recommendation_impression_policies('
            'impression_id,kind,raw_version,policy_version,resolution_reason) '
            "SELECT s.impression_id,s.kind,s.raw_version,p.version,CASE WHEN s.valid=0 THEN 'invalid_type' "
            "WHEN p.version IS NULL THEN 'missing_policy' END FROM ("
            "SELECT i.id impression_id,'primary' kind,i.model_version raw_version,1 valid "
            'FROM rec_served_impressions i WHERE ' + where + ' UNION ALL '
            "SELECT i.id,'pool',CAST(json_extract(i.provenance,'$.pool_policy') AS TEXT),"
            "json_type(i.provenance,'$.pool_policy') IN ('text','integer') "
            'FROM rec_served_impressions i WHERE (' + where + ') AND '
            "json_type(i.provenance,'$.pool_policy') IS NOT NULL AND "
            "json_type(i.provenance,'$.pool_policy')<>'null') s "
            'LEFT JOIN recommendation_policies p ON s.valid=1 AND p.version=s.raw_version')


def _configuration(raw, key):
    try:
        if not isinstance(raw, str):
            raise ValueError('Configuration must be JSON text')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError('Configuration must be an object')
        encoded = canonical({'format': 'recommendation-policy-1', 'configuration': value})
        if len(encoded) > 8 * 1024 * 1024:
            raise ValueError('Configuration exceeds file byte budget')
        return value
    except (ValueError, TypeError) as exc:
        raise MigrationError(f'Invalid recommendation policy {key!r}: {exc}') from exc


def _metadata(version, created):
    if (not isinstance(version, str) or not version
            or type(created) not in (float, int) or not math.isfinite(created)):
        raise MigrationError(f'Invalid recommendation policy metadata: {version!r}')


def legacy_plan(conn, *, destination):
    values, timestamps, identities = {}, [], []
    for surface in proposal_cache.SURFACES:
        head = conn.execute('SELECT cache_id,built_at FROM rec_proposal_cache_heads WHERE surface=?',
                            (surface,)).fetchone()
        if head:
            raw = proposal_cache.read(conn, surface)
            built = head[1]
        else:
            row = conn.execute('SELECT payload,built_at FROM rec_proposals WHERE surface=?', (surface,)).fetchone()
            if row is None:
                continue
            raw, built = row
        if len(raw.encode()) > bundle_storage.publication.MAX_PLAN_BYTES:
            raise ValueError('Legacy candidate source exceeds its migration byte budget')
        values[surface] = json_cache.decode(raw, profile_fields=('digest',),
                                           preserve_profiles=lambda item: not conn.execute(
                                               'SELECT 1 FROM recordings WHERE identity_key=?',
                                               (item.get('key'),)).fetchone(),
                                           max_bytes=bundle_storage.publication.MAX_PLAN_BYTES)
        timestamps.append(built)
        identities.append((surface, hashlib.sha256(raw.encode()).hexdigest(), built))
    if not values or all(value is None for value in values.values()):
        return None
    bundle, fresh = values.get('mode_bundles') or {}, values.get('fresh_pool')
    if not isinstance(bundle, dict) or fresh is not None and not isinstance(fresh, list):
        raise ValueError('Invalid original prepared candidate collection')
    versions = set()

    def candidates(value):
        if isinstance(value, list):
            for item in value:
                candidates(item)
        elif isinstance(value, dict):
            if 'pool_policy' in value:
                version = value['pool_policy']
                if not isinstance(version, str) or not version:
                    raise ValueError('Invalid original candidate policy')
                versions.add(version)
            else:
                for field, item in value.items():
                    if field != 'musical':
                        candidates(item)
    candidates(bundle)
    candidates(fresh)
    if not versions:
        if any(value for key, value in bundle.items() if key != '_meta') or fresh:
            raise ValueError('Original candidate output has no recorded policy')
        return None
    families = set()
    for version in sorted(versions):
        row = destination.execute('SELECT COALESCE(base_version,version) FROM recommendation_policies WHERE version=?',
                                  (version,)).fetchone()
        if row is None:
            raise ValueError('Missing original candidate policy: ' + version)
        families.add(row[0])
    if len(families) != 1:
        raise ValueError('Original candidate pools have different recorded input families')
    key = 'legacy-bundle:' + hashlib.sha256(canonical(identities)).hexdigest()
    plan = bundle_storage.prepare(destination, bundle, fresh, external_key=key, policy_version=min(versions),
                                  input_revision=None, created_at=max(timestamps))
    if values.get('mode_bundles') is None:
        plan = replace(plan, outputs=tuple(output for output in plan.outputs if output.surface != 'home'), framing=None)
    return plan
