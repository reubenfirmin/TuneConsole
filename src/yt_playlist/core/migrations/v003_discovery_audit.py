from .v001_baseline import setting, stamp


def apply(conn, context):
    if not setting(conn, 'discovery_enrichment_audit_requeued'):
        conn.execute('UPDATE discovered_tracks SET last_enriched=NULL')
        stamp(conn, 'discovery_enrichment_audit_requeued')
