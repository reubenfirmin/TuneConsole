"""The primary and pool policy roles of a served impression."""

ROLES = ('primary', 'pool')
RAW_VERSION = {'primary': 'model_version',
               'pool': "CAST(json_extract(provenance,'$.pool_policy') AS TEXT)"}


def live_reference(version):
    return '(' + ' OR '.join(
        f'EXISTS(SELECT 1 FROM rec_served_impressions WHERE {role}_policy_version={version})'
        for role in ROLES) + ')'


def resolve(conn, versions):
    if not versions:
        return
    marks = ','.join('?' for _ in versions)
    for role, raw in RAW_VERSION.items():
        conn.execute(f"UPDATE rec_served_impressions SET {role}_policy_version={raw},{role}_policy_reason=NULL "
                     f"WHERE {role}_policy_reason='missing_policy' AND {raw} IN ({marks})", versions)


def archive(conn, version):
    for role in ROLES:
        conn.execute(f'UPDATE rec_served_impressions SET {role}_historical_version={role}_policy_version,'
                     f'{role}_policy_version=NULL WHERE {role}_policy_version=?', (version,))
